"""The Downloads directory: what the queues still reference, and the 24-hour purge."""
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


def purge(downloads_dir, protected, owner):
    """Remove the files older than 24 hours from the Downloads directory.

    Files and folders still referenced by a queue (`protected`) are kept,
    whatever their age. `owner` is the (uid, gid) given to the recreated
    category subdirectories.
    """
    current_time = time.time()
    max_age_seconds = MAX_FILE_AGE_HOURS * 3600

    for root, dirs, files in os.walk(downloads_dir):
        # Never descend into a download folder that is still active.
        dirs[:] = [d for d in dirs if d not in protected]
        for name in files:
            file_path = os.path.join(root, name)
            if name in protected or os.path.basename(root) in protected:
                continue
            try:
                file_age = current_time - os.path.getmtime(file_path)
                if file_age > max_age_seconds:
                    os.remove(file_path)
                    log.info('Downloads purge: removed (older than %dh): %s',
                             MAX_FILE_AGE_HOURS, file_path)
            except Exception as error:
                log.error('Downloads purge: cannot remove %s: %s', file_path, error)

    # Remove the directories left empty.
    for root, dirs, _files in os.walk(downloads_dir, topdown=False):
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass

    # Recreate the category subdirectories Sonarr/Radarr expect.
    for subdir in REQUIRED_SUBDIRS:
        subdir_path = os.path.join(downloads_dir, subdir)
        os.makedirs(subdir_path, exist_ok=True)
        os.chown(subdir_path, *owner)
