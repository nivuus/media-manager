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
- a stack file is removed through a directory fd opened with O_NOFOLLOW
  below the deploy dir, the same rule as safe_copy.py, and only when it is a
  regular file.

systemd forgets a removed unit on the next daemon-reload, which activate
runs before starting the timers.
"""
import os
import stat

from safe_copy import DEST_ROOT_FLAGS

# media_cleanup.py was replaced by Maintainerr (rule-based, tiered on free
# space); its timer had not been armed since the 2026-09-28 audit.
RETIRED_UNITS = ("media-manager-cleanup.service", "media-manager-cleanup.timer")
RETIRED_STACK_FILES = ("media_cleanup.py",)
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


def retire_stack_files(dest):
    """Remove the retired files from the deploy dir, never through a symlink."""
    removed = []
    try:
        dir_fd = os.open(dest, DEST_ROOT_FLAGS)
    except FileNotFoundError:
        return removed
    try:
        for name in RETIRED_STACK_FILES:
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                os.unlink(name, dir_fd=dir_fd)
                removed.append(os.path.join(dest, name))
    finally:
        os.close(dir_fd)
    return removed
