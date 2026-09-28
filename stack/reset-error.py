import os
import sys
import time
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configuration
DOWNLOADS_DIR = os.environ.get('DOWNLOADS_DIR', '/media/data/Downloads')
MAX_FILE_AGE_HOURS = 24
# Queue items older than this that still have not imported are considered dead
# in the download client (Alldebrid serves over HTTPS: nothing legitimate takes
# days). Seen: items stuck 9 months with status=warning/trackedDownloadStatus=ok.
STUCK_MAX_AGE_HOURS = 72
REQUIRED_SUBDIRS = ['tv-sonarr', 'radarr']
PUID = int(os.environ.get('PUID', 1000))
PGID = int(os.environ.get('PGID', 1000))

# Une seule instance par type depuis la fusion des instances 4K (2026-08-18).
# Les listes restent des listes : la boucle d'appel est inchangée.
RADARR_URL = os.environ.get('RADARR_URL', 'http://localhost:7878')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY', '')

RADARR_INSTANCES = [
    {'url': f'{RADARR_URL}/api/v3', 'api_key': RADARR_API_KEY}
]

SONARR_URL = os.environ.get('SONARR_URL', 'http://localhost:8989')
SONARR_API_KEY = os.environ.get('SONARR_API_KEY', '')

SONARR_INSTANCES = [
    {'url': f'{SONARR_URL}/api/v3', 'api_key': SONARR_API_KEY}
]

# Local/recoverable problems: the download is fine, the issue is on OUR side
# (permissions, path mount). These must NOT be removed/blocklisted — fixing the
# permissions and letting Radarr/Sonarr retry the import resolves them.
# (Learned the hard way: a root-owned library folder made imports fail with
#  "Permission denied"; auto-removing those downloads just wasted the grab.)
LOCAL_RECOVERABLE_KEYWORDS = [
    'permission denied',
    'access to the path',
    'access is denied',
    'is denied',
    'path does not exist',
]

# Statuses the download client itself raises against one specific grab.
# Radarr/Sonarr expose them on `status`, NOT on `trackedDownloadStatus`, which
# happily stays 'ok'. A real row measured on 2026-09-16:
#   status=warning / trackedDownloadStatus=ok / trackedDownloadState=downloading
#   errorMessage="qBittorrent is reporting an error"   (zero byte on disk)
# Rule 6 only ever tested trackedDownloadStatus, so those rows were invisible to
# it and only died of old age, three days later.
CLIENT_ERROR_STATUSES = ('warning', 'failed')
# 'downloadClientUnavailable' is deliberately NOT in that list: it says OUR
# client is unreachable, not that the release is bad. Blocklisting on it would
# throw away perfectly good grabs every time rdtclient restarts.

# A client error can be a blip — the debrid provider answering 502 mid-transfer
# is a routine event in the rdtclient log. Give it a few hours to clear before
# writing the release off; still twelve times faster than STUCK_MAX_AGE_HOURS,
# which is what used to pick these up.
CLIENT_ERROR_GRACE_HOURS = 6

# A finished download waiting for a manual import is NOT a failure: the bytes
# are on disk and only the matching step is missing (release name Radarr/Sonarr
# can't parse, season pack in a single file, "matched by ID" safety guard...).
# Deleting those used to re-open the exact same grab on the next cycle, forever:
# measured 64 re-grabs of one season pack, 40 of another, which is what burned
# the indexer API quotas. Try the import instead, blocklist only if it fails.
IMPORT_PENDING_STATES = ('importPending', 'importBlocked')

# Rejections that mean "importing this file would be wrong", not "we failed to
# identify it". Re-grabbing the same release cannot help, so blocklist it.
UNIMPORTABLE_REJECTIONS = [
    'not an upgrade',
    'not a sample',
    'sample',
]

# Genuine bad-release problems: the release itself is unusable. Remove it AND
# blocklist so Radarr/Sonarr grabs a DIFFERENT release instead of re-grabbing
# the same broken one on the next cycle (that infinite loop was caused by
# deleting without blocklisting).
BAD_RELEASE_KEYWORDS = [
    'has been removed',
    'missing from disk',
    'file not found',
    'no files found',
    'unpack failed',
    'unpacking failed',
    'corrupt',
]


def check_api_keys():
    """Verify that required API keys are configured."""
    missing = []
    if not RADARR_API_KEY:
        missing.append('RADARR_API_KEY')
    if not SONARR_API_KEY:
        missing.append('SONARR_API_KEY')
    if missing:
        print(f"Error: Missing API keys in environment: {', '.join(missing)}")
        print("Configure them in your .env file or as environment variables.")
        sys.exit(1)


