#!/usr/bin/env python3
"""Remove what an earlier release of this package installed and this one no longer ships.

The nivuus engine has no notion of a file a package used to own: the install
hook only ever writes. Dropping a unit or a script from the package would
therefore leave the previous release's copy on every host that ran it — a
unit systemd still loads, a script an operator can still run by hand — and
nothing would ever tell them apart from a current file.

What is retired is DATA (RETIRED_UNITS, RETIRED_STACK_FILES): retiring
something else later is one entry. Only names in this package's own
namespace are listed, and only the shapes this package itself wrote are
removed:

- a unit file is removed only when it is a regular file. A symlink at that
  name is an operator's decision (a mask, most likely) and is left alone;
- the .wants link is removed only when it points at our unit path, which is
  exactly what activate.arm() creates;
- the OnFailure drop-in is removed only when is_our_dropin() recognises its
  exact content, and its .d directory only when that leaves it empty;
- a stack file is removed through directory fds opened one component at a
  time with O_NOFOLLOW below the deploy dir, the same rule as safe_copy.py,
  and only when it is a regular file. A symlinked directory on the way is
  never followed: the file behind it is left alone. The directories a
  removal leaves empty go too, deepest first, stopping at the first one that
  still holds something — so a directory the current release ships into, or
  one holding an operator's file, always survives.

systemd forgets a removed unit on the next daemon-reload, which activate
runs before starting the timers.
"""
import errno
import os
import stat

from safe_copy import DEST_ROOT_FLAGS, DIR_FLAGS

# media_cleanup.py was replaced by Maintainerr (rule-based, tiered on free
# space); its timer had not been armed since the 2026-09-28 audit.
RETIRED_UNITS = ("media-manager-cleanup.service", "media-manager-cleanup.timer")
# The Dolby Vision re-encode plugin left the flow on 2026-07-26 and was never
# brought back; its tooling (fetch-binaries.sh, the mounted tdarr/bin) and its
# logic copy went with it on 2026-10-07. addNormalizedStereo was replaced by
# ensureStereoPerLanguage, and deploy_flow_v6.py by deploy_tdarr.py, the same
# day (Tdarr audit).
RETIRED_STACK_FILES = (
    "media_cleanup.py",
    "tdarr/bin/fetch-binaries.sh",
    "scripts/tdarr/deploy_flow_v6.py",
    "scripts/tdarr/dv_reencode_qsv.logic.mjs",
    "scripts/tdarr/dv_reencode_qsv.test.mjs",
    "tdarr/server/Tdarr/Plugins/FlowPlugins/LocalFlowPlugins/audio/"
    "addNormalizedStereo/1.0.0/index.js",
    "tdarr/server/Tdarr/Plugins/FlowPlugins/LocalFlowPlugins/video/"
    "dvReencodeQsv/1.0.0/index.js",
)
WANTS_DIRS = ("timers.target.wants",)


def _remove_regular(path):
    """Remove path when it is a regular file; return whether it was."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    os.remove(path)
    return True


def retire_units(unit_dir, dropin_name, is_our_dropin):
    """Remove the retired units, their .wants links and our drop-ins."""
    removed = []
    for unit in RETIRED_UNITS:
        for wants in WANTS_DIRS:
            link = os.path.join(unit_dir, wants, unit)
            if (os.path.islink(link)
                    and os.readlink(link) == os.path.join("/etc/systemd/system", unit)):
                os.remove(link)
                removed.append(link)

        dropin_dir = os.path.join(unit_dir, f"{unit}.d")
        dropin = os.path.join(dropin_dir, dropin_name)
        if is_our_dropin(dropin):
            os.remove(dropin)
            removed.append(dropin)
            if not os.path.islink(dropin_dir) and not os.listdir(dropin_dir):
                os.rmdir(dropin_dir)

        path = os.path.join(unit_dir, unit)
        if _remove_regular(path):
            removed.append(path)
    return removed


def _retire_one(root_fd, dest, rel):
    """Remove one retired file, then the directories that leaves empty."""
    parts = rel.split("/")
    fds = [root_fd]
    try:
        for name in parts[:-1]:
            try:
                fds.append(os.open(name, DIR_FLAGS, dir_fd=fds[-1]))
            except FileNotFoundError:
                return []
            except OSError as exc:
                # A symlink or a file where a directory was: not our shape.
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    return []
                raise
        try:
            st = os.stat(parts[-1], dir_fd=fds[-1], follow_symlinks=False)
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(st.st_mode):
            return []
        os.unlink(parts[-1], dir_fd=fds[-1])
        removed = [os.path.join(dest, rel)]
        # fds[i] is the directory holding parts[i]; walk back up from the leaf.
        for depth in range(len(parts) - 2, -1, -1):
            try:
                os.rmdir(parts[depth], dir_fd=fds[depth])
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    break
                raise
            removed.append(os.path.join(dest, *parts[:depth + 1]))
        return removed
    finally:
        for fd in fds[1:]:
            os.close(fd)


def retire_stack_files(dest):
    """Remove the retired files from the deploy dir, never through a symlink."""
    try:
        root_fd = os.open(dest, DEST_ROOT_FLAGS)
    except FileNotFoundError:
        return []
    removed = []
    try:
        for rel in RETIRED_STACK_FILES:
            removed += _retire_one(root_fd, dest, rel)
    finally:
        os.close(root_fd)
    return removed
