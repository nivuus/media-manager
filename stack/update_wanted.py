import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configuration from environment variables
SONARR_URL = os.environ.get('SONARR_URL', 'http://localhost:8989')
SONARR_API_KEY = os.environ.get('SONARR_API_KEY', '')

RADARR_URL = os.environ.get('RADARR_URL', 'http://localhost:7878')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY', '')

# Searching every missing item of every instance at once emptied the indexers'
# daily API allowance in minutes (429 TooManyRequests, indexers disabled for
# hours), so the searches that mattered found nothing. Instead: search a bounded
# slice per run, newest first, and rotate through the backlog across days.
MAX_SEARCH_PER_INSTANCE = 100
# Anything aired/released within this window is searched on EVERY run: that is
# where a release still has a real chance of surfacing. The rest of the budget
# goes to the backlog.
#
# Measured on 2026-09-16, Sonarr's 1224 missing episodes: 970 aired more than ten
# years ago (532 of them one single show), and 6 within the last thirty days. The
# previous circular rotation spent 100 slots a day walking that backlog in list
# order, so a brand new episode could wait twelve days for its turn behind
# decade-old ones that never show up. This inverts the priority without asking
# the indexers for a single extra request.
RECENT_WINDOW_DAYS = 30
# Seconds between instances, so they don't hit the same indexers through
# Prowlarr at the same second.
DELAY_BETWEEN_INSTANCES = 120

# Les noms ne servent plus que d'étiquette de journal depuis que la sélection
# est sans état : plus de fichier de rotation dont ils seraient les clés.
instances = [
    {
        'name': 'Sonarr Instance 1',
        'url': SONARR_URL,
        'api_key': SONARR_API_KEY,
        'type': 'sonarr'
    },
    {
        'name': 'Radarr Instance 1',
        'url': RADARR_URL,
        'api_key': RADARR_API_KEY,
        'type': 'radarr'
    }
]


def check_api_keys():
    """Verify that required API keys are configured."""
    missing = []
    if not RADARR_API_KEY:
        missing.append('RADARR_API_KEY')
    if not SONARR_API_KEY:
        missing.append('SONARR_API_KEY')
    if missing:
        print(f"Error: Missing API keys in environment: {', '.join(missing)}")
        print("Configure them in your .env file or as environment variables.")
        sys.exit(1)


def fetch_missing(instance):
    """Every monitored missing item of an instance, newest air/release first."""
    headers = {'X-Api-Key': instance['api_key']}
    records = []
    page = 1
    while True:
        response = requests.get(
            instance['url'] + '/api/v3/wanted/missing',
            headers=headers,
            params={
                'page': page,
                'pageSize': 500,
                'monitored': True,
                'sortKey': 'airDateUtc' if instance['type'] == 'sonarr' else 'movieMetadata.sortTitle',
                'sortDirection': 'descending',
            },
            timeout=180,
        )
        response.raise_for_status()
        payload = response.json()
        batch = payload.get('records', [])
        records.extend(batch)
        if not batch or len(records) >= payload.get('totalRecords', 0):
            break
        page += 1
    return records


def release_date(record, kind):
    """When the item aired (Sonarr) or was released (Radarr), or None.

    Parsed rather than string-compared: Radarr hands back naive dates for some
    release types while Sonarr always sends a 'Z' suffix, and comparing those as
    text silently gets the ordering wrong around the current instant.
    """
    if kind == 'sonarr':
        raw = record.get('airDateUtc')
    else:
        raw = (
            record.get('digitalRelease')
            or record.get('physicalRelease')
            or record.get('inCinemas')
        )
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_searchable(record, kind):
    """Skip what cannot possibly be found yet: nothing has been released.

    Searching an unreleased title every single day is pure indexer quota burnt
    for a guaranteed zero result.
    """
    released = release_date(record, kind)
    return released is not None and released <= datetime.now(timezone.utc)


def select_batch(records, kind):
    """The recent ones first, the rest of the budget drawn from the backlog.

    The backlog is sampled at random rather than walked in rotation: the offset
    it used to keep was an index into a list whose length and order change on
    every run (new episodes are inserted at the head, found ones disappear), so
    it never delivered the even coverage it promised. A draw has the same
    expected coverage, no state file to keep in sync, and no way to starve an
    item forever because the list shifted under the offset.

    Returns (recent, sampled) so the caller can log the split.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)
    recent, backlog = [], []
    for record in records:
        released = release_date(record, kind)
        (recent if released and released >= cutoff else backlog).append(record)

    recent = recent[:MAX_SEARCH_PER_INSTANCE]
    budget_left = MAX_SEARCH_PER_INSTANCE - len(recent)
    sampled = random.sample(backlog, min(budget_left, len(backlog))) if budget_left else []
    return recent, sampled


def trigger_search(instance, records):
    """Search exactly the given items, instead of the whole missing list."""
    headers = {'X-Api-Key': instance['api_key']}
    if instance['type'] == 'sonarr':
        payload = {
            'name': 'EpisodeSearch',
            'episodeIds': [r['id'] for r in records],
        }
    else:
        payload = {
            'name': 'MoviesSearch',
            'movieIds': [r['id'] for r in records],
        }
    response = requests.post(
        instance['url'] + '/api/v3/command',
        headers=headers,
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    return response.status_code


def check_and_search(instance):
    if instance['type'] not in ('sonarr', 'radarr'):
        print(f"Type inconnu pour l'instance {instance['name']}")
        return

    try:
        missing_items = fetch_missing(instance)
    except requests.exceptions.RequestException as err:
        print(f"[{instance['name']}] Erreur lors de la recuperation des elements manquants: {err}")
        return

    if not missing_items:
        print(f"[{instance['name']}] No missing items found.")
        return

    searchable = [r for r in missing_items if is_searchable(r, instance['type'])]
    skipped = len(missing_items) - len(searchable)
    if not searchable:
        print(f"[{instance['name']}] {len(missing_items)} manquants, aucun encore sorti/diffuse.")
        return

    recent, sampled = select_batch(searchable, instance['type'])
    selected = recent + sampled
    print(
        f"[{instance['name']}] {len(missing_items)} manquants "
        f"({skipped} pas encore sortis) -> recherche de {len(selected)} "
        f"({len(recent)} recents <{RECENT_WINDOW_DAYS}j, {len(sampled)} tires du backlog)."
    )

    try:
        trigger_search(instance, selected)
        print(f"[{instance['name']}] Search triggered successfully.")
    except requests.exceptions.RequestException as err:
        print(f"[{instance['name']}] Erreur lors du lancement de la recherche: {err}")


def main():
    check_api_keys()
    for index, instance in enumerate(instances):
        if index:
            time.sleep(DELAY_BETWEEN_INSTANCES)
        check_and_search(instance)


if __name__ == '__main__':
    main()