def get_active_download_names():
    """Collect the download folder/file names still referenced by any queue.

    A file whose name matches an active queue entry must never be purged, even
    if it is older than 24h (large downloads can legitimately take longer).
    """
    active = set()
    for instance, label in (
        [(i, 'Radarr') for i in RADARR_INSTANCES]
        + [(i, 'Sonarr') for i in SONARR_INSTANCES]
    ):
        for item in fetch_queue(instance, label):
            for key in ('outputPath', 'title', 'downloadId'):
                val = item.get(key)
                if val:
                    active.add(os.path.basename(str(val).rstrip('/')))
                    active.add(str(val))
    return active


def cleanup_downloads_directory(protected_names=None):
    """Supprime les fichiers de plus de 24h dans le dossier Downloads.

    Les fichiers/dossiers encore reference par une queue active (protected_names)
    sont conserves, quelle que soit leur anciennete.
    """
    protected_names = protected_names or set()
    current_time = time.time()
    max_age_seconds = MAX_FILE_AGE_HOURS * 3600

    for root, dirs, files in os.walk(DOWNLOADS_DIR):
        # Ne pas descendre dans un dossier de telechargement encore actif.
        dirs[:] = [d for d in dirs if d not in protected_names]
        for file in files:
            file_path = os.path.join(root, file)
            if file in protected_names or os.path.basename(root) in protected_names:
                continue
            try:
                file_age = current_time - os.path.getmtime(file_path)
                if file_age > max_age_seconds:
                    os.remove(file_path)
                    print(f"[Cleanup] Suppression (>{MAX_FILE_AGE_HOURS}h): {file_path}")
            except Exception as e:
                print(f"Erreur lors de la suppression du fichier {file_path}: {e}")

    # Suppression des dossiers vides
    for root, dirs, files in os.walk(DOWNLOADS_DIR, topdown=False):
        for dir in dirs:
            dir_path = os.path.join(root, dir)
            try:
                os.rmdir(dir_path)
            except OSError:
                pass

    # Recreer les sous-dossiers requis par Sonarr/Radarr
    for subdir in REQUIRED_SUBDIRS:
        subdir_path = os.path.join(DOWNLOADS_DIR, subdir)
        os.makedirs(subdir_path, exist_ok=True)
        os.chown(subdir_path, PUID, PGID)


def _iter_item_texts(item):
    """Yield every lowercased error/status string attached to a queue item."""
    yield (item.get('errorMessage') or '').lower()
    for msg_obj in item.get('statusMessages', []):
        yield (msg_obj.get('title') or '').lower()
        for message in msg_obj.get('messages', []):
            yield (message or '').lower()


def _item_age_hours(item):
    """Age of a queue item in hours, or None if the 'added' date is unusable."""
    added = item.get('added')
    if not added:
        return None
    try:
        added_dt = datetime.fromisoformat(str(added).replace('Z', '+00:00'))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - added_dt).total_seconds() / 3600


def classify_item(item):
    """
    Decide what to do with a queue item.

    Returns one of:
      'keep'             -> leave it alone (still working, or transient)
      'skip_recover'     -> a LOCAL/recoverable problem (permissions, path);
                            do NOT remove — fixing perms + retry will import it
      'try_import'       -> download finished but not imported; the bytes are
                            fine, only the matching failed -> manual-import it
      'remove'           -> dead entry, remove but do not blocklist
      'remove_blocklist' -> bad release, remove AND blocklist so a different
                            release is grabbed instead of re-grabbing this one
    """
    texts = list(_iter_item_texts(item))

    # 1) Local, recoverable issue on our side -> never auto-remove.
    if any(kw in t for t in texts for kw in LOCAL_RECOVERABLE_KEYWORDS):
        return 'skip_recover'

    # 2) The release itself is unusable -> remove and blocklist.
    if any(kw in t for t in texts for kw in BAD_RELEASE_KEYWORDS):
        return 'remove_blocklist'

    status = item.get('status')
    tracked_status = item.get('trackedDownloadStatus')
    tracked_state = item.get('trackedDownloadState')

    # 3) Hard download-client error / failed import with no local-error hint ->
    #    treat the release as bad and blocklist it (breaks the re-grab loop).
    if status == 'error' or tracked_state == 'importFailed':
        return 'remove_blocklist'

    age = _item_age_hours(item)

    # 4) Download finished, import did not happen: the release is on disk and
    #    healthy. Import it rather than throwing it away — deleting here is what
    #    created the grab/delete/re-grab loop. Only give up once it has resisted
    #    for STUCK_MAX_AGE_HOURS, and then blocklist so the next search picks a
    #    DIFFERENT release instead of the same unimportable one.
    if tracked_state in IMPORT_PENDING_STATES:
        if age is not None and age > STUCK_MAX_AGE_HOURS:
            return 'remove_blocklist'
        return 'try_import'

    # 5) The download client reports a failure on this grab. Whatever the exact
    #    wording (it comes from the client, not from Radarr/Sonarr, so rules 1-2
    #    cannot enumerate it), nothing is being downloaded. Blocklist so the next
    #    search picks a different release.
    #
    #    age is None means the row carries no usable 'added' date. Every rule
    #    below needs one to fire, so keeping such a row means keeping it forever
    #    — measured: one row stuck in 'downloading' with no date, invisible to
    #    every rule. Here the error message is the evidence the date would only
    #    have confirmed, so act on it straight away.
    if status in CLIENT_ERROR_STATUSES and item.get('errorMessage'):
        if age is None or age > CLIENT_ERROR_GRACE_HOURS:
            return 'remove_blocklist'
        return 'keep'

    # 6) Warnings without a clearer signal. Blocklist as well: a warning on a
    #    grab we are about to drop means this release did not work out, and an
    #    unblocklisted removal invites Radarr/Sonarr to grab it right back.
    if tracked_status == 'warning':
        return 'remove_blocklist'

    # 7) Anything sitting in the queue for days without importing is dead on
    #    the download-client side, whatever its status flags say ('warning/ok/
    #    downloading' items evaded rules 1-6 for months). Blocklist so the next
    #    search grabs a different release.
    if age is not None and age > STUCK_MAX_AGE_HOURS:
        return 'remove_blocklist'

    return 'keep'


