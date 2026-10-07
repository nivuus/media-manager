"""The Downloads quarantine: where the 24-hour purge puts files instead of deleting them.

A file the purge judges abandoned is renamed into
DOWNLOADS_DIR/.quarantine/<batch>/<its path under Downloads>, where <batch>
is the UTC time of the run. It is deleted only once its batch is older than
QUARANTINE_HOURS. A wrong verdict of the purge (a queue that missed a
download despite every trust gate) is then a file to move back, not a lost
download. To restore one, move it back to the same path under Downloads.

The move is a rename inside the Downloads filesystem: atomic, and the space
is not used twice. A file whose rename would cross a mount point (EXDEV) is
left where it is and the failure recorded: copying it would double the space
the purge exists to free, and deleting it would defeat the quarantine.
"""
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

QUARANTINE_DIRNAME = '.quarantine'
QUARANTINE_HOURS = 72
BATCH_FORMAT = '%Y%m%dT%H%M%SZ'


def quarantine_root(downloads_dir):
    return os.path.join(downloads_dir, QUARANTINE_DIRNAME)


def batch_name(now):
    """The batch directory of a run started at `now` (aware UTC)."""
    return now.astimezone(timezone.utc).strftime(BATCH_FORMAT)


def batch_time(name):
    """When a batch was made, from its name; None for a name this module did not write."""
    try:
        return datetime.strptime(name, BATCH_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def move_in(downloads_dir, file_path, batch):
    """Rename one file under Downloads into the quarantine batch, keeping its relative path.

    Raises OSError, EXDEV included, when it cannot be renamed: the file is
    then still where it was.
    """
    rel = os.path.relpath(file_path, downloads_dir)
    target = os.path.join(quarantine_root(downloads_dir), batch, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.rename(file_path, target)
    return target


def expire(downloads_dir, now, failures):
    """Delete the quarantine batches older than QUARANTINE_HOURS.

    A directory in the quarantine whose name is not a batch time is left
    alone, with a warning: something this module did not write is not its
    to delete.
    """
    root = quarantine_root(downloads_dir)
    try:
        entries = sorted(os.listdir(root))
    except FileNotFoundError:
        return
    except OSError as error:
        failures.record(f'Quarantine: cannot list {root}', error)
        return
    cutoff = now - timedelta(hours=QUARANTINE_HOURS)
    for name in entries:
        path = os.path.join(root, name)
        made = batch_time(name)
        if made is None or os.path.islink(path) or not os.path.isdir(path):
            log.warning('Quarantine: left alone, not a batch of this script: %s', path)
            continue
        if made > cutoff:
            continue

        def unremovable(_function, failed_path, error):
            failures.record(f'Quarantine: cannot delete {failed_path}', error)

        shutil.rmtree(path, onexc=unremovable)
        if not os.path.exists(path):
            log.info('Quarantine: deleted batch %s (older than %dh)', name, QUARANTINE_HOURS)
