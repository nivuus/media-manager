#!/usr/bin/env python3
"""hooks/safe_copy.py: the write path itself, dir-fd hardened.

Split out of tests/test_safe_copy.py (which kept growing past 500 lines):
that file covers WHAT gets copied (checkout vs. export, .env exclusion,
missing files, git failures); this one covers HOW a single file gets
written once copy_stack has decided to write it — _write_file,
_open_dest_dir_fd and friends, and every symlink-hardening property they
are responsible for. Real filesystem, real symlinks, real syscalls
throughout; internals are reached directly (import safe_copy) only where a
test needs to patch one specific function as a transparent spy, never to
simulate the thing under test.

Run: python3 tests/test_safe_copy_writes.py
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
DEST_REL = "opt/nivuus/media-manager"

sys.path.insert(0, str(REPO / "hooks"))
import safe_copy  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# Same small fixtures as tests/test_safe_copy.py, duplicated rather than
# imported: these are standalone scripts, not guarded modules, so importing
# one from the other would re-run its entire suite as a side effect.
ANSWERS = {
    "media_root": "/srv/media",
    "transcode_dir": "/srv/transcode",
    "timezone": "Europe/Paris",
    "nvenc_node": True,
    "usenet": False,
    "plex_claim": "claim-abc",
}


def context(answers):
    return json.dumps({
        "package": {"name": "media-manager", "version": "1.0.0",
                    "root": str(REPO)},
        "hw": {"gpus": [{"slot": "00:02.0", "vendor": "intel",
                         "discrete": False}]},
        "answers": answers,
    })


def fake_group_file(root):
    """Une cible ou render vaut 106, pas la valeur par defaut 105."""
    path = pathlib.Path(root) / "etc" / "group"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("root:x:0:\nvideo:x:44:\nrender:x:106:\n")


def make_stack_skeleton(pkg):
    """A minimal fake package directory: hooks/, systemd/, and a tiny stack/."""
    shutil.copytree(REPO / "hooks", pkg / "hooks")
    shutil.copytree(REPO / "systemd", pkg / "systemd")
    (pkg / "stack").mkdir()
    shutil.copy2(REPO / "stack" / "env.template", pkg / "stack" / "env.template")
    (pkg / "stack" / "docker-compose.yml").write_text("services: {}\n")


def run_pkg(pkg, root, env=None):
    """Run a fake package's own install.py against --root root."""
    return subprocess.run(
        [sys.executable, str(pkg / "hooks" / "install.py"),
         "--phase", "install", "--root", root],
        input=context(ANSWERS), capture_output=True, text=True, cwd=str(pkg),
        env=env)


def git_commit_tracked(pkg, *tracked_paths):
    """git init + add + commit exactly the given paths, inside pkg."""
    def git(*args):
        proc = subprocess.run(["git", *args], cwd=str(pkg),
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    git("init", "-q")
    git("add", *tracked_paths)
    git("-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "tracked files")


# --- Root writes never follow symlinks: a hijacked ancestor directory -----
# (fix round 1, item 7). A container with a read-write bind mount under the
# deploy tree (Tdarr's tdarr/server/Tdarr/Plugins/..., see
# stack/docker-compose.yml) could plant a symlink ahead of a reinstall,
# redirecting a root-run write anywhere else on the host.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / "tdarr" / "server").mkdir(parents=True)
    (pkg / "stack" / "tdarr" / "server" / "marker.txt").write_text(
        "expected content\n")
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml",
                       "stack/tdarr/server/marker.txt")

    dest = pathlib.Path(root) / DEST_REL
    (dest / "tdarr").mkdir(parents=True)
    victim = pathlib.Path(root) / "victim"
    victim.mkdir()
    (dest / "tdarr" / "server").symlink_to(victim)
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("symlinked ancestor: exit status", proc.returncode, 1)
    check("symlinked ancestor: clean message",
          proc.stderr.startswith("media-manager install:"), True)
    check("symlinked ancestor: no traceback", "Traceback" in proc.stderr, False)
    check("symlinked ancestor: victim untouched",
          sorted(p.name for p in victim.iterdir()), [])
    check("symlinked ancestor: symlink itself untouched",
          (dest / "tdarr" / "server").is_symlink(), True)
    # fix round 2, item B: docker-compose.yml and env.template sort BEFORE
    # tdarr/server/marker.txt (d < e < t), so a per-file ancestor check run
    # inside the copy loop lets both land before the symlinked one is even
    # reached — a partial deploy, demonstrated by the re-review. The
    # pre-flight must refuse the whole batch before writing anything.
    check("symlinked ancestor: nothing else deployed either (compose)",
          (dest / "docker-compose.yml").exists(), False)
    check("symlinked ancestor: nothing else deployed either (env.template)",
          (dest / "env.template").exists(), False)

# --- Root writes never follow symlinks: a hijacked destination file -------
# The final path component is replaced (os.replace), never opened and
# written through: the symlink's target must survive exactly as it was.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / "tdarr" / "server").mkdir(parents=True)
    (pkg / "stack" / "tdarr" / "server" / "marker.txt").write_text(
        "expected content\n")
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml",
                       "stack/tdarr/server/marker.txt")

    dest = pathlib.Path(root) / DEST_REL
    (dest / "tdarr" / "server").mkdir(parents=True)
    victim = pathlib.Path(root) / "victim.txt"
    victim.write_text("original victim content\n")
    (dest / "tdarr" / "server" / "marker.txt").symlink_to(victim)
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("symlinked destination file: exit status", proc.returncode, 0)
    check("symlinked destination file: replaced with a regular file",
          (dest / "tdarr" / "server" / "marker.txt").is_symlink(), False)
    check("symlinked destination file: tracked content written",
          (dest / "tdarr" / "server" / "marker.txt").read_text(),
          "expected content\n")
    check("symlinked destination file: victim untouched",
          victim.read_text(), "original victim content\n")

