#!/usr/bin/env python3
"""hooks/data_dirs.py: data directories of services that do not run as root.

Docker creates a missing bind-mount source as root; a container running as
`user: PUID:PGID` then cannot write it and crashes at boot (Maintainerr stages
its UI into /opt/data on every start). The install hook therefore creates
these directories itself, owned by the rendered PUID/PGID. Real filesystem on
scratch roots; ownership is checked with a real chown when the suite runs as
root, and through a spy on os.fchown otherwise, so it never depends on the
user running it.

Run: python3 tests/test_data_dirs.py
"""
import io
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
from unittest import mock

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "tests"))
import data_dirs  # noqa: E402
from hook_fixtures import ANSWERS, context, fake_group_file  # noqa: E402

DEST_REL = "opt/nivuus/media-manager"
IS_ROOT = os.geteuid() == 0
OTHER = 4242  # an owner that is not the test user

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


# The declared list is data: Maintainerr is in it, with a private mode.
check("declared list", data_dirs.NON_ROOT_DATA_DIRS, {"maintainerr": 0o750})

# --- Missing dir: created 0750 (whatever the umask), owned as asked -------
old_umask = os.umask(0o000)
try:
    with tempfile.TemporaryDirectory() as dest:
        uid, gid = (OTHER, OTHER) if IS_ROOT else (os.getuid(), os.getgid())
        data_dirs.ensure_data_dirs(dest, uid, gid)
        path = os.path.join(dest, "maintainerr")
        check("created: is a directory", os.path.isdir(path), True)
        check("created: mode", mode(path), 0o750)
        check("created: owner", (os.stat(path).st_uid, os.stat(path).st_gid),
              (uid, gid))
finally:
    os.umask(old_umask)

# --- Existing dir, right owner: nothing is touched, chown not even called -
with tempfile.TemporaryDirectory() as dest:
    path = os.path.join(dest, "maintainerr")
    os.mkdir(path)
    os.chmod(path, 0o700)
    with mock.patch("os.fchown") as spy, mock.patch("os.geteuid", return_value=0):
        data_dirs.ensure_data_dirs(dest, os.getuid(), os.getgid())
    check("right owner: no chown", spy.call_count, 0)
    check("right owner: mode kept", mode(path), 0o700)

# --- Existing dir, other owner: only the dir itself is chowned ------------
with tempfile.TemporaryDirectory() as dest:
    path = os.path.join(dest, "maintainerr")
    os.mkdir(path)
    os.chmod(path, 0o755)
    inner = os.path.join(path, "maintainerr.sqlite")
    pathlib.Path(inner).write_text("data\n")
    with mock.patch("os.fchown") as spy, mock.patch("os.geteuid", return_value=0):
        data_dirs.ensure_data_dirs(dest, os.getuid() + 1, os.getgid() + 1)
    check("other owner: one chown", spy.call_count, 1)
    check("other owner: chown args", spy.call_args.args[1:],
          (os.getuid() + 1, os.getgid() + 1))
    check("other owner: mode kept", mode(path), 0o755)
    check("other owner: content untouched", os.path.isfile(inner), True)
    if IS_ROOT:
        data_dirs.ensure_data_dirs(dest, OTHER, OTHER)
        check("other owner: dir chowned", os.stat(path).st_uid, OTHER)
        check("other owner: content not chowned (not recursive)",
              os.stat(inner).st_uid, 0)

# --- Unprivileged (euid != 0): dir created, no chown, one named warning ---
# Exercised whoever runs the suite, by faking the euid: the shared CI gate runs
# the hook as uid 1001, where a chown to 1000 can only fail with EPERM.
with tempfile.TemporaryDirectory() as dest:
    path = os.path.join(dest, "maintainerr")
    with mock.patch("os.fchown") as spy, \
            mock.patch("os.geteuid", return_value=1001), \
            mock.patch("sys.stderr", new=io.StringIO()) as err:
        data_dirs.ensure_data_dirs(dest, 1000, 1000)
        data_dirs.ensure_data_dirs(dest, 1000, 1000)  # existing, still wrong
    check("unprivileged: dir created", os.path.isdir(path), True)
    check("unprivileged: mode 0750", mode(path), 0o750)
    check("unprivileged: no chown attempted", spy.call_count, 0)
    warnings = err.getvalue().splitlines()
    check("unprivileged: one warning per run", len(warnings), 2)
    check("unprivileged: warning names dir and owner",
          path in warnings[0] and "1000:1000" in warnings[0]
          and "when install runs as root" in warnings[0], True)

# --- A symlink in place of the dir is refused, its target untouched -------
with tempfile.TemporaryDirectory() as dest, \
        tempfile.TemporaryDirectory() as victim:
    os.symlink(victim, os.path.join(dest, "maintainerr"))
    before = mode(victim)
    try:
        data_dirs.ensure_data_dirs(dest, os.getuid() + 1, os.getgid() + 1)
        check("symlink: refused", "no error", "RuntimeError")
    except RuntimeError as exc:
        check("symlink: names the path", "maintainerr" in str(exc), True)
    check("symlink: target mode untouched", mode(victim), before)