def fetch_queue(instance, label):
    """Fetch all queue records from a Radarr/Sonarr instance.

    'Unknown' items — downloads Radarr/Sonarr could not attach to a movie or a
    series, which show up in the UI under their raw torrent hash — are excluded
    by default by the API. They are exactly the ones that get stuck forever, so
    they must be asked for explicitly or the cleanup never sees them.
    """
    params = {'page': 1, 'pageSize': 1000}
    if label == 'Radarr':
        params['includeUnknownMovieItems'] = True
    else:
        params['includeUnknownSeriesItems'] = True
    try:
        response = requests.get(
            f"{instance['url']}/queue",
            headers={'X-Api-Key': instance['api_key']},
            params=params
        )
        response.raise_for_status()
        queue = response.json()

        records = queue.get('records', [])
        if not isinstance(records, list):
            print(f"[{label} {instance['url']}] La cle 'records' n'est pas une liste : {queue}")
            return []
        return records
    except requests.exceptions.RequestException as e:
        print(f"Erreur lors de la connexion a {label} {instance['url']}: {e}")
        return []


def delete_queue_item(instance, item_id, blocklist=False):
    """Delete a single queue item.

    blocklist=True tells Radarr/Sonarr to blocklist the release so it grabs a
    different one next time instead of re-grabbing the same broken release.
    """
    del_response = requests.delete(
        f"{instance['url']}/queue/{item_id}",
        headers={'X-Api-Key': instance['api_key']},
        params={'removeFromClient': True, 'blocklist': blocklist}
    )
    del_response.raise_for_status()


def _blocking_rejection(candidate):
    """Return the rejection that makes a candidate not worth importing, if any."""
    for rejection in candidate.get('rejections') or []:
        reason = rejection.get('reason') if isinstance(rejection, dict) else rejection
        reason = (reason or '').lower()
        if any(kw in reason for kw in UNIMPORTABLE_REJECTIONS):
            return reason
    return None


def build_import_file(candidate, label):
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

    if label == 'Radarr':
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


def try_manual_import(instance, item, label):
    """Import a finished-but-unimported download. Returns True if queued.

    This is the automated equivalent of the "Manual Import" button: it asks the
    API which files the download holds and how they were matched, then imports
    the ones that are unambiguously identified.
    """
    download_id = item.get('downloadId')
    if not download_id:
        return False

    headers = {'X-Api-Key': instance['api_key']}
    try:
        response = requests.get(
            f"{instance['url']}/manualimport",
            headers=headers,
            params={'downloadId': download_id, 'filterExistingFiles': True},
            timeout=120,
        )
        response.raise_for_status()
        candidates = response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"[{label} {instance['url']}] Lecture manualimport impossible ({download_id}): {e}")
        return False

    files = [f for f in (build_import_file(c, label) for c in candidates) if f]
    if not files:
        return False

    try:
        requests.post(
            f"{instance['url']}/command",
            headers=headers,
            json={'name': 'ManualImport', 'importMode': 'auto', 'files': files},
            timeout=120,
        ).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"[{label} {instance['url']}] Import manuel refuse ({download_id}): {e}")
        return False

    return True


def format_item_description(item):
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


