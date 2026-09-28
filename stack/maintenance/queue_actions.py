"""Reading a Radarr/Sonarr download queue, and acting on its rows.

The actions are the manual import of a finished download and the removal of
a queue row, with or without blocklisting its release.
"""
import logging

import requests

from maintenance.arr_api import call

log = logging.getLogger(__name__)

# The manual-import lookup makes Radarr/Sonarr scan the download and ffprobe
# every file in it: allow it longer than an ordinary call.
MANUAL_IMPORT_TIMEOUT = (10, 120)

# Rejections that mean "importing this file would be wrong", not "we failed to
# identify it". Re-grabbing the same release cannot help, so blocklist it.
UNIMPORTABLE_REJECTIONS = [
    'not an upgrade',
    'not a sample',
    'sample',
]


def fetch_queue(instance):
    """Fetch all queue records from a Radarr/Sonarr instance.

    'Unknown' items — downloads Radarr/Sonarr could not attach to a movie or a
    series, which show up in the UI under their raw torrent hash — are excluded
    by default by the API. They are exactly the ones that get stuck forever, so
    they must be asked for explicitly or the cleanup never sees them.
    """
    params = {'page': 1, 'pageSize': 1000}
    if instance.kind == 'radarr':
        params['includeUnknownMovieItems'] = True
    else:
        params['includeUnknownSeriesItems'] = True
    try:
        queue = call(instance, 'GET', 'queue', params=params).json()

        records = queue.get('records', [])
        if not isinstance(records, list):
            log.error("[%s] the queue's 'records' is not a list: %s", instance, queue)
            return []
        return records
    except requests.exceptions.RequestException as error:
        log.error('[%s] cannot read the queue: %s', instance, error)
        return []


def remove_queue_item(instance, item_id, blocklist=False):
    """Delete a single queue item.

    blocklist=True tells Radarr/Sonarr to blocklist the release so it grabs a
    different one next time instead of re-grabbing the same broken release.
    """
    call(instance, 'DELETE', f'queue/{item_id}',
         params={'removeFromClient': True, 'blocklist': blocklist})


def _blocking_rejection(candidate):
    """Return the rejection that makes a candidate not worth importing, if any."""
    for rejection in candidate.get('rejections') or []:
        reason = rejection.get('reason') if isinstance(rejection, dict) else rejection
        reason = (reason or '').lower()
        if any(kw in reason for kw in UNIMPORTABLE_REJECTIONS):
            return reason
    return None


def build_import_file(candidate, kind):
    """Turn a /manualimport candidate into a ManualImport command payload.

    Returns None when the candidate is not usable: either Radarr/Sonarr could
    not attach it to a movie/episode (nothing to import it *as*), or a rejection
    says importing it would be wrong.
    """
    if _blocking_rejection(candidate):
        return None

    payload = {
        'path': candidate.get('path'),
        'quality': candidate.get('quality'),
        'languages': candidate.get('languages'),
        'releaseGroup': candidate.get('releaseGroup'),
        'downloadId': candidate.get('downloadId'),
    }

    if kind == 'radarr':
        movie_id = (candidate.get('movie') or {}).get('id')
        if not movie_id:
            return None
        payload['movieId'] = movie_id
    else:
        series_id = (candidate.get('series') or {}).get('id')
        episode_ids = [e['id'] for e in candidate.get('episodes') or [] if e.get('id')]
        # No episode match means Sonarr cannot tell which episode(s) the file
        # holds — importing it would file the release under the wrong number.
        if not series_id or not episode_ids:
            return None
        payload['seriesId'] = series_id
        payload['episodeIds'] = episode_ids

    return payload if payload['path'] else None


def manual_import(instance, item):
    """Import a finished-but-unimported download. Returns True if queued.

    This is the automated equivalent of the "Manual Import" button: it asks the
    API which files the download holds and how they were matched, then imports
    the ones that are unambiguously identified.
    """
    download_id = item.get('downloadId')
    if not download_id:
        return False

    try:
        candidates = call(
            instance, 'GET', 'manualimport',
            params={'downloadId': download_id, 'filterExistingFiles': True},
            timeout=MANUAL_IMPORT_TIMEOUT,
        ).json()
    except (requests.exceptions.RequestException, ValueError) as error:
        log.error('[%s] cannot list the manual-import candidates of %s: %s',
                  instance, download_id, error)
        return False

    files = [f for f in (build_import_file(c, instance.kind) for c in candidates) if f]
    if not files:
        return False

    try:
        call(instance, 'POST', 'command',
             payload={'name': 'ManualImport', 'importMode': 'auto', 'files': files},
             timeout=MANUAL_IMPORT_TIMEOUT)
    except requests.exceptions.RequestException as error:
        log.error('[%s] manual import refused for %s: %s', instance, download_id, error)
        return False

    return True
