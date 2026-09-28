"""Daily clean-up of the Radarr/Sonarr download queues and of the Downloads directory.

Run by media-manager-reset-error.service through stack/reset-error.py, which
only calls main(): a file name with a hyphen cannot be imported, and this
module is what the tests import.
"""
import logging
import os
from datetime import datetime, timezone

import requests

from maintenance import run_log
from maintenance.arr_api import (load_environment, missing_api_keys,
                                 radarr_instances, sonarr_instances)
from maintenance.dead_metadata import remove_dead_entries
from maintenance.downloads_purge import protected_names, purge
from maintenance.queue_actions import fetch_queue, manual_import, remove_queue_item
from maintenance.queue_policy import classify_item, item_age_hours

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


def process_queue(instance, records, now):
    """Classify and act on every row of one instance's queue."""
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
        action = classify_item(item, now)
        if action == 'keep':
            continue

        desc = describe(item)

        if action == 'skip_recover':
            # Local/recoverable problem (e.g. permissions): keep the download,
            # just flag it. Fixing the cause + a rescan will import it.
            recoverable += 1
            log.info('[%s] kept, local problem to fix first: %s', instance, desc)
            continue

        if action == 'try_import':
            if manual_import(instance, item):
                imported += 1
                log.info('[%s] manual import triggered: %s', instance, desc)
                if download_id:
                    handled_download_ids.add(download_id)
                continue
            if item_age_hours(item, now) is not None:
                # Datable: give it until STUCK_MAX_AGE_HOURS to become
                # importable (the download client may still be moving files).
                awaiting += 1
                log.info('[%s] nothing importable yet, kept: %s', instance, desc)
                continue
            # No 'added' date AND nothing to import: a ghost row pointing at
            # content the download client no longer has. Keeping it would be
            # forever, since every age-based rule needs a date to fire.
            action = 'remove_blocklist'

        blocklist = action == 'remove_blocklist'
        log.info('[%s] removing%s: %s', instance, ' + blocklist' if blocklist else '', desc)
        try:
            remove_queue_item(instance, item['id'], blocklist=blocklist)
            removed += 1
            if download_id:
                handled_download_ids.add(download_id)
        except requests.exceptions.RequestException as error:
            log.error('[%s] cannot remove queue row %s: %s', instance, item['id'], error)

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


def run(environ):
    """One clean-up run against the instances configured in `environ`."""
    instances = radarr_instances(environ) + sonarr_instances(environ)
    missing = missing_api_keys(instances)
    if missing:
        log.error('Missing API keys in environment: %s. Configure them in the '
                  '.env file or as environment variables.', ', '.join(missing))
        return 1

    now = datetime.now(timezone.utc)
    downloads_dir = environ.get('DOWNLOADS_DIR', DEFAULT_DOWNLOADS_DIR)
    owner = (int(environ.get('PUID', 1000)), int(environ.get('PGID', 1000)))

    # Protect files still tied to an active download before purging by age.
    purge(downloads_dir,
          protected_names(record for instance in instances
                          for record in fetch_queue(instance)),
          owner)
    for instance in instances:
        process_queue(instance, fetch_queue(instance), now)
    remove_dead_entries(instances)
    return 0


def main():
    load_environment()
    run_log.setup()
    return run(os.environ)
