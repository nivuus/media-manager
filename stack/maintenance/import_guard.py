"""When a manual import may be attempted: twice seen pending, and nothing else importing.

Two checks, both stateless, made within one run, before any ManualImport:

1. CONFIRMATION. importPending is often transient: Radarr/Sonarr's own
   completed-download handling (ProcessMonitoredDownloads, every minute)
   imports most downloads a few moments after they finish. A row is only
   imported by this script when a second read of the queue, made
   CONFIRM_DELAY_SECONDS after the first, still shows it import-pending.

2. IDLE. A manual import started while the app is itself importing or
   scanning the same download reads files that are being moved. The
   instance's command list is polled until no import-related command is
   queued or started, for at most IDLE_TIMEOUT_SECONDS; still busy then,
   its imports wait for the next run. Commands are matched on their `name`,
   which is an identifier, never on any message text.

Neither check limits how many times one download is tried. That bound is
the age rule (queue_policy.STUCK_MAX_AGE_HOURS): a row still import-pending
72 h after it was added is removed and blocklisted, so a daily run tries it
at most three times. The apps keep no record of a ManualImport that imported
nothing (no history event, and the command list is in memory and short), so
counting attempts would need a state file, which these scripts avoid.
"""
import logging
import time

from maintenance.arr_api import ApiError, get_json_list
from maintenance.queue_actions import fetch_queue
from maintenance.queue_policy import IMPORT_PENDING_STATES

log = logging.getLogger(__name__)

CONFIRM_DELAY_SECONDS = 120
IDLE_TIMEOUT_SECONDS = 300
IDLE_POLL_SECONDS = 10
# Commands that import, or scan downloads for import, on Radarr or Sonarr.
IMPORT_COMMANDS = frozenset({
    'ManualImport', 'ProcessMonitoredDownloads', 'RefreshMonitoredDownloads',
    'DownloadedMoviesScan', 'DownloadedEpisodesScan',
})
ACTIVE_STATUSES = ('queued', 'started')


def pending_key(item):
    return (item.get('id'), item.get('downloadId'))


def confirmed_pending(instance):
    """The (id, downloadId) of the rows import-pending in a fresh read of the queue.

    Raises ApiError when the queue cannot be read again: without the second
    observation, no row of this instance is confirmed.
    """
    return {pending_key(item) for item in fetch_queue(instance)
            if item.get('trackedDownloadState') in IMPORT_PENDING_STATES}


def busy_commands(instance):
    """The import-related commands queued or started on the instance; ApiError if unreadable."""
    return sorted({command.get('name') for command in get_json_list(instance, 'command')
                   if command.get('name') in IMPORT_COMMANDS
                   and command.get('status') in ACTIVE_STATUSES})


def wait_idle(instance):
    """Whether the instance stopped importing within IDLE_TIMEOUT_SECONDS.

    Raises ApiError when the command list cannot be read.
    """
    started = time.monotonic()
    while True:
        busy = busy_commands(instance)
        if not busy:
            return True
        if time.monotonic() - started >= IDLE_TIMEOUT_SECONDS:
            log.warning('[%s] still importing after %d s (%s): its manual imports '
                        'wait for the next run', instance, IDLE_TIMEOUT_SECONDS,
                        ', '.join(busy))
            return False
        time.sleep(IDLE_POLL_SECONDS)


def import_clearance(pending_instances, failures):
    """For each instance with import-pending rows: the rows cleared for a manual import.

    `pending_instances` lists the instances whose first read had such rows.
    Waits CONFIRM_DELAY_SECONDS once for all of them, then, per instance,
    reads the queue again and waits until it is idle. Returns
    {instance: set of (id, downloadId)}; an instance that cannot be checked
    gets an empty set, with the failure recorded, and an instance still busy
    gets an empty set too: its rows are kept, never removed for that.
    """
    clearance = {}
    if not pending_instances:
        return clearance
    log.info('Import-pending rows found: confirming them in %d s', CONFIRM_DELAY_SECONDS)
    time.sleep(CONFIRM_DELAY_SECONDS)
    for instance in pending_instances:
        try:
            confirmed = confirmed_pending(instance)
            idle = wait_idle(instance)
        except ApiError as error:
            failures.record(f'[{instance}] cannot check whether imports may run', error)
            clearance[instance] = set()
            continue
        clearance[instance] = confirmed if idle else set()
    return clearance
