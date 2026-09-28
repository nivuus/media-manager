#!/usr/bin/env python3
"""Safe stack/ deployment: git-tracked-only in a checkout, symlink-hardened.

install.py runs as root and writes into a tree that containers mount
read-write in places — the Tdarr container bind-mounts
tdarr/server/Tdarr/Plugins/... read-write (stack/docker-compose.yml), and
tracked files land there. A container that plants a symlink must not be
able to redirect a root-run reinstall's write anywhere else on the host.

What is guaranteed, and how:
- every source is validated before anything is written: a symlinked source
  is refused, a tracked file missing from the working tree is refused, both
  checked for every file before the first write (_validate_sources);
- every destination path's existing components are lstat-checked before the
  first write too (_validate_destinations) — an all-or-nothing pre-flight
  that fails fast on the common case, but is NOT itself proof against a
  change made after it runs (it re-resolves paths, like any lstat-then-act
  check);
- the actual, race-proof enforcement happens at write time, per file
  (_write_file): every destination directory component is opened one at a
  time through a directory file descriptor with O_NOFOLLOW | O_DIRECTORY,
  which the kernel itself refuses on a symlink or a non-directory — opening
  by (parent_fd, name) leaves no path string for anything to re-resolve
  after the refusal is decided. The temp file a write goes through is
  created the same way (O_EXCL | O_NOFOLLOW, dir_fd-relative, never
  tempfile.mkstemp(), which only takes a path), its mode and times are set
  through the open fd rather than shutil.copystat()'s by-path chmod/utime —
  the exact gap the re-review used to swap a symlink in — and the final
  os.replace() takes both sides as (dir_fd, name) pairs so it can never be
  pointed anywhere else either. xattrs are not copied: plain copy2()
  semantics (content, mode, mtime) are all that is promised.
- an export's directory walk (no .git) refuses a symlinked directory
  outright instead of silently skipping it, which is os.walk()'s own
  behaviour for a symlinked entry in dirnames when followlinks=False.
"""
import errno
import os
import secrets
import shutil
import stat
import subprocess

ENV_BASENAME = ".env"

