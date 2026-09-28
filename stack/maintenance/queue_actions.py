"""Reading a Radarr/Sonarr download queue, and acting on its rows.

The actions are the manual import of a finished download and the removal of
a queue row, with or without blocklisting its release. Every API failure
raises ApiError: the caller records it.
"""
from maintenance.arr_api import ApiError, call, get_json

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
    """Every row of a Radarr/Sonarr download queue.

    Raises ApiError when the queue cannot be read in full. The rows feed the
    set of downloads the purge must not touch, so a partial answer is not a
    smaller queue: it is an unknown one.

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
    queue = get_json(instance, 'queue', params=params)
    records = queue.get('records') if isinstance(queue, dict) else None
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ApiError("the queue answer has no 'records' list of rows")
    total = queue.get('totalRecords')
    if not isinstance(total, int):
        raise ApiError("the queue answer has no 'totalRecords' count")
    if total > len(records):
        raise ApiError(f'only {len(records)} of the {total} queue rows were returned')
    return records


def remove_queue_item(instance, item_id, blocklist=False, skip_redownload=False):
    """Delete a single queue item, removing its download from the client.

    blocklist=True tells Radarr/Sonarr to blocklist the release so it grabs a
    different one next time instead of re-grabbing the same broken release.
    skip_redownload=True asks them not to search for a replacement right away.
    Raises ApiError when the row could not be removed.
    """
    call(instance, 'DELETE', f'queue/{item_id}',
         params={'removeFromClient': True, 'blocklist': blocklist,
                 'skipRedownload': skip_redownload})


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
    """Import a finished-but-unimported download.

    This is the automated equivalent of the "Manual Import" button: it asks the
    API which files the download holds and how they were matched, then imports
    the ones that are unambiguously identified.

    Returns True when a ManualImport command was queued, False when the
    download holds nothing that can be imported unambiguously. Raises ApiError
    when the API could not be asked, which says nothing about the download.
    """
    download_id = item.get('downloadId')
    if not download_id:
        return False

    candidates = get_json(
        instance, 'manualimport',
        params={'downloadId': download_id, 'filterExistingFiles': True},
        timeout=MANUAL_IMPORT_TIMEOUT)
    if not isinstance(candidates, list) or not all(isinstance(c, dict) for c in candidates):
        raise ApiError('the manual-import answer is not a list of candidates')

    files = [f for f in (build_import_file(c, instance.kind) for c in candidates) if f]
    if not files:
        return False

    call(instance, 'POST', 'command',
         payload={'name': 'ManualImport', 'importMode': 'auto', 'files': files},
         timeout=MANUAL_IMPORT_TIMEOUT)
    return True
