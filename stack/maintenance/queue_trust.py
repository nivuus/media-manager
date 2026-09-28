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
non-empty list of clients: it tests every enabled client there and then
(for qBittorrent, with the same GetTorrents call the refresh makes), but
ignores the back-off, so it cannot see a blocked one. A client whose test
found only warnings passes: a remark on its configuration is logged, not
held against the queue. Health must report neither
DownloadClientStatusCheck, clients the back-off blocked (it misses a client
inside its first-failure grace period), nor DownloadClientCheck, a client
whose listing fails or no client at all (it reruns on every client failure,
grace period or not).

One failure stays invisible: one confined to the refresh's own listing
call (GetTorrents for qBittorrent), while the same call just before it, in
the first testall, and just after it, in the second, succeeds. Health reruns
its checks only 5 s after the failure is recorded, usually after the second
read; a first failure does not trip the back-off; and no API exposes a
client's failure record.
"""
import logging
import time
from http import HTTPStatus

from maintenance.arr_api import ApiError, call_json, get_json, get_json_list, object_list
from maintenance.queue_actions import fetch_queue

log = logging.getLogger(__name__)

# The health sources saying a download client cannot feed the queue: the
# checks' class names, which are never localised (their messages are).
# DownloadClientStatusCheck reports clients the provider back-off blocked;
# DownloadClientCheck, a client whose listing fails ("unable to communicate",
# an error) or that no client is available (a warning).
BLOCKING_SOURCES = ('DownloadClientStatusCheck', 'DownloadClientCheck')

TESTALL = 'downloadclient/testall'
TESTALL_ANSWER = f'the answer to POST /api/v3/{TESTALL}'
# How the JSON of a validation failure can say it is only a warning: an
# isWarning of true, or FluentValidation's Severity.Warning written as a
# camelCase string. Radarr/Sonarr (develop) write neither in testall's
# answer: its failures are serialised as the base ValidationFailure, which
# drops NzbDroneValidationFailure's isWarning, and their warnings leave
# Severity at Error. There, a warning still counts as an error.
WARNING_SEVERITY = 'warning'
REFRESH_COMMAND = 'RefreshMonitoredDownloads'
REFRESH_TIMEOUT_SECONDS = 120
REFRESH_POLL_SECONDS = 2
# Radarr/Sonarr's CommandStatus values, serialised in camelCase.
RUNNING_STATUSES = ('queued', 'started')
FAILED_STATUSES = ('failed', 'aborted', 'cancelled', 'orphaned')


def clients_problem(instance):
    """Why the instance's download clients cannot vouch for its queue; None when they can."""
    problem = _testall_problem(instance)
    if problem is not None:
        return problem
    try:
        health = get_json_list(instance, 'health')
    except ApiError as error:
        return f'health unreadable ({error})'
    for item in health:
        if item.get('source') in BLOCKING_SOURCES:
            return f"download client unavailable ({item['source']}: {item.get('message')})"
    return None


def _testall_problem(instance):
    """Why testall says a client cannot feed the queue; None when it says none.

    testall answers 400 as soon as one client has a validation failure, a
    warning included, with the same list of results as its 200. A client is
    held against the queue only for a failure that is not a warning; each
    warning is logged instead. Any other status or body fails the check with
    the error's own message, and so does a 400 that no client's failure
    explains.
    """
    try:
        results = object_list(call_json(instance, 'POST', TESTALL), TESTALL_ANSWER)
        rejection = None
    except ApiError as error:
        results = _rejected_results(error)
        if results is None:
            return f'download client test failed ({error})'
        rejection = error
    if not results and rejection is None:
        # A disabled client may still hold completed, unimported downloads.
        return 'no enabled download client feeds the queue'
    failing, warned = _verdicts(results)
    for client in warned:
        log.warning('[%s] download client test warning, not held against the queue: %s',
                    instance, client)
    if failing:
        return f"download client test failed: {'; '.join(failing)}"
    if rejection is not None and not warned:
        return f'download client test failed ({rejection})'
    return None


def _rejected_results(error):
    """The list of results a 400 from testall carries; None when it carries none."""
    response = error.response
    if response is None or response.status_code != HTTPStatus.BAD_REQUEST:
        return None
    try:
        return object_list(response.json(), TESTALL_ANSWER)
    except (ValueError, ApiError):  # not JSON, or not a list: the error explains itself
        return None


def _verdicts(results):
    """testall's results as (failing clients, clients with only warnings), described.

    A client passes when isValid is true, or when it has failures and every
    one of them is a warning. Anything else fails it, including a failure
    that cannot be read as a warning.
    """
    failing, warned = [], []
    for result in results:
        if result.get('isValid') is True:
            continue
        failures = result.get('validationFailures')
        failures = failures if isinstance(failures, list) else []
        errors = [failure for failure in failures if not _is_warning(failure)]
        client = f"client {result.get('id')}"
        if failures and not errors:
            warned.append(f'{client}: {_messages(failures)}')
        else:
            failing.append(f'{client}: {_messages(errors)}')
    return failing, warned


def _is_warning(failure):
    """Whether the JSON of a validation failure marks it as only a warning."""
    return isinstance(failure, dict) and (
        failure.get('isWarning') is True or failure.get('severity') == WARNING_SEVERITY)


def _messages(failures):
    """The failures' errorMessage values, joined; 'no message' when none has one."""
    messages = [failure['errorMessage'] for failure in failures
                if isinstance(failure, dict) and isinstance(failure.get('errorMessage'), str)
                and failure['errorMessage']]
    return ', '.join(messages) or 'no message'


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
