"""The Downloads directory: what is still in it, what the queues reference,
and the 24-hour purge, which moves files to the quarantine (quarantine.py)
rather than deleting them."""
import errno
import logging
import os
import time

from maintenance.quarantine import QUARANTINE_DIRNAME, move_in

log = logging.getLogger(__name__)

MAX_FILE_AGE_HOURS = 24
# Category subdirectories Radarr and Sonarr have the download client file
# their downloads under; the purge recreates them if it emptied them.
REQUIRED_SUBDIRS = ['tv-sonarr', 'radarr']


def entry_name(path):
    """The name a queue path has in its parent directory."""
    return os.path.basename(str(path).rstrip('/'))


def walk_downloads(downloads_dir, onerror, topdown=True):
    """os.walk over Downloads, never entering the quarantine.

    What is in the quarantine is no download any more: never purged again,
    never a reason to believe a queue row's files are still there.
    """
    quarantine = os.path.join(downloads_dir, QUARANTINE_DIRNAME)
    for root, dirs, files in os.walk(downloads_dir, topdown=topdown, onerror=onerror):
        if root == quarantine or root.startswith(quarantine + os.sep):
            continue
        if root == downloads_dir:
            dirs[:] = [d for d in dirs if d != QUARANTINE_DIRNAME]
        yield root, dirs, files


def names_under(downloads_dir):
    """Every file and directory name in the Downloads directory, at any depth.

    Raises OSError when any directory cannot be listed: a name missing from a
    partial listing proves nothing.
    """
    def unlistable(error):
        raise error

    names = set()
    for _root, dirs, files in walk_downloads(downloads_dir, unlistable):
        names.update(dirs)
        names.update(files)
    return names


def download_present(item, names):
    """Whether a queue row's download is still on disk.

    True or False when `names`, from names_under(), settles it; None when it
    cannot be known: no listing, or a row without an outputPath. The whole
    tree is searched, not only the category subdirectories: SABnzbd files a
    finished job under complete/<category>/, and a download wrongly taken for
    a vanished one would be removed from its client.
    """
    output_path = item.get('outputPath')
    if names is None or not output_path:
        return None
    name = entry_name(output_path)
    return (name in names) if name else None


def protected_names(records):
    """Collect the download folder/file names still referenced by queue records.

    A file whose name matches an active queue entry must never be purged, even
    if it is older than 24h (large downloads can legitimately take longer).
    """
    active = set()
    for item in records:
        for key in ('outputPath', 'title', 'downloadId'):
            val = item.get(key)
            if val:
                active.add(entry_name(val))
                active.add(str(val))
    return active


def purge(downloads_dir, protected, owner, failures, batch):
    """Move the files older than 24 hours from Downloads into the quarantine.

    Files and folders still referenced by a queue (`protected`) are kept,
    whatever their age, so the caller must only purge when every queue was
    read. `batch` is this run's quarantine batch (quarantine.batch_name).
    `owner` is the (uid, gid) given to the recreated category
    subdirectories. I/O errors are recorded in `failures`; a file that
    cannot be renamed into the quarantine (another filesystem mounted inside
    Downloads included) stays where it is.
    """
    if not os.path.isdir(downloads_dir):
        # Reported, never created: with the media disk unmounted, creating it
        # would write into the bare mount point.
        failures.record('Downloads purge', f'{downloads_dir} is not a directory')
        return

    def unlistable(error):
        failures.record(f'Downloads purge: cannot list {error.filename}', error)

    current_time = time.time()
    max_age_seconds = MAX_FILE_AGE_HOURS * 3600

    for root, dirs, files in walk_downloads(downloads_dir, unlistable):
        # Never descend into a download folder that is still active.
        dirs[:] = [d for d in dirs if d not in protected]
        for name in files:
            file_path = os.path.join(root, name)
            if name in protected or os.path.basename(root) in protected:
                continue
            try:
                if current_time - os.path.getmtime(file_path) > max_age_seconds:
                    target = move_in(downloads_dir, file_path, batch)
                    log.info('Downloads purge: quarantined (older than %dh): %s -> %s',
                             MAX_FILE_AGE_HOURS, file_path, target)
            except FileNotFoundError:
                continue  # moved away since the listing (an import): nothing to do
            except OSError as error:
                failures.record(f'Downloads purge: cannot quarantine {file_path}', error)

    # Remove the directories left empty.
    for root, dirs, _files in walk_downloads(downloads_dir, unlistable, topdown=False):
        for name in dirs:
            if root == downloads_dir and name == QUARANTINE_DIRNAME:
                continue
            dir_path = os.path.join(root, name)
            try:
                os.rmdir(dir_path)
            except FileNotFoundError:
                continue  # removed since the listing: nothing to do
            except OSError as error:
                # Not empty is the normal case: the directory still holds files.
                if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    failures.record(
                        f'Downloads purge: cannot remove the empty directory {dir_path}',
                        error)

    # Recreate the category subdirectories Sonarr/Radarr expect.
    for subdir in REQUIRED_SUBDIRS:
        subdir_path = os.path.join(downloads_dir, subdir)
        try:
            os.makedirs(subdir_path, exist_ok=True)
            os.chown(subdir_path, *owner)
        except OSError as error:
            failures.record(f'Downloads purge: cannot restore {subdir_path}', error)
