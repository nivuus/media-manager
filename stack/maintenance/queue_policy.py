"""What to do with one row of a Radarr/Sonarr download queue.

Pure decisions: a queue row and the current time go in, an action comes out.
No I/O here; queue_actions.py and reset_error.py carry the actions out.
"""
from datetime import datetime

# Queue items older than this that still have not imported are considered dead
# in the download client (Alldebrid serves over HTTPS: nothing legitimate takes
# days). Seen: items stuck 9 months with status=warning/trackedDownloadStatus=ok.
STUCK_MAX_AGE_HOURS = 72

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


def item_texts(item):
    """Yield every lowercased error/status string attached to a queue item."""
    yield (item.get('errorMessage') or '').lower()
    for msg_obj in item.get('statusMessages', []):
        yield (msg_obj.get('title') or '').lower()
        for message in msg_obj.get('messages', []):
            yield (message or '').lower()


def item_age_hours(item, now):
    """Age of a queue item in hours at `now`, or None if 'added' is unusable."""
    added = item.get('added')
    if not added:
        return None
    try:
        added_dt = datetime.fromisoformat(str(added).replace('Z', '+00:00'))
    except ValueError:
        return None
    return (now - added_dt).total_seconds() / 3600


def classify_item(item, now):
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
    texts = list(item_texts(item))

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

    age = item_age_hours(item, now)

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
