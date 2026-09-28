"""Radarr/Sonarr configuration and API access, shared by the maintenance scripts.

Configuration comes from the environment. Under systemd the unit loads the
deployment's .env through EnvironmentFile=; a manual run finds the same file
through python-dotenv.
"""
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

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


def call(instance, method, path, *, params=None, payload=None,
         timeout=DEFAULT_TIMEOUT):
    """One API v3 call. Raises requests' exceptions, non-2xx statuses included."""
    response = requests.request(
        method, f'{instance.url}/api/v3/{path}',
        headers={'X-Api-Key': instance.api_key},
        params=params, json=payload, timeout=timeout)
    response.raise_for_status()
    return response
