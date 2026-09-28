"""What to do with one row of a Radarr/Sonarr download queue.

Pure decisions: a queue row, the current time and whether the row's download
is on disk go in, an action comes out. No I/O here; the caller establishes
presence from the Downloads directory, and queue_actions.py and
reset_error.py carry the actions out.
"""
from maintenance.arr_api import parse_time

# Queue items older than this that still have not imported are considered dead
# in the download client (Alldebrid serves over HTTPS: nothing legitimate takes
# days). Seen: items stuck 9 months with status=warning/trackedDownloadStatus=ok.
STUCK_MAX_AGE_HOURS = 72

# Local/recoverable problems: the download is fine, the issue is on OUR side
# (permissions, path mount). These must NOT be removed/blocklisted — fixing the
# permissions and letting Radarr/Sonarr retry the import resolves them.
# (Learned the hard way: a root-owned library folder made imports fail with
#  "Permission denied"; auto-removing those downloads just wasted the grab.)
# Only a download whose files are gone from disk is removed despite them, and
# then without a blocklist.
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
# throw away perfectly good grabs every time rdtclient restarts. The same holds
# for the age rule: such a release waits for the client, it is not stuck.
CLIENT_UNAVAILABLE_STATUS = 'downloadClientUnavailable'

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
    added = parse_time(item.get('added'))
    if added is None:
        return None
    return (now - added).total_seconds() / 3600


def classify_item(item, now, download_present=None):
    """
    Decide what to do with a queue item.

    `download_present` says whether the row's download is still on disk: True,
    False, or None when that could not be established (no outputPath, or the
    Downloads directory could not be listed). Only import-pending rows are
    judged on it; None concludes nothing.

    Returns one of:
      'keep'                   -> leave it alone (still working, or transient)
      'skip_recover'           -> a LOCAL/recoverable problem (permissions,
                                  path); do NOT remove — fixing it + a retry
                                  will import it
      'try_import'             -> download finished but not imported; the
                                  bytes are fine, only the matching failed ->
                                  manual-import it
      'try_import_recoverable' -> the same while a local problem is reported:
                                  manual-import it, and never remove it by age
      'remove_files_gone'      -> the download's files are gone: remove the
                                  row, do NOT blocklist the release
      'remove_blocklist'       -> bad release, remove AND blocklist so a
                                  different release is grabbed instead of
                                  re-grabbing this one
    """
    texts = list(item_texts(item))
    local_problem = any(kw in t for t in texts for kw in LOCAL_RECOVERABLE_KEYWORDS)
    bad_release = any(kw in t for t in texts for kw in BAD_RELEASE_KEYWORDS)
    status = item.get('status')
    tracked_status = item.get('trackedDownloadStatus')
    tracked_state = item.get('trackedDownloadState')
    age = item_age_hours(item, now)

    # 1) Download finished, import did not happen. Judged before the keyword
    #    rules, which would otherwise pre-empt the manual import.
    if tracked_state in IMPORT_PENDING_STATES:
        # a) Its files are gone from Downloads: the release was fine, only the
        #    files vanished, so remove the row without blocklisting, whatever
        #    the messages or the age say. Decided from the filesystem, never
        #    from message text: 'no files found' also means unimportable
        #    content, and 'path does not exist' also means a path-mapping fault
        #    with the files intact.
        if download_present is False:
            return 'remove_files_gone'
        # b) A local problem (permissions, path mapping): keep attempting the
        #    import, and never remove the row by age — fixing the cause is what
        #    gets it imported.
        if local_problem:
            return 'try_import_recoverable'
        # c) The content itself cannot be imported: remove and blocklist.
        if bad_release:
            return 'remove_blocklist'
        # d) The release is on disk and healthy. Import it rather than throwing
        #    it away — deleting here is what created the grab/delete/re-grab
        #    loop. Only give up once it has resisted for STUCK_MAX_AGE_HOURS,
        #    and then blocklist so the next search picks a DIFFERENT release
        #    instead of the same unimportable one.
        if age is not None and age > STUCK_MAX_AGE_HOURS:
            return 'remove_blocklist'
        return 'try_import'

    # 2) Local, recoverable issue on our side -> never auto-remove.
    if local_problem:
        return 'skip_recover'

    # 3) The release itself is unusable -> remove and blocklist.
    if bad_release:
        return 'remove_blocklist'

    # 4) The download client reports a failure on this grab. Whatever the exact
    #    wording (it comes from the client, not from Radarr/Sonarr, so the
    #    keyword rules cannot enumerate it), nothing is being downloaded.
    #    Blocklist so the next search picks a different release.
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

    # 5) A failed download, as the APIs actually report it: status 'failed'
    #    from the download client, trackedDownloadState 'failed' once Radarr/
    #    Sonarr processed it. After rule 4, so a failed row that carries an
    #    error message keeps its grace.
    if status == 'failed' or tracked_state == 'failed':
        return 'remove_blocklist'

    # 6) Warnings without a clearer signal. Blocklist as well: a warning on a
    #    grab we are about to drop means this release did not work out, and an
    #    unblocklisted removal invites Radarr/Sonarr to grab it right back.
    if tracked_status == 'warning':
        return 'remove_blocklist'

    # 7) Anything sitting in the queue for days without importing is dead on
    #    the download-client side, whatever its status flags say ('warning/ok/
    #    downloading' items evaded rules 1-6 for months). Blocklist so the next
    #    search grabs a different release. Not a release that is only waiting
    #    for an unreachable client: that one is fine, and blocklisting it
    #    would throw it away.
    if (age is not None and age > STUCK_MAX_AGE_HOURS
            and status != CLIENT_UNAVAILABLE_STATUS):
        return 'remove_blocklist'

    return 'keep'