# O_NOFOLLOW refuses a symlink at the final path component; O_DIRECTORY
# refuses anything that exists but is not a directory. Together, opening
# (parent_fd, name) with these flags is the one check that cannot be raced:
# there is no path left to resolve again after the kernel has decided.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def git_tracked_stack_files(pkg_dir):
    """The paths `git ls-files` reports under stack/, or None outside a checkout.

    A package directory with no .git — a file for a worktree, a directory
    for a plain clone — is a `git archive` export: every file it holds
    already went through the commit filter, so there is nothing left to
    distrust. `safe.directory=` is required because the install hook runs
    as root while the checkout is normally owned by the operator: git 2.47
    refuses a repository it does not own ("detected dubious ownership")
    unless told to trust it explicitly. A non-zero exit is raised, never
    swallowed into "copy everything" — that fallback is exactly what would
    ship an untracked .env or a service's config.xml.
    """
    if not os.path.exists(os.path.join(pkg_dir, ".git")):
        return None
    proc = subprocess.run(
        ["git", "-c", f"safe.directory={pkg_dir}", "-C", pkg_dir,
         "ls-files", "-z", "stack/"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-files -z stack/: {(proc.stderr or '').strip()}")
    return [path for path in proc.stdout.split("\0") if path]


def _walk_stack_files(stack_dir):
    """Every file's path relative to stack_dir, for a git-archive export.

    A symlinked subdirectory is refused outright, not silently skipped:
    left alone, os.walk(followlinks=False) still lists a symlinked entry in
    dirnames, just without descending into it, which would make an export
    succeed with content quietly missing instead of failing the way the
    checkout mode's source check refuses a symlinked file.
    """
    paths = []
    for dirpath, dirnames, filenames in os.walk(stack_dir, followlinks=False):
        for name in dirnames:
            if os.path.islink(os.path.join(dirpath, name)):
                rel = os.path.relpath(os.path.join(dirpath, name), stack_dir)
                raise RuntimeError(f"refusing a symlinked directory: {rel}")
        rel_dir = os.path.relpath(dirpath, stack_dir)
        for name in filenames:
            rel = name if rel_dir == os.curdir else os.path.join(rel_dir, name)
            paths.append(rel.replace(os.sep, "/"))
    return sorted(paths)


def _validate_sources(stack_dir, relpaths):
    """Every source must exist as a plain file, and never a symlink.

    Checked for ALL of them before writing ANY: git's index and the working
    tree are two different sources of truth, and one file missing (removed,
    or never restored, after being committed) must not leave a half-copied
    tree behind — the caller sees either the whole deploy or none of it. A
    symlinked source is refused outright — none is tracked today, and a
    root-run copy has no business preserving one blindly.
    """
    for rel in relpaths:
        source = os.path.join(stack_dir, rel)
        if os.path.islink(source):
            raise RuntimeError(f"refusing a symlinked source file: {rel}")
        if not os.path.isfile(source):
            raise RuntimeError(
                f"tracked file is missing from the working tree: {rel}")


def _check_no_symlink_ancestors(dest_root, target):
    """Raise if any directory between dest_root and target's parent is
    already a symlink, at the moment this specific call runs.

    This is a path-string, lstat-based check: like any check performed on a
    path rather than an open descriptor, something swapped in immediately
    after it returns would not be caught by it. It exists to fail fast on
    the common case (nothing tampered with); the actual guarantee against a
    swap made after this check runs is _write_file's own directory-fd walk,
    which re-verifies every component again, independently, right as it
    opens each one — there is no path left to re-resolve there, unlike here.
    """
    rel_dir = os.path.dirname(os.path.relpath(target, dest_root))
    if not rel_dir:
        return
    current = dest_root
    for part in rel_dir.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise RuntimeError(f"refusing to write through a symlink: {current}")


def _validate_destinations(dest_root, relpaths):
    """Check every destination path's existing ancestors before the first
    write, for the same all-or-nothing reason as _validate_sources: a
    symlink discovered on the fourth of twenty destination paths must not
    leave the first three already replaced (re-review finding: with the
    check running inside the copy loop instead, docker-compose.yml was
    deployed before the phase exited 1). This narrows the window; it is not
    the race-proof check — _write_file's directory-fd walk is, and runs
    regardless, per file, at write time.
    """
    for rel in relpaths:
        _check_no_symlink_ancestors(dest_root, os.path.join(dest_root, rel))


def _open_dir_component(parent_fd, name):
    """Open `name` under parent_fd, refusing anything but a real directory.

    O_NOFOLLOW + O_DIRECTORY make the kernel itself refuse a symlink or a
    plain file at `name` — observed on this host as ENOTDIR for both cases
    (a symlink-to-directory and a plain file alike), though POSIX leaves
    room for ELOOP too, so both are treated as the same refusal. Opening by
    (parent_fd, name) rather than resolving a path string is what makes
    this uncircumventable: there is no path left for anything to swap
    underneath between "checked" and "used", because checking IS using.
    """
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise RuntimeError(
                f"refusing to write through a symlink: {name}") from exc
        raise


def _ensure_dir_component(parent_fd, name):
    """Open `name` under parent_fd, creating it first if it does not exist.

    mkdir and the open that follows are two syscalls, not one atomic step,
    but a symlink planted in that gap is still caught: the open uses the
    same O_NOFOLLOW | O_DIRECTORY as an already-existing component, so a
    swap made in between fails loudly instead of being followed. Mode 0o777
    (subject to umask, exactly like os.makedirs()'s own default) is used
    for a newly created directory — new directories keep the process
    umask, existing ones are never re-stamped, by design (see task-4's
    fix-round-2 brief, "not in scope").
    """
    try:
        os.mkdir(name, 0o777, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return _open_dir_component(parent_fd, name)


def _open_dest_dir_fd(dest_root, rel_dir):
    """Walk from dest_root down to a file's own directory, one path
    component at a time, entirely through directory file descriptors.

    dest_root itself is opened by path: it is the deploy dir, fixed and
    root-controlled before this hook ever runs (see copy_stack), not
    something a container's bind mount reaches. Every component below it —
    the part a container CAN reach, such as Tdarr's own
    tdarr/server/Tdarr/Plugins/... — goes through _ensure_dir_component
    instead, which is what actually enforces the no-symlink guarantee.
    """
    dir_fd = os.open(dest_root, _DIR_FLAGS)
    try:
        if rel_dir:
            for part in rel_dir.split(os.sep):
                next_fd = _ensure_dir_component(dir_fd, part)
                os.close(dir_fd)
                dir_fd = next_fd
        return dir_fd
    except BaseException:
        os.close(dir_fd)
        raise


def _create_temp_component(dir_fd, basename):
    """A uniquely-named temp file under dir_fd, created with O_EXCL.

    tempfile.mkstemp() only takes a path, not a dir_fd, so it cannot be
    used here without reintroducing the exact path-based race this module
    exists to close. This is its dir_fd-relative equivalent: the same
    create-exclusive-and-retry-on-collision approach mkstemp uses
    internally, just addressed by (dir_fd, name) instead of a path string.
    """
    for _ in range(100):
        name = f".{basename}.tmp-{secrets.token_hex(8)}"
        try:
            fd = os.open(name, _TMP_FLAGS, 0o600, dir_fd=dir_fd)
            return name, fd
        except FileExistsError:
            continue
    raise RuntimeError(f"could not create a temp file for {basename}")


def _write_file(dest_root, rel, source):
    """Copy `source` to `dest_root/rel`, entirely through directory file
    descriptors: every path component is opened or created and the file
    itself is finally replaced by (dir_fd, name), never by a path string
    that could be re-resolved after being checked. See the module
    docstring for what this closes and why.
    """
    rel_dir, name = os.path.split(rel)
    dir_fd = _open_dest_dir_fd(dest_root, rel_dir)
    try:
        tmp_name, fd = _create_temp_component(dir_fd, name)
        try:
            src_stat = os.stat(source)
            with open(source, "rb") as src, os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fchmod(dst.fileno(), stat.S_IMODE(src_stat.st_mode))
                os.utime(dst.fileno(),
                         ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
                os.fsync(dst.fileno())
            os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(dir_fd)


def copy_stack(pkg_dir, stack_dir, dest):
    """Deploy stack/ to dest: git-tracked files only in a checkout, the
    whole subtree (walked, without following symlinks) otherwise. Never a
    .env in either mode — a force-added one in a checkout, or one simply
    sitting in an export's working tree, must never overwrite the live
    .env, since merge_env() has not even run yet at this point. See the
    module docstring for the symlink hardening applied on both the source
    and the destination side.
    """
    tracked = git_tracked_stack_files(pkg_dir)
    if tracked is None:
        relpaths = _walk_stack_files(stack_dir)
    else:
        prefix = "stack/"
        relpaths = [rel[len(prefix):] for rel in tracked
                   if rel.startswith(prefix)]  # ls-files was scoped to stack/
    relpaths = [rel for rel in relpaths
               if os.path.basename(rel) != ENV_BASENAME]

    _validate_sources(stack_dir, relpaths)
    _validate_destinations(dest, relpaths)
    # dest itself (and any of ITS OWN missing ancestors, e.g. a fresh
    # root/opt/nivuus/ on a first install) sits above the threat model this
    # module defends against: nothing a container's bind mount reaches
    # lives above it, only inside it. A plain, path-based makedirs is
    # enough here; every directory BELOW dest goes through _write_file's
    # directory-fd walk instead.
    os.makedirs(dest, exist_ok=True)

    for rel in relpaths:
        source = os.path.join(stack_dir, rel)
        _write_file(dest, rel, source)
