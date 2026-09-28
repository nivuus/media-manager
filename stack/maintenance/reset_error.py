"""Daily clean-up of the Radarr/Sonarr download queues and of the Downloads directory.

Run by media-manager-reset-error.service through stack/reset-error.py, which
only calls main(): a file name with a hyphen cannot be imported, and this
module is what the tests import.

The run fails closed. Before any action it checks that the storage is
evidently there (storage_guard.py), and changes nothing at all when it is
not; and it only removes a row because its download left Downloads while
the library shows the media disk is mounted. The queues are what protects
a download from the purge, so the purge only happens when every queue was
read in full, right after a refresh, and its download clients passed their
test and were not blocked both before and after the read (queue_trust.py):
a queue lists nothing for a client that is down, blocked or not yet
refreshed. Any API or I/O failure is recorded, the run does what it still
safely can, and the exit status is 1 so that systemd marks the unit failed.
"""
import logging
from datetime import datetime, timezone

from maintenance import run_log
from maintenance.arr_api import (ApiError, Failures, exit_status,
                                 missing_api_keys, radarr_instances,
                                 sonarr_instances)
from maintenance.dead_metadata import remove_dead_entries
from maintenance.downloads_purge import (download_present, names_under,
                                         protected_names, purge)
from maintenance.queue_actions import manual_import, remove_queue_item
from maintenance.queue_policy import classify_item, item_age_hours
from maintenance.queue_trust import read_queue
from maintenance.storage_guard import library_doubt, storage_problems

log = logging.getLogger(__name__)

DEFAULT_DOWNLOADS_DIR = '/media/data/Downloads'


def describe(item):
    """Format a human-readable description of a queue item."""
    title = item.get('title', 'Unknown')
    status = item.get('status', 'unknown')
    tracked_status = item.get('trackedDownloadStatus', '')

    desc = f"{title} (status={status}"
    if tracked_status:
        desc += f", trackedStatus={tracked_status}"
    desc += ")"

    episode = item.get('episode')
    if episode:
        season = episode.get('seasonNumber', '?')
        ep_num = episode.get('episodeNumber', '?')
        desc += f" - S{season}E{ep_num}"

    return desc


def process_queue(instance, records, names, now, failures):
    """Classify and act on every row of one instance's queue.

    `names` is the Downloads listing from names_under(), or None when it could
    not be made: presence is then unknown and no row is removed as files-gone.
    """
    removed = 0
    recoverable = 0
    imported = 0
    awaiting = 0
    # A season pack spans several queue rows sharing one downloadId;
    # deleting the first row removes the download and the sibling rows 404.
    handled_download_ids = set()
    for item in records:
        download_id = item.get('downloadId')
        if download_id and download_id in handled_download_ids:
            continue
        action = classify_item(item, now,
                               download_present=download_present(item, names))
        if action == 'keep':
            continue

        desc = describe(item)

        if action == 'skip_recover':
            # Local/recoverable problem (e.g. permissions): keep the download,
            # just flag it. Fixing the cause + a rescan will import it.
            recoverable += 1
            log.info('[%s] kept, local problem to fix first: %s', instance, desc)
            continue

        if action in ('try_import', 'try_import_recoverable'):
            try:
                queued = manual_import(instance, item)
            except ApiError as error:
                # Says nothing about the download itself: keep the row for a
                # run that can actually ask.
                failures.record(f'[{instance}] manual import of {desc}', error)
                continue
            if queued:
                imported += 1
                log.info('[%s] manual import triggered: %s', instance, desc)
                if download_id:
                    handled_download_ids.add(download_id)
                continue
            if action == 'try_import_recoverable':
                # Never removed while the local problem lasts: fixing it is
                # what gets the download imported.
                recoverable += 1
                log.info('[%s] nothing importable until a local problem is fixed, '
                         'kept: %s', instance, desc)
                continue
            if item_age_hours(item, now) is not None:
                # Datable: give it until STUCK_MAX_AGE_HOURS to become
                # importable (the download client may still be moving files).
                awaiting += 1
                log.info('[%s] nothing importable yet, kept: %s', instance, desc)
                continue
            # No 'added' date AND nothing importable: a ghost row. Keeping it
            # would be forever, since every age-based rule needs a date to
            # fire; remove + blocklist it so the next search picks another
            # release. (A row whose files are gone never gets here.)
            action = 'remove_blocklist'

        if action == 'remove_files_gone':
            # The release was fine, only its files vanished: no blocklist.
            blocklist, skip_redownload = False, True
            log.info('[%s] removing, its files are gone (not blocklisted): %s',
                     instance, desc)
        elif action == 'remove_blocklist':
            blocklist, skip_redownload = True, False
            log.info('[%s] removing + blocklist: %s', instance, desc)
        else:
            raise ValueError(f'unhandled queue action {action!r}')
        try:
            remove_queue_item(instance, item['id'], blocklist=blocklist,
                              skip_redownload=skip_redownload)
        except ApiError as error:
            failures.record(f'[{instance}] cannot remove queue row {item["id"]}', error)
            continue
        removed += 1
        if download_id:
            handled_download_ids.add(download_id)

    summary = f'[{instance}] {removed}/{len(records)} queue rows removed'
    details = []
    if imported:
        details.append(f'{imported} manual imports triggered')
    if awaiting:
        details.append(f'{awaiting} awaiting identification')
    if recoverable:
        details.append(f'{recoverable} kept until a local problem is fixed')
    if details:
        summary += f" ({', '.join(details)})"
    log.info(summary)