# --- Root writes never follow symlinks: a component swapped in DURING the
# routine's own write, not before it (fix round 2, item A) -----------------
# The re-review demonstrated two races: shutil.copystat() and makedirs() /
# mkstemp() / os.replace() all take a path and re-resolve it, so a container
# watching its writable directory can swap a symlink in between the ancestor
# check and the actual write. Reproduced deterministically here (no real
# concurrency needed): a transparent spy on _ensure_dir_component performs
# the swap itself, then calls straight through to the real function, right
# as the routine is about to open that exact component — the narrowest
# possible window. The fix must refuse this at that exact moment (the open()
# call itself, via O_NOFOLLOW | O_DIRECTORY), not merely at some earlier
# check that this swap happens after.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / "tdarr" / "server").mkdir(parents=True)
    (pkg / "stack" / "tdarr" / "server" / "marker.txt").write_text(
        "expected content\n")
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml",
                       "stack/tdarr/server/marker.txt")

    dest = pathlib.Path(root) / DEST_REL
    (dest / "tdarr" / "server").mkdir(parents=True)  # a real dir, like after a prior install
    victim = pathlib.Path(root) / "victim"
    victim.mkdir()

    real_ensure = safe_copy._ensure_dir_component
    swapped = []

    def _swap_then_ensure(parent_fd, name):
        if name == "server" and not swapped:
            swapped.append(True)
            (dest / "tdarr" / "server").rmdir()
            (dest / "tdarr" / "server").symlink_to(victim)
        return real_ensure(parent_fd, name)

    raised = None
    with mock.patch("safe_copy._ensure_dir_component", side_effect=_swap_then_ensure):
        try:
            safe_copy.copy_stack(str(pkg), str(pkg / "stack"), str(dest))
        except RuntimeError as exc:
            raised = exc

    check("mid-routine swap: refused", raised is not None, True)
    check("mid-routine swap: victim untouched",
          sorted(p.name for p in victim.iterdir()), [])
    check("mid-routine swap: swap itself left in place, not written through",
          (dest / "tdarr" / "server").is_symlink(), True)

# --- N1 (fix round 3): the temp fd must not leak when the source is bad ---
# _write_file used to create the temp file/fd BEFORE stat-ing or opening the
# source: os.stat(source) or open(source, "rb") raising left the temp file
# unlinked (the except clause did that) but its fd never closed — a leak on
# every such failure. _open_fd_count uses /proc/self/fd, Linux-specific like
# the rest of this module's dir_fd usage.
def _open_fd_count():
    return len(os.listdir("/proc/self/fd"))


with tempfile.TemporaryDirectory() as root:
    dest = pathlib.Path(root) / "dest"
    (dest / "sub").mkdir(parents=True)

    before = _open_fd_count()
    caught = None
    try:
        safe_copy._write_file(str(dest), "sub/missing.txt",
                              str(dest / "sub" / "does-not-exist.txt"))
    except OSError as exc:
        caught = exc
    after = _open_fd_count()

    check("missing source: raised a clean OSError", isinstance(caught, OSError), True)
    check("missing source: no fd leak", after, before)
    check("missing source: no stray temp file left behind",
          list((dest / "sub").iterdir()), [])

with tempfile.TemporaryDirectory() as root:
    dest = pathlib.Path(root) / "dest"
    (dest / "sub").mkdir(parents=True)
    a_directory = pathlib.Path(root) / "a-directory"
    a_directory.mkdir()

    before = _open_fd_count()
    caught = None
    try:
        safe_copy._write_file(str(dest), "sub/missing.txt", str(a_directory))
    except OSError as exc:
        caught = exc
    after = _open_fd_count()

    check("directory as source: raised a clean OSError",
          isinstance(caught, OSError), True)
    check("directory as source: no fd leak", after, before)
    check("directory as source: no stray temp file left behind",
          list((dest / "sub").iterdir()), [])

# --- N2 (fix round 3, Ruling 24): the deploy dir ITSELF may be a symlink --
# It is root-controlled and no compose bind mount reaches it (every mount
# targets a subdirectory below it) -- an operator may legitimately symlink
# /opt/nivuus/media-manager to another disk. Only components BELOW it must
# refuse to be a symlink (the three tests above already cover that and
# needed no changes for this item).
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml")

    real_target = pathlib.Path(root) / "actual-disk" / "media-manager"
    real_target.mkdir(parents=True)
    dest = pathlib.Path(root) / DEST_REL
    dest.parent.mkdir(parents=True)
    dest.symlink_to(real_target)
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("symlinked deploy dir: exit status", proc.returncode, 0)
    check("symlinked deploy dir: still a symlink, not replaced",
          dest.is_symlink(), True)
    check("symlinked deploy dir: content landed in the real target",
          (real_target / "docker-compose.yml").is_file(), True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_safe_copy_writes: OK")