# --- A plain file in place of the dir is refused --------------------------
with tempfile.TemporaryDirectory() as dest:
    pathlib.Path(dest, "maintainerr").write_text("not a dir\n")
    try:
        data_dirs.ensure_data_dirs(dest, os.getuid(), os.getgid())
        check("file: refused", "no error", "RuntimeError")
    except RuntimeError:
        pass


# --- Through the hook: created under the deploy dir, errors are named -----
def run_hook(root):
    return subprocess.run(
        [sys.executable, str(REPO / "hooks" / "install.py"),
         "--phase", "install", "--root", root],
        input=context(ANSWERS), capture_output=True, text=True, cwd=str(REPO))


with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    proc = run_hook(root)
    path = pathlib.Path(root) / DEST_REL / "maintainerr"
    # As root the dir ends up owned 1000:1000. Unprivileged it is only
    # created (a chown to another uid is impossible), with one warning unless
    # the user happens to be 1000:1000 already.
    check("hook: exit status", proc.returncode, 0)
    check("hook: dir created", path.is_dir(), True)
    check("hook: mode 0750", mode(path), 0o750)
    if IS_ROOT:
        check("hook: owned by the rendered PUID/PGID",
              (path.stat().st_uid, path.stat().st_gid), (1000, 1000))
    else:
        check("hook (unprivileged): no traceback",
              "Traceback" in proc.stderr, False)
        if (os.getuid(), os.getgid()) != (1000, 1000):
            check("hook (unprivileged): warns", "warning" in proc.stderr, True)

with tempfile.TemporaryDirectory() as root, \
        tempfile.TemporaryDirectory() as victim:
    fake_group_file(root)
    dest = pathlib.Path(root) / DEST_REL
    dest.mkdir(parents=True)
    (dest / "maintainerr").symlink_to(victim)
    proc = run_hook(root)
    check("hook symlink: exit status", proc.returncode, 1)
    check("hook symlink: named error",
          proc.stderr.startswith("media-manager install: "), True)
    check("hook symlink: no traceback", "Traceback" in proc.stderr, False)

# --- The owner comes from the EFFECTIVE .env, not from the defaults -------
# compose runs the container as ${PUID}:${PGID} read from the .env, and a
# reinstall keeps the existing values (merge_env), so the data dir must follow.
import install  # noqa: E402

for text, want in (("PUID=1234\nPGID=4321\n", (1234, 4321)),
                   ("A=1\nPUID=7\nPGID=8\nPUID=9\n", (9, 8)),
                   ('PUID="1234"\nPGID=\'55\'\n', (1234, 55))):
    check(f"env identity {text!r}", install.env_identity(text), want)
for text in ("PGID=1000\n", "PUID=1000\n", "PUID=abc\nPGID=1\n",
             "PUID=\nPGID=1\n", "PUID=-1\nPGID=1\n", "PUID=1_0\nPGID=1\n"):
    try:
        install.env_identity(text)
        check(f"env identity refused {text!r}", "no error", "ValueError")
    except ValueError as exc:
        check(f"env identity error names the key {text!r}",
              "PUID" in str(exc) or "PGID" in str(exc), True)


def install_with_env(env_text):
    """Run install() in-process on a scratch root, spying on the data dirs."""
    with tempfile.TemporaryDirectory() as root:
        fake_group_file(root)
        dest = pathlib.Path(root) / DEST_REL
        dest.mkdir(parents=True)
        (dest / ".env").write_text(env_text)
        with mock.patch.object(install, "ensure_data_dirs") as spy, \
                mock.patch.object(install, "emit"):
            try:
                install.install(root, dict(ANSWERS))
            except (ValueError, RuntimeError, OSError) as exc:
                return spy, exc
        return spy, None


spy, err = install_with_env("PUID=1234\nPGID=1235\nTZ=UTC\n")
check("existing .env: no error", err, None)
check("existing .env: dirs owned by its PUID/PGID",
      [c.args[1:] for c in spy.call_args_list], [(1234, 1235)])

spy, err = install_with_env("PUID=nope\nPGID=1000\n")
check("bad PUID: error", isinstance(err, ValueError), True)
check("bad PUID: no dir touched", spy.call_count, 0)

spy, err = install_with_env("")
check("fresh install: rendered defaults", err, None)
check("fresh install: defaults used",
      [c.args[1:] for c in spy.call_args_list],
      [(install.PUID, install.PGID)])

if IS_ROOT:
    with tempfile.TemporaryDirectory() as root:
        fake_group_file(root)
        dest = pathlib.Path(root) / DEST_REL
        dest.mkdir(parents=True)
        (dest / ".env").write_text("PUID=1234\nPGID=1234\n")
        proc = run_hook(root)
        check("hook, existing .env: exit status", proc.returncode, 0)
        st = (dest / "maintainerr").stat()
        check("hook, existing .env: real owner 1234",
              (st.st_uid, st.st_gid), (1234, 1234))

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_data_dirs: OK")
