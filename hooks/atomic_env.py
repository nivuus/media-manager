#!/usr/bin/env python3
"""Atomic .env writes, shared by install.py and activate.py.

A .env on a live host carries the real API keys and the Plex claim token.
Both hooks can run against an already-configured target, sometimes more than
once, so a write must never leave a torn or empty file behind if it is
interrupted, and a write that changes nothing must not disturb the file's
inode or mtime — an operator diffing timestamps across a reinstall should see
only what actually changed.
"""
import os
import stat
import tempfile

MODE = 0o600


def write_env(path, content):
    """Write `content` to the .env at `path`, atomically, only if it changed.

    A temporary file is created in the same directory as `path` (so the
    final os.replace() is a same-filesystem rename, which is atomic), set to
    `MODE` and to the existing file's owner before any content reaches it,
    fsynced, then moved over the target — the containing directory is
    fsynced too, right after, since the rename itself is a directory-entry
    change that needs its own durability guarantee. When `path` already
    holds exactly `content`, no write happens — but the mode is still fixed
    to `MODE` if it had drifted, since the "mode 0600 is guaranteed" comments
    at every call site make no exception for that case.
    """
    try:
        with open(path) as fh:
            existing_stat = os.fstat(fh.fileno())
            if fh.read() == content:
                if stat.S_IMODE(existing_stat.st_mode) != MODE:
                    os.chmod(path, MODE)
                return
    except FileNotFoundError:
        existing_stat = None

    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=f"{os.path.basename(path)}.tmp-")
    # The fd is ours to close from the instant mkstemp returns it: a failure
    # in fchmod/fchown below must not leak it, only os.fdopen() (right after)
    # hands that responsibility to the file object it wraps it in.
    try:
        os.fchmod(fd, MODE)
        # No existing file: stays owned by whoever runs this hook (root in
        # production). An existing one keeps ITS owner — .env is never
        # supposed to change hands just because its content did.
        if existing_stat is not None:
            os.fchown(fd, existing_stat.st_uid, existing_stat.st_gid)
    except BaseException:
        os.close(fd)
        os.unlink(tmp_path)
        raise

    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    _fsync_directory(directory)


def _fsync_directory(directory):
    """fsync the directory itself, right after the rename it follows.

    A rename() is a change to the DIRECTORY's own metadata (which name
    points at which inode), not to the file's content — fsyncing the file
    (already done, above) says nothing about whether the rename that follows
    it survives a crash. This is what does.
    """
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
