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
import tempfile

MODE = 0o600


def write_env(path, content):
    """Write `content` to the .env at `path`, atomically, only if it changed.

    A temporary file is created in the same directory as `path` (so the
    final os.replace() is a same-filesystem rename, which is atomic), set to
    `MODE` before any content reaches it, fsynced, then moved over the
    target. When `path` already holds exactly `content`, nothing is written.
    """
    try:
        with open(path) as fh:
            if fh.read() == content:
                return
    except FileNotFoundError:
        pass

    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=f"{os.path.basename(path)}.tmp-")
    try:
        os.fchmod(fd, MODE)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        os.unlink(tmp_path)
        raise
