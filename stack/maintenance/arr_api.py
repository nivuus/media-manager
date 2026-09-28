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


def missing_api_keys(instances):
    """The environment variables whose API key is empty, in instance order."""
    return [instance.api_key_variable for instance in instances
            if not instance.api_key]


class ApiError(Exception):
    """An API call that did not produce a usable answer."""


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
        raise ApiError(f'{method} /api/v3/{path}: {error}') from error
    return response


def get_json(instance, path, *, params=None, timeout=DEFAULT_TIMEOUT):
    """GET a resource and return its decoded body; ApiError if it is not JSON."""
    response = call(instance, 'GET', path, params=params, timeout=timeout)
    try:
        return response.json()
    except ValueError as error:  # requests' JSONDecodeError is a ValueError
        raise ApiError(f'GET /api/v3/{path}: the answer is not JSON ({error})') from error


class Failures:
    """The failures of one run: logged as they happen, counted for the exit status."""

    def __init__(self):
        self.count = 0

    def record(self, context, error):
        self.count += 1
        log.error('%s: %s', context, error)
