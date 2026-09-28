"""The Downloads directory: what the queues still reference, and the 24-hour purge."""
import errno
import logging
import os
import time

log = logging.getLogger(__name__)

MAX_FILE_AGE_HOURS = 24
# Category subdirectories Radarr and Sonarr have the download client file
# their downloads under; the purge recreates them if it emptied them.
REQUIRED_SUBDIRS = ['tv-sonarr', 'radarr']


def entry_name(path):
    """The name a queue path has in its parent directory."""
    return os.path.basename(str(path).rstrip('/'))


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


def purge(downloads_dir, protected, owner, failures):
    """Remove the files older than 24 hours from the Downloads directory.

    Files and folders still referenced by a queue (`protected`) are kept,
    whatever their age, so the caller must only purge when every queue was
    read. `owner` is the (uid, gid) given to the recreated category
    subdirectories. I/O errors are recorded in `failures`.
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

    for root, dirs, files in os.walk(downloads_dir, onerror=unlistable):
        # Never descend into a download folder that is still active.
        dirs[:] = [d for d in dirs if d not in protected]
        for name in files:
            file_path = os.path.join(root, name)
            if name in protected or os.path.basename(root) in protected:
                continue
            try:
                if current_time - os.path.getmtime(file_path) > max_age_seconds:
                    os.remove(file_path)
                    log.info('Downloads purge: removed (older than %dh): %s',
                             MAX_FILE_AGE_HOURS, file_path)
            except FileNotFoundError:
                continue  # moved away since the listing (an import): nothing to do
            except OSError as error:
                failures.record(f'Downloads purge: cannot remove {file_path}', error)

    # Remove the directories left empty.
    for root, dirs, _files in os.walk(downloads_dir, topdown=False,
                                      onerror=unlistable):
        for name in dirs:
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
