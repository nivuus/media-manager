#!/usr/bin/env python3
"""Safe stack/ deployment: git-tracked-only in a checkout, symlink-hardened.

install.py runs as root and writes into a tree that containers mount
read-write in places — the Tdarr container bind-mounts
tdarr/server/Tdarr/Plugins/... read-write (stack/docker-compose.yml), and
tracked files land there. A container that plants a symlink ahead of time
must not be able to redirect a root-run reinstall's write anywhere else on
the host: every source is validated before anything is written, every
directory component between the deploy dir and a file is checked with
lstat before it is created or entered, and every destination write replaces
whatever sits at the final path rather than opening it.
"""
import os
import shutil
import subprocess
import tempfile

ENV_BASENAME = ".env"


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

    followlinks=False: a symlinked subdirectory (none exists in stack/
    today) is not descended into, matching the checkout mode's refusal of a
    symlinked source rather than silently reading through it.
    """
    paths = []
    for dirpath, _dirnames, filenames in os.walk(stack_dir, followlinks=False):
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
    """Raise if any directory between dest_root and target's parent is a symlink.

    Intermediate symlinks are always resolved by the kernel for path-based
    calls (mkdir, open, ...) — there is no way to refuse to follow one short
    of checking each component by hand first. This is what stops a
    container with a read-write bind mount under the deploy tree (Tdarr's
    tdarr/server/Tdarr/Plugins/...) from redirecting a root-run reinstall's
    write anywhere else on the host, by planting a symlink ahead of time.
    """
    rel_dir = os.path.dirname(os.path.relpath(target, dest_root))
    if not rel_dir:
        return
    current = dest_root
    for part in rel_dir.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise RuntimeError(f"refusing to write through a symlink: {current}")


def _replace_file(source, target):
    """Copy source to target, atomically, keeping copy2 semantics.

    Written through a temp file in target's own directory, then
    os.replace()d: a symlink already sitting at `target` (the final path
    component — the one thing _check_no_symlink_ancestors does not cover,
    since it is the file being written, not a directory on the way to it)
    is replaced as a directory entry, never opened and written through.
    """
    directory = os.path.dirname(target)
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=f"{os.path.basename(target)}.tmp-")
    try:
        with os.fdopen(fd, "wb") as dst, open(source, "rb") as src:
            shutil.copyfileobj(src, dst)
        shutil.copystat(source, tmp_path)
        os.replace(tmp_path, target)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


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

    for rel in relpaths:
        source = os.path.join(stack_dir, rel)
        target = os.path.join(dest, rel)
        _check_no_symlink_ancestors(dest, target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _replace_file(source, target)