def process_error_downloads(instances, label):
    """Classify and act on every queue item across the given instances."""
    for instance in instances:
        records = fetch_queue(instance, label)
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
            action = classify_item(item)
            if action == 'keep':
                continue

            desc = format_item_description(item)

            if action == 'skip_recover':
                # Local/recoverable problem (e.g. permissions): keep the download,
                # just flag it. Fixing the cause + a rescan will import it.
                recoverable += 1
                print(f"[{label} {instance['url']}] Conserve (probleme local recuperable) : {desc}")
                continue

            if action == 'try_import':
                if try_manual_import(instance, item, label):
                    imported += 1
                    print(f"[{label} {instance['url']}] Import manuel declenche : {desc}")
                    if download_id:
                        handled_download_ids.add(download_id)
                    continue
                if _item_age_hours(item) is not None:
                    # Datable: give it until STUCK_MAX_AGE_HOURS to become
                    # importable (the download client may still be moving files).
                    awaiting += 1
                    print(f"[{label} {instance['url']}] Import impossible pour l'instant, conserve : {desc}")
                    continue
                # No 'added' date AND nothing to import: a ghost row pointing at
                # content the download client no longer has. Keeping it would be
                # forever, since every age-based rule needs a date to fire.
                action = 'remove_blocklist'

            blocklist = action == 'remove_blocklist'
            suffix = ' + blocklist' if blocklist else ''
            print(f"[{label} {instance['url']}] Suppression{suffix} de : {desc}")
            try:
                delete_queue_item(instance, item['id'], blocklist=blocklist)
                removed += 1
                if download_id:
                    handled_download_ids.add(download_id)
            except requests.exceptions.RequestException as e:
                print(f"[{label} {instance['url']}] Erreur suppression {item['id']}: {e}")

        msg = f"[{label} {instance['url']}] {removed}/{len(records)} items supprimes"
        details = []
        if imported:
            details.append(f"{imported} imports manuels declenches")
        if awaiting:
            details.append(f"{awaiting} en attente d'identification")
        if recoverable:
            details.append(f"{recoverable} conserves pour retry apres correction locale")
        if details:
            msg += f" ({', '.join(details)})"
        print(msg)


def cleanup_deleted_metadata():
    """Remove entries whose metadata was deleted upstream (TMDb/TheTVDB).

    Radarr/Sonarr mark them with status='deleted' and spam refresh errors
    forever. Entries WITH files on disk are only logged, never removed here:
    deciding what to do with orphaned media stays a human call.
    """
    for instance in RADARR_INSTANCES:
        try:
            response = requests.get(
                f"{instance['url']}/movie",
                headers={'X-Api-Key': instance['api_key']})
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"[Radarr {instance['url']}] Erreur lecture films: {e}")
            continue
        for movie in response.json():
            if movie.get('status') != 'deleted':
                continue
            desc = f"{movie.get('title')} (tmdb {movie.get('tmdbId')})"
            if movie.get('hasFile'):
                print(f"[Radarr {instance['url']}] Metadata supprimee de TMDb mais fichier present, conserve : {desc}")
                continue
            try:
                requests.delete(
                    f"{instance['url']}/movie/{movie['id']}",
                    headers={'X-Api-Key': instance['api_key']},
                    params={'deleteFiles': False, 'addImportExclusion': False}
                ).raise_for_status()
                print(f"[Radarr {instance['url']}] Entree morte TMDb supprimee : {desc}")
            except requests.exceptions.RequestException as e:
                print(f"[Radarr {instance['url']}] Erreur suppression {desc}: {e}")

    for instance in SONARR_INSTANCES:
        try:
            response = requests.get(
                f"{instance['url']}/series",
                headers={'X-Api-Key': instance['api_key']})
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"[Sonarr {instance['url']}] Erreur lecture series: {e}")
            continue
        for series in response.json():
            if series.get('status') != 'deleted':
                continue
            desc = f"{series.get('title')} (tvdb {series.get('tvdbId')})"
            if series.get('statistics', {}).get('sizeOnDisk', 0) > 0:
                print(f"[Sonarr {instance['url']}] Metadata supprimee de TheTVDB mais fichiers presents, conserve : {desc}")
                continue
            try:
                requests.delete(
                    f"{instance['url']}/series/{series['id']}",
                    headers={'X-Api-Key': instance['api_key']},
                    params={'deleteFiles': False}
                ).raise_for_status()
                print(f"[Sonarr {instance['url']}] Entree morte TheTVDB supprimee : {desc}")
            except requests.exceptions.RequestException as e:
                print(f"[Sonarr {instance['url']}] Erreur suppression {desc}: {e}")


if __name__ == "__main__":
    check_api_keys()
    # Protect files still tied to an active download before purging by age.
    cleanup_downloads_directory(protected_names=get_active_download_names())
    process_error_downloads(RADARR_INSTANCES, 'Radarr')
    process_error_downloads(SONARR_INSTANCES, 'Sonarr')
    cleanup_deleted_metadata()
