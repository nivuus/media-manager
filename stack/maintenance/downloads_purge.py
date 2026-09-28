"""The Downloads directory: whether what is in it can be trusted, what is still
in it, what the queues reference, and the 24-hour purge."""
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


def unmounted_library_reason(library_dirs):
    """Why the media library does not look mounted, or None when it does.

    The evidence is one library directory (Movies, TV Shows) that exists and
    holds at least one entry. Downloads alone proves nothing: with the media
    disk unmounted, Docker recreates the bind sources as empty directories,
    and every download would then look vanished.
    """
    reasons = []
    for path in library_dirs:
        try:
            with os.scandir(path) as entries:
                if next(entries, None) is not None:
                    return None
        except OSError as error:
            reasons.append(f'{path}: {error.strerror or error}')
        else:
            reasons.append(f'{path} is empty')
    return '; '.join(reasons) or 'no library directory is configured (MOVIES_DIR, TV_DIR)'


def names_under(downloads_dir):
    """Every file and directory name in the Downloads directory, at any depth.

    Raises OSError when any directory cannot be listed: a name missing from a
    partial listing proves nothing.
    """
    def unlistable(error):
        raise error

    names = set()
    for _root, dirs, files in os.walk(downloads_dir, onerror=unlistable):
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
