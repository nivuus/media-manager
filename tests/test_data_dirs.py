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
    with mock.patch("os.fchown") as spy:
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
    with mock.patch("os.fchown") as spy:
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
    if IS_ROOT or (os.getuid(), os.getgid()) == (1000, 1000):
        check("hook: exit status", proc.returncode, 0)
        check("hook: dir created", path.is_dir(), True)
        check("hook: mode 0750", mode(path), 0o750)
        check("hook: owned by the rendered PUID/PGID",
              (path.stat().st_uid, path.stat().st_gid), (1000, 1000))
    else:
        # Not root and not 1000: the chown is refused, loudly and by name.
        check("hook (unprivileged): exit status", proc.returncode, 1)
        check("hook (unprivileged): named error",
              proc.stderr.startswith("media-manager install: "), True)

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

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_data_dirs: OK")
