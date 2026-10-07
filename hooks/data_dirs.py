#!/usr/bin/env python3
"""Data directories of the services that do not run as root.

Docker creates a missing bind-mount source as root. A service started with
`user: "${PUID}:${PGID}"` (Maintainerr: the image has no PUID/PGID handling,
it runs as the compose user) then cannot write its own data directory, and
fails at boot. The install hook creates these directories itself, owned by
the PUID/PGID it renders into the .env.

Which services need it is DATA (NON_ROOT_DATA_DIRS), not a special case:
adding one is one entry. Services that start as root and drop privileges
themselves (the linuxserver images, through PUID/PGID) do not belong here.

Same safety rules as safe_copy.py, whose directory-fd helpers this reuses:
the deploy dir is opened following a symlink, everything below it is opened
with O_NOFOLLOW | O_DIRECTORY, and chown/chmod go through the open fd, never
through a path. A symlink planted where a data directory belongs is
refused: this runs as root, and a chown that followed it would hand an
arbitrary host directory to the service user.

Ownership needs root: when the hook runs unprivileged (euid != 0) the
directory is still created but not chowned, and one warning names it.

Ownership is fixed on the directory ITSELF, never recursively. Only the
directory this hook created is known to be ours; what lives inside an
existing one was written by the service (or restored by the operator) and
may deliberately belong to someone else. The service's own start-up
reports a file it cannot write by name, which is a better signal than a
silent recursive chown over data we do not own. Mode is applied only to a
directory this hook creates; an existing one keeps the operator's mode.
"""
import errno
import os
import sys

from safe_copy import DIR_FLAGS, DEST_ROOT_FLAGS, refuse_component

# Relative to the deploy dir -> mode of the directory when it is created.
# 0750: the service user owns it, its group may read, nobody else.
NON_ROOT_DATA_DIRS = {
    "maintainerr": 0o750,
    # Holds the tracked recyclarr.yml (laid by the install copy, as root)
    # AND the state Recyclarr writes next to it, so the directory itself
    # must be the service user's.
    "recyclarr": 0o750,
    "lingarr": 0o750,
}


def _ensure_owned_dir(root_fd, dest, name, mode, uid, gid):
    created = True
    try:
        os.mkdir(name, mode, dir_fd=root_fd)
    except FileExistsError:
        created = False
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=root_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            refuse_component(os.path.join(dest, name), exc)
        raise
    try:
        if created:
            os.fchmod(fd, mode)  # mkdir's mode is masked by the umask
        st = os.fstat(fd)
        if (st.st_uid, st.st_gid) != (uid, gid):
            if os.geteuid() == 0:
                os.fchown(fd, uid, gid)
            else:
                # Not an error swallowed: a process that is not root cannot
                # give a directory to another uid, by definition. The engine
                # installs as root; an unprivileged run (the CI idempotence
                # gate, a developer checkout) only gets the directory.
                print(f"media-manager install: warning: {os.path.join(dest, name)}"
                      f" is not owned by {uid}:{gid}; ownership is set when"
                      " install runs as root", file=sys.stderr)
    finally:
        os.close(fd)


def ensure_data_dirs(dest, uid, gid, dirs=None):
    """Create each declared directory under dest and give it to uid:gid."""
    dirs = NON_ROOT_DATA_DIRS if dirs is None else dirs
    root_fd = os.open(dest, DEST_ROOT_FLAGS)
    try:
        for name, mode in dirs.items():
            _ensure_owned_dir(root_fd, dest, name, mode, uid, gid)
    finally:
        os.close(root_fd)