def downloads_listing(environ, downloads_dir, failures):
    """The Downloads listing files-gone is decided on, or None when it proves nothing.

    None means presence is unknown: no row is removed as files-gone, and
    every other rule applies as usual. With the media disk missing at boot,
    Docker recreates both the library directories and DOWNLOADS_DIR empty,
    and the storage guard passes, so the listing is only made while a
    library directory holds something (storage_guard.library_doubt); a
    fresh install's empty library only delays files-gone, hence a warning.
    A directory that cannot be listed is a failure.
    """
    try:
        doubt = library_doubt(environ)
    except OSError as error:
        failures.record('Library cannot be listed, no row removed as files-gone', error)
        return None
    if doubt is not None:
        log.warning('No row removed as files-gone this run, the library holds '
                    'nothing to show the media disk is mounted: %s', doubt)
        return None
    try:
        return names_under(downloads_dir)
    except OSError as error:
        failures.record(f'Downloads directory {downloads_dir} cannot be listed', error)
        return None


def run(environ):
    """One clean-up run against the instances configured in `environ`."""
    instances = radarr_instances(environ) + sonarr_instances(environ)
    missing = missing_api_keys(instances)
    if missing:
        log.error('Missing API keys in environment: %s. Configure them in the '
                  '.env file or as environment variables.', ', '.join(missing))
        return 1

    failures = Failures()
    now = datetime.now(timezone.utc)
    downloads_dir = environ.get('DOWNLOADS_DIR', DEFAULT_DOWNLOADS_DIR)
    owner = (int(environ.get('PUID', 1000)), int(environ.get('PGID', 1000)))

    # Before any action. With storage unavailable, the apps' own messages
    # ("No files found...") are unreliable and every rule would act on a
    # broken view, so the run then changes nothing at all.
    problems = storage_problems(instances, downloads_dir)
    if problems:
        failures.record('Storage looks unavailable, nothing changed this run',
                        '; '.join(problems))
        return exit_status(failures)

    # One read per queue: the same rows protect downloads from the purge and
    # are then processed, so both steps see the same queue.
    queues = []
    distrusted = []
    for instance in instances:
        try:
            records, doubt = read_queue(instance)
        except ApiError as error:
            failures.record(f'[{instance}] queue unreadable', error)
            distrusted.append(f'queue of {instance} unreadable')
            continue
        queues.append((instance, records))
        if doubt is not None:
            failures.record(f'[{instance}] queue not trusted to protect the purge', doubt)
            distrusted.append(f'queue of {instance} not trusted, {doubt}')

    if not distrusted:
        # Protect files still tied to an active download before purging by age.
        purge(downloads_dir,
              protected_names(record for _, records in queues for record in records),
              owner, failures)
    else:
        # A queue we could not read, or one its download clients did not
        # demonstrably feed, may miss any file in Downloads: with Radarr
        # unreachable for 7 days, purging anyway deleted a completed,
        # unimported 36.8 GB download (audit C2).
        log.warning('Downloads purge skipped: %s', '; '.join(distrusted))

    # What is still on disk decides which import-pending rows lost their files.
    # Listed after the purge, so it shows what the processing will face: a
    # download folder the purge found empty, and removed, counts as gone.
    names = downloads_listing(environ, downloads_dir, failures)

    # The queues that were read are still processed, trusted or not: the rows
    # present are real, only absence is unreliable.
    for instance, records in queues:
        process_queue(instance, records, names, now, failures)
    remove_dead_entries(instances, failures)
    return exit_status(failures)


def main():
    """The unit's entry point: run_log's shared sequence, logging to reset-error.log."""
    return run_log.entry_point('reset-error', run)
