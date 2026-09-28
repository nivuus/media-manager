"""Radarr/Sonarr configuration and API access, shared by the maintenance scripts.

Configuration comes from the environment. Under systemd the unit loads the
deployment's .env through EnvironmentFile=; a manual run finds the same file
through python-dotenv.

Every API failure surfaces as ApiError, and each step reports it to a
Failures recorder instead of swallowing it: the run carries on with what it
can still do safely, then exits non-zero so that systemd marks the unit
failed and OnFailure= can alert.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

log = logging.getLogger(__name__)

# (connect, read) in seconds. Without a timeout requests waits forever, and a
# single hung call would hold the unit until systemd kills it.
DEFAULT_TIMEOUT = (10, 60)


def load_environment():
    """Load the deployment's .env; variables already set are left alone.

    python-dotenv searches upwards from this file, so it finds the .env one
    level up, next to the scripts, as it did when each script called it.
    """
    load_dotenv()


@dataclass(frozen=True)
class Instance:
    """One Radarr or Sonarr server."""

    kind: str  # 'radarr' or 'sonarr'
    name: str  # label for log lines
    url: str  # base URL, without /api/v3
    api_key: str
    api_key_variable: str  # the environment variable api_key comes from

    def __str__(self):
        return f'{self.name} {self.url}'


# One instance per type since the 4K pairs were merged (2026-08-18). These stay
# lists: adding an instance back is one more line, every caller already loops.
def radarr_instances(environ):
    return [
        Instance('radarr', 'Radarr',
                 environ.get('RADARR_URL', 'http://localhost:7878'),
                 environ.get('RADARR_API_KEY', ''), 'RADARR_API_KEY'),
    ]


def sonarr_instances(environ):
    return [
        Instance('sonarr', 'Sonarr',
                 environ.get('SONARR_URL', 'http://localhost:8989'),
                 environ.get('SONARR_API_KEY', ''), 'SONARR_API_KEY'),
    ]


def check_api_keys(instances):
    """Whether every instance has its API key; logs the missing variables when not.

    Checked before any call: without its key every call to an instance
    fails, so a run stops there and exits 1 instead.
    """
    missing = [instance.api_key_variable for instance in instances
               if not instance.api_key]
    if missing:
        log.error('Missing API keys in environment: %s. Configure them in the '
                  '.env file or as environment variables.', ', '.join(missing))
    return not missing


class ApiError(Exception):
    """An API call that did not produce a usable answer.

    `response` is the server's answer when it replied with an error status,
    for a caller that knows what the body of that error says; None for any
    other failure.
    """

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


def call(instance, method, path, *, params=None, payload=None,
         timeout=DEFAULT_TIMEOUT):
    """One API v3 call, returning the response.

    Raises ApiError on a network error, a timeout or a non-2xx status.
    """
    try:
        response = requests.request(
            method, f'{instance.url}/api/v3/{path}',
            headers={'X-Api-Key': instance.api_key},
            params=params, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        # raise_for_status() attaches the answer to the HTTPError it raises;
        # a network error or a timeout has none.
        raise ApiError(f'{method} /api/v3/{path}: {error}',
                       response=error.response) from error
    return response


def call_json(instance, method, path, *, params=None, payload=None,
              timeout=DEFAULT_TIMEOUT):
    """One API v3 call, returning its decoded body.

    Raises ApiError as call() does, and when the body is not JSON.
    """
    response = call(instance, method, path, params=params, payload=payload,
                    timeout=timeout)
    try:
        return response.json()
    except ValueError as error:  # requests' JSONDecodeError is a ValueError
        raise ApiError(f'{method} /api/v3/{path}: the answer is not JSON ({error})') from error


def get_json(instance, path, *, params=None, timeout=DEFAULT_TIMEOUT):
    """GET a resource and return its decoded body; ApiError if it is not JSON."""
    return call_json(instance, 'GET', path, params=params, timeout=timeout)


def object_list(value, what):
    """`value` when it is a list of JSON objects; ApiError saying `what` is not."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ApiError(f'{what} is not a list of objects')
    return value


def get_json_list(instance, path, *, params=None, timeout=DEFAULT_TIMEOUT):
    """GET a collection and return its items.

    Raises ApiError as get_json() does, and when the answer is not a list of
    objects: the caller loops over the items and reads each with .get(), so
    any other shape must stop it here, not halfway through the loop.
    """
    return object_list(get_json(instance, path, params=params, timeout=timeout),
                       f'the answer to GET /api/v3/{path}')


def get_page(instance, path, *, params=None, timeout=DEFAULT_TIMEOUT):
    """GET one page of a paged collection; return its (records, totalRecords).

    Raises ApiError as get_json() does, when the answer is not an object or
    its 'records' is not a list of objects, and when its 'totalRecords' is
    not an integer: that count is how the caller knows whether it holds
    every row, and an answer without it cannot say.
    """
    page = get_json(instance, path, params=params, timeout=timeout)
    records = object_list(page.get('records') if isinstance(page, dict) else None,
                          f"the 'records' of the {path} answer")
    total = page.get('totalRecords')
    if not isinstance(total, int):
        raise ApiError(f"the {path} answer has no 'totalRecords' count")
    return records, total


def parse_time(raw):
    """A timestamp as Radarr/Sonarr send it, as an aware UTC datetime; None when unusable.

    ISO 8601 with a 'Z' suffix or an offset, or naive: Radarr sends naive
    dates for some release types, and both apps store UTC, so a naive value
    is UTC, never local time (which astimezone() would silently assume).
    Comparing the raw text instead would get the order wrong around the
    current instant.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Failures:
    """The failures of one run: logged as they happen, counted for the exit status."""

    def __init__(self):
        self.count = 0

    def record(self, context, error):
        self.count += 1
        log.error('%s: %s', context, error)


def exit_status(failures):
    """1 when `failures` recorded anything this run, 0 otherwise.

    What every maintenance script's run() returns: systemd marks the unit
    failed on 1, so OnFailure= can alert.
    """
    if failures.count:
        log.error('Run finished with %d failure(s).', failures.count)
        return 1
    return 0
