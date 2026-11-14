#!/usr/bin/env python3
"""The maintenance units and their OnFailure= drop-ins, placed by the install hook.

Split out of install.py: placing units is its own concern (where they go,
their mode, what an operator's mask means), next to deploying the stack and
rendering the .env. A unit or drop-in is written through a temp file and a
rename, never by path; a symlink at its name is the operator's and is left
alone (see _place_file).
"""
import errno
import os
import stat

from safe_copy import DEST_ROOT_FLAGS, DIR_FLAGS, DIR_MODE, replace_file_at

UNITS_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "systemd")
UNIT_REL_DIR = "etc/systemd/system"

UNITS = [
    "media-manager-reset-error.service", "media-manager-reset-error.timer",
    "media-manager-update-wanted.service", "media-manager-update-wanted.timer",
]

# systemd-failure-notify@.service belongs to the host, not to this package
# (see CLAUDE.md): it is what actually sends an alert on a failed run, but
# this package must never declare a hard dependency on a unit it does not
# ship. A drop-in wires each maintenance service to it, ONLY when the host
# has it — a reinstall must not resurrect a dangling OnFailure= after an
# operator removes that host unit.
FAILURE_NOTIFY_UNIT = "systemd-failure-notify@.service"
FAILURE_DROPIN_NAME = "10-on-failure.conf"
FAILURE_DROPIN_CONTENT = "[Unit]\nOnFailure=systemd-failure-notify@%n.service\n"

# A unit or a drop-in is data, not a program.
UNIT_MODE = 0o644


def _place_file(dir_fd, name, data):
    """Write (dir_fd, name) unless it is a symlink; return whether it was written.

    A symlink at a unit's name is an operator's mask (to /dev/null: the only
    form a mask takes in /etc/systemd/system), and a mask wins over the
    package. Writing by path would follow it: the content would land in the
    link's target and that target would become 0644 — for /dev/null, every
    non-root process on the host would lose it. Anything else is replaced
    through a temp file, never written through.
    """
    try:
        if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            return False
    except FileNotFoundError:
        pass
    replace_file_at(dir_fd, name, data, UNIT_MODE)
    return True


def place_unit(name, dest_dir):
    """Place one shipped unit in dest_dir; False when an operator's mask is kept."""
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(UNITS_SRC, name), "rb") as fh:
        data = fh.read()
    dir_fd = os.open(dest_dir, DEST_ROOT_FLAGS)
    try:
        return _place_file(dir_fd, name, data)
    finally:
        os.close(dir_fd)


def _is_our_dropin(path):
    """True only for the exact file this hook itself would have written.

    Never a symlink (it could point anywhere, and removing it would not be
    "removing our content" but removing whatever name an operator or another
    tool chose to put there) and never a directory (os.path.isfile is False
    for one, so this returns False instead of letting a later open() crash
    the hook). Any other content at the path — an operator's drop-in for a
    different notifier, most likely — is left alone: this is provenance, not
    "the path is ours because we chose the name."

    Read and compared as bytes, never as decoded text: a text-mode open()
    uses universal newlines, which would silently turn a CRLF copy of our
    content into a match (and get it deleted), and raises UnicodeDecodeError
    — uncaught here, since sync_failure_dropins runs after this hook's own
    try/except block — on the first non-UTF-8 byte in an unrelated file.
    """
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as fh:
            return fh.read() == FAILURE_DROPIN_CONTENT.encode()
    except OSError:
        return False


def sync_failure_dropins(root):
    """Write or drop the OnFailure= alert drop-in for each maintenance service.

    Only the .service units get one — never the .timer units, which never
    fail themselves. When the host has no systemd-failure-notify@, a
    drop-in a previous install left behind is removed — but only when it is
    still exactly that (see _is_our_dropin); nothing else at that path, and
    nothing else in that directory, is ever touched. A symlink in place of
    the drop-in or of its .d directory is the operator's and is left alone,
    like a masked unit (see _place_file). Returns the paths left alone.
    """
    unit_dir = os.path.join(root, UNIT_REL_DIR)
    notify_present = os.path.isfile(os.path.join(unit_dir, FAILURE_NOTIFY_UNIT))
    kept = []
    for unit in (u for u in UNITS if u.endswith(".service")):
        dropin = os.path.join(unit_dir, f"{unit}.d", FAILURE_DROPIN_NAME)
        if notify_present:
            if not _write_dropin(unit_dir, f"{unit}.d"):
                kept.append(dropin)
        elif _is_our_dropin(dropin):
            os.remove(dropin)
    return kept


def _write_dropin(unit_dir, dropin_dir):
    """Write our drop-in under unit_dir/dropin_dir; False when a symlink is kept."""
    os.makedirs(unit_dir, exist_ok=True)
    root_fd = os.open(unit_dir, DEST_ROOT_FLAGS)
    try:
        try:
            os.mkdir(dropin_dir, DIR_MODE, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            dir_fd = os.open(dropin_dir, DIR_FLAGS, dir_fd=root_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR) and os.path.islink(
                    os.path.join(unit_dir, dropin_dir)):
                return False
            raise
    finally:
        os.close(root_fd)
    try:
        return _place_file(dir_fd, FAILURE_DROPIN_NAME, FAILURE_DROPIN_CONTENT.encode())
    finally:
        os.close(dir_fd)
