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
import os
import pathlib
import stat
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


# Keep the test run from leaving a __pycache__ in tests/.
sys.dont_write_bytecode = True

from hook_fixtures import (fake_group_file, git_commit_tracked,  # noqa: E402
                           make_stack_skeleton, run_pkg)


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

# --- N3 (fix round 3): the pre-flight refuses ANY non-directory ancestor,
# not just a symlink one, and names the full path + reason -----------------
# _check_no_symlink_ancestors only checked os.path.islink(): a PLAIN FILE
# sitting where a directory is expected (no symlink involved at all) passed
# the pre-flight silently, and only failed later, at write time, inside the
# copy loop -- by which point docker-compose.yml and env.template (sorting
# before tdarr/server/marker.txt) were already deployed. The write-time
# message also said "symlink" even for this non-symlink case, and named
# only the bare component ("tdarr"), not the full path.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / "tdarr" / "server").mkdir(parents=True)
    (pkg / "stack" / "tdarr" / "server" / "marker.txt").write_text(
        "expected content\n")
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml",
                       "stack/tdarr/server/marker.txt")

    dest = pathlib.Path(root) / DEST_REL
    dest.mkdir(parents=True)
    (dest / "tdarr").write_text("not a directory at all\n")  # a plain FILE, no symlink
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("non-directory ancestor: exit status", proc.returncode, 1)
    check("non-directory ancestor: clean message",
          proc.stderr.startswith("media-manager install:"), True)
    check("non-directory ancestor: no traceback", "Traceback" in proc.stderr, False)
    check("non-directory ancestor: full path named in message",
          str(dest / "tdarr") in proc.stderr, True)
    check("non-directory ancestor: reason named, not \"symlink\"",
          "non-directory" in proc.stderr, True)
    check("non-directory ancestor: nothing deployed at all (compose)",
          (dest / "docker-compose.yml").exists(), False)
    check("non-directory ancestor: nothing deployed at all (env.template)",
          (dest / "env.template").exists(), False)
    check("non-directory ancestor: the plain file itself untouched",
          (dest / "tdarr").read_text(), "not a directory at all\n")

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

# --- N4 (fix round 3): mode/times must be set through the fd, never by
# name -- only the directory-swap half of item A had a dedicated test ------
# A regression to os.chmod(tmp_name, ..., dir_fd=dir_fd) /
# os.utime(tmp_name, ..., dir_fd=dir_fd) would still pass every OTHER test
# in this file: it compiles, "works" on the happy path, and dir_fd= sounds
# safe. It is not -- follow_symlinks defaults to True, so it would follow a
# symlink swapped in for the temp NAME after creation, mutating whatever
# that symlink points to. A spy on _create_temp_component performs exactly
# that swap right after the real temp file is created, before _write_file
# ever gets to set mode/times.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml")

    # Both modes pinned explicitly, not left to the process umask: an
    # earlier draft of this test picked 0o640 for the victim without
    # noticing that THIS host's umask (0o027) already makes a freshly
    # written source file 0o640 too, which would make the mode assertion
    # pass by coincidence even under the mutant it exists to catch.
    (pkg / "stack" / "docker-compose.yml").chmod(0o644)
    dest = pathlib.Path(root) / DEST_REL
    dest.mkdir(parents=True)
    victim = pathlib.Path(root) / "victim.txt"
    victim.write_text("victim original\n")
    os.chmod(victim, 0o600)
    victim_mode_before = stat.S_IMODE(victim.stat().st_mode)
    victim_mtime_before = victim.stat().st_mtime_ns

    real_create = safe_copy._create_temp_component

    def _create_then_swap(dir_fd, basename):
        tmp_name, fd = real_create(dir_fd, basename)
        if basename == "docker-compose.yml":
            os.unlink(tmp_name, dir_fd=dir_fd)
            os.symlink(str(victim), tmp_name, dir_fd=dir_fd)
        return tmp_name, fd

    with mock.patch("safe_copy._create_temp_component", side_effect=_create_then_swap):
        safe_copy.copy_stack(str(pkg), str(pkg / "stack"), str(dest))

    check("temp-name swap: victim mode unchanged",
          stat.S_IMODE(victim.stat().st_mode), victim_mode_before)
    check("temp-name swap: victim mtime unchanged",
          victim.stat().st_mtime_ns, victim_mtime_before)
    check("temp-name swap: victim content unchanged",
          victim.read_text(), "victim original\n")

# --- Deployed modes are git's, not the umask of the checkout --------------
# Root's umask here is 027: a checkout made by root has 0640 files and 0750
# directories, and copying those bits deployed Recyclarr's configuration
# unreadable by the PUID:PGID it runs as. Git records 0644 or 0755 only.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / "tool" / "nested").mkdir(parents=True)
    conf = pkg / "stack" / "tool" / "nested" / "conf.yml"
    conf.write_text("a: 1\n")
    script = pkg / "stack" / "run.py"
    script.write_text("print()\n")
    conf.chmod(0o640)
    script.chmod(0o750)
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml",
                       "stack/tool/nested/conf.yml", "stack/run.py")
    dest = pathlib.Path(root) / DEST_REL
    dest.mkdir(parents=True)
    old_umask = os.umask(0o077)
    try:
        safe_copy.copy_stack(str(pkg), str(pkg / "stack"), str(dest))
    finally:
        os.umask(old_umask)
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    check("git mode: plain file 0644", mode(dest / "tool" / "nested" / "conf.yml"), 0o644)
    check("git mode: executable 0755", mode(dest / "run.py"), 0o755)
    check("git mode: new directories 0755",
          (mode(dest / "tool"), mode(dest / "tool" / "nested")), (0o755, 0o755))

    # An existing directory keeps its mode: it may be a data directory.
    (dest / "tool").chmod(0o750)
    safe_copy.copy_stack(str(pkg), str(pkg / "stack"), str(dest))
    check("git mode: existing directory not re-stamped", mode(dest / "tool"), 0o750)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_safe_copy_writes: OK")
