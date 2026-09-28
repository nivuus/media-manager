"""Whether a download queue that was read can protect the Downloads purge.

Radarr/Sonarr keep the queue in memory and replace it whole each time they
refresh it from the download clients. It is empty at startup. A client whose
listing fails contributes no row, and neither does one the provider back-off
has blocked, since the refresh skips it. GET /queue succeeds all the same,
and the purge acts on absence: it deletes the old files no queue lists. A
queue the clients did not demonstrably feed would let it delete a completed,
unimported download (audit C2, through a read that succeeded). The timer is
Persistent=true, so a missed 06:00 run fires at boot, when the queues are
cold.

A queue is trusted when every step passes for its instance: the clients
check, a queue refresh the run waits for, the read itself, and the clients
check again. The clients check needs both halves: testall passing for a
non-empty list of clients (it ignores the back-off, so it cannot see a
blocked client), and health reporting no blocked client (it misses a client
inside its first-failure grace period, which testall catches).
"""
import time

from maintenance.arr_api import ApiError, call_json, get_json, get_json_list, object_list
from maintenance.queue_actions import fetch_queue

# The health source of a download client blocked by the provider back-off:
# the check's class name, which is never localised (its message is).
BLOCKED_CLIENT_SOURCE = 'DownloadClientStatusCheck'

REFRESH_COMMAND = 'RefreshMonitoredDownloads'
REFRESH_TIMEOUT_SECONDS = 120
REFRESH_POLL_SECONDS = 2
# Radarr/Sonarr's CommandStatus values, serialised in camelCase.
RUNNING_STATUSES = ('queued', 'started')
FAILED_STATUSES = ('failed', 'aborted', 'cancelled', 'orphaned')


def clients_problem(instance):
    """Why the instance's download clients cannot vouch for its queue; None when they can."""
    try:
        results = object_list(call_json(instance, 'POST', 'downloadclient/testall'),
                              'the answer to POST /api/v3/downloadclient/testall')
    except ApiError as error:
        return f'download client test failed ({error})'
    if not results:
        # A disabled client may still hold completed, unimported downloads.
        return 'no enabled download client feeds the queue'
    failing = [result.get('id') for result in results if result.get('isValid') is not True]
    if failing:
        return f'download client test failed for client id(s) {failing}'
    try:
        health = get_json_list(instance, 'health')
    except ApiError as error:
        return f'health unreadable ({error})'
    for item in health:
        if item.get('source') == BLOCKED_CLIENT_SOURCE:
            return f"download client blocked ({BLOCKED_CLIENT_SOURCE}: {item.get('message')})"
    return None


def refresh(instance):
    """Have the instance rebuild its queue from its clients, and wait until it has.

    Raises ApiError when the refresh cannot be started, ends in any status
    but completed, or has not completed REFRESH_TIMEOUT_SECONDS after it was
    asked for.
    """
    asked = time.monotonic()
    created = call_json(instance, 'POST', 'command', payload={'name': REFRESH_COMMAND})
    command_id = created.get('id') if isinstance(created, dict) else None
    if not isinstance(command_id, int):
        raise ApiError(f'the {REFRESH_COMMAND} command was not given an id')
    while True:
        command = get_json(instance, f'command/{command_id}')
        status = command.get('status') if isinstance(command, dict) else None
        if status == 'completed':
            return
        if status in FAILED_STATUSES:
            raise ApiError(f'the {REFRESH_COMMAND} command ended {status}')
        if status not in RUNNING_STATUSES:
            raise ApiError(f'the {REFRESH_COMMAND} command reports an unknown status {status!r}')
        if time.monotonic() - asked >= REFRESH_TIMEOUT_SECONDS:
            raise ApiError(f'the {REFRESH_COMMAND} command has not completed '
                           f'within {REFRESH_TIMEOUT_SECONDS} s')
        time.sleep(REFRESH_POLL_SECONDS)


def read_queue(instance):
    """Read one instance's queue, and say whether it can protect the purge.

    Returns (records, doubt): the rows, which are real either way, and why
    their absence proves nothing, or None when the queue is trusted. When
    the first clients check fails there is no point refreshing, but the rows
    are still read for processing. Raises ApiError when the queue itself
    cannot be read.
    """
    doubt = clients_problem(instance)
    if doubt is None:
        try:
            refresh(instance)
        except ApiError as error:
            doubt = f'queue refresh failed ({error})'
    records = fetch_queue(instance)
    if doubt is None:
        problem = clients_problem(instance)
        if problem is not None:
            doubt = f'after the queue read, {problem}'
    return records, doubt
