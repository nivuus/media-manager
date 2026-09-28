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
check again. The clients check needs both halves. testall must pass for a
non-empty list of clients: it tests every enabled client there and then,
but ignores the back-off, so it cannot see a blocked one. Health must report
neither DownloadClientStatusCheck, clients the back-off blocked (it misses a
client inside its first-failure grace period), nor DownloadClientCheck, a
client whose listing fails or no client at all (it reruns on every client
failure, grace period or not).

Health reruns those checks 5 s after the event that triggers them, so the
second health read can predate a failure during this run's own refresh; the
second testall still sees a client that is down by then.
"""
import time
from http import HTTPStatus

from maintenance.arr_api import ApiError, call_json, get_json, get_json_list, object_list
from maintenance.queue_actions import fetch_queue

# The health sources saying a download client cannot feed the queue: the
# checks' class names, which are never localised (their messages are).
# DownloadClientStatusCheck reports clients the provider back-off blocked;
# DownloadClientCheck, a client whose listing fails ("unable to communicate",
# an error) or that no client is available (a warning).
BLOCKING_SOURCES = ('DownloadClientStatusCheck', 'DownloadClientCheck')

TESTALL = 'downloadclient/testall'
REFRESH_COMMAND = 'RefreshMonitoredDownloads'
REFRESH_TIMEOUT_SECONDS = 120
REFRESH_POLL_SECONDS = 2
# Radarr/Sonarr's CommandStatus values, serialised in camelCase.
RUNNING_STATUSES = ('queued', 'started')
FAILED_STATUSES = ('failed', 'aborted', 'cancelled', 'orphaned')


def clients_problem(instance):
    """Why the instance's download clients cannot vouch for its queue; None when they can."""
    try:
        results = object_list(call_json(instance, 'POST', TESTALL),
                              f'the answer to POST /api/v3/{TESTALL}')
    except ApiError as error:
        failing = _rejected_clients(error)
        if failing:
            return f'download client test failed: {failing}'
        return f'download client test failed ({error})'
    if not results:
        # A disabled client may still hold completed, unimported downloads.
        return 'no enabled download client feeds the queue'
    failing = _failing_clients(results)
    if failing:
        return f'download client test failed: {failing}'
    try:
        health = get_json_list(instance, 'health')
    except ApiError as error:
        return f'health unreadable ({error})'
    for item in health:
        if item.get('source') in BLOCKING_SOURCES:
            return f"download client unavailable ({item['source']}: {item.get('message')})"
    return None


def _failing_clients(results):
    """Each client a list of testall results reports failing, with what its test said.

    None when the list reports none.
    """
    failing = []
    for result in results:
        if not isinstance(result, dict) or result.get('isValid') is True:
            continue
        messages = [failure['errorMessage'] for failure in result.get('validationFailures') or []
                    if isinstance(failure, dict) and failure.get('errorMessage')]
        failing.append(f"client {result.get('id')}: {', '.join(messages) or 'no message'}")
    return '; '.join(failing) or None


def _rejected_clients(error):
    """The failing clients a 400 from testall names; None when it names none.

    testall answers 400 as soon as one client fails, with the same list of
    results as its 200 in the body: that list is what says which client
    failed and why. Any other error keeps its own message.
    """
    response = error.response
    if response is None or response.status_code != HTTPStatus.BAD_REQUEST:
        return None
    try:
        results = response.json()
    except ValueError:  # not JSON: the error's own message is the reason
        return None
    return _failing_clients(results) if isinstance(results, list) else None


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
