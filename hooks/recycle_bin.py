#!/usr/bin/env python3
"""Radarr's and Sonarr's recycle bin, which Recyclarr cannot set.

Without a recycle bin, both apps delete a replaced file for good. An upgrade
chosen in an interactive search does exactly that: on 2026-11-21 a 2160p
English/Italian "DUAL" release replaced a French 1080p remux of the same film,
and the French file was gone. Recyclarr manages only `propers_and_repacks`
under Media Management, so the setting is applied here, through the API.

Two parts, run by the activate hook once the services answer:

1. THE DIRECTORIES. Both apps validate the path when it is set: it must exist,
   be writable by the service, and be neither a root folder nor one of its
   ancestors. One directory per app under `${MEDIA_ROOT}/.recycle-bin`, on the
   same filesystem and inside the same `${MEDIA_ROOT}:/data` bind mount as the
   library, so sending a file to the bin is a rename, not a 50 GB copy. Owned
   by PUID:PGID through data_dirs (no symlink followed: the media root is
   often world-writable).

2. THE SETTING. Set only while it is still empty, the apps' default: a path
   someone chose in the UI is theirs, and is left alone with a message.

CLEANUP_DAYS must stay below Maintainerr's 14-day grace (see CLAUDE.md,
"Disk Cleanup"). A Maintainerr deletion goes through Radarr/Sonarr and so
lands in the bin too; the space only comes back when the bin is purged, and
it must come back before Maintainerr's next batch is due, or the rules that
watch free space would keep matching titles they no longer need to.
"""
import json
import os
import time
import urllib.error
import urllib.request

from data_dirs import ensure_data_dirs

BIN_DIR = ".recycle-bin"
# Where every Radarr/Sonarr container sees ${MEDIA_ROOT} (docker-compose.yml).
CONTAINER_MEDIA_ROOT = "/data"
CLEANUP_DAYS = 7
DIR_MODE = 0o750

# App -> (base URL on the host, .env variable holding its API key).
APPS = {
    "radarr": ("http://127.0.0.1:7878", "RADARR_API_KEY"),
    "sonarr": ("http://127.0.0.1:8989", "SONARR_API_KEY"),
}

READY_TIMEOUT = 300
READY_INTERVAL = 5


class RecycleBinError(RuntimeError):
    """An app did not come up, or refused the setting."""


def bin_path(app):
    """The app's recycle bin as the container sees it."""
    return f"{CONTAINER_MEDIA_ROOT}/{BIN_DIR}/{app}"


def ensure_dirs(media_root, uid, gid):
    """Create ${MEDIA_ROOT}/.recycle-bin/<app> for every app, owned by uid:gid."""
    ensure_data_dirs(media_root, uid, gid,
                     {f"{BIN_DIR}/{app}": DIR_MODE for app in APPS})


def _call(base, key, method, path, body=None, opener=urllib.request.urlopen):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{base}/api/v3/{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "X-Api-Key": key})
    try:
        with opener(request, timeout=30) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as exc:
        # The validation errors Radarr/Sonarr return are the useful part.
        detail = exc.read().decode(errors="replace")[:500]
        raise RecycleBinError(f"{method} {base}/api/v3/{path}: {exc}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RecycleBinError(f"{method} {base}/api/v3/{path}: {exc}") from exc
    try:
        return json.loads(text) if text else None
    except ValueError as exc:
        raise RecycleBinError(f"{method} {base}/api/v3/{path}: not JSON") from exc


def _wait_ready(base, key, opener, clock, sleep):
    deadline = clock() + READY_TIMEOUT
    while True:
        try:
            return _call(base, key, "GET", "system/status", opener=opener)
        except RecycleBinError:
            if clock() >= deadline:
                raise
            sleep(READY_INTERVAL)


def configure(keys, opener=urllib.request.urlopen, clock=time.monotonic,
              sleep=time.sleep):
    """Set each app's recycle bin while it is empty. Returns progress messages.

    `keys` maps the .env variables to the API keys. An app without a key is
    reported, not configured: the activate hook already says which keys it
    could not harvest, and they are filled in by hand.
    """
    messages = []
    for app, (base, variable) in APPS.items():
        key = keys.get(variable, "")
        if not key:
            messages.append(f"{app}: no API key ({variable}), recycle bin not set")
            continue
        _wait_ready(base, key, opener, clock, sleep)
        config = _call(base, key, "GET", "config/mediamanagement", opener=opener)
        if not isinstance(config, dict) or "id" not in config:
            raise RecycleBinError(f"{app}: unexpected media management config")
        current = (config.get("recycleBin") or "").strip()
        wanted = bin_path(app)
        if current == wanted:
            continue
        if current:
            messages.append(f"{app}: recycle bin set by hand to {current}, left as is")
            continue
        _call(base, key, "PUT", f"config/mediamanagement/{config['id']}",
              {**config, "recycleBin": wanted, "recycleBinCleanupDays": CLEANUP_DAYS},
              opener=opener)
        messages.append(f"{app}: recycle bin {wanted}, purged after {CLEANUP_DAYS} days")
    return messages


def env_value(text, name):
    """The last value assigned to `name` in a .env text, quotes stripped; "" if absent."""
    found = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            if key.strip() == name:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                found = value
    return found


def media_root(env_text):
    """MEDIA_ROOT from the .env, refused by name when missing or relative."""
    root = env_value(env_text, "MEDIA_ROOT").rstrip("/")
    if not os.path.isabs(root):
        raise RecycleBinError(f"MEDIA_ROOT in .env must be an absolute path, got {root!r}")
    return root
