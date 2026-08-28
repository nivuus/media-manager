import os
import sys
import time
from datetime import datetime, timezone

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
# Seconds between instances, so they don't hit the same indexers through
# Prowlarr at the same second.
DELAY_BETWEEN_INSTANCES = 120
# Where the rotation offset is remembered between runs.
STATE_FILE = os.environ.get(
    'WANTED_STATE_FILE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '.update_wanted_state')
)

# Les noms sont conservés tels quels : ils servent de clés dans
# .update_wanted_state, les renommer remettrait la rotation à zéro.
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


def read_state():
    """Rotation offsets from the previous run, keyed by instance name."""
    state = {}
    try:
        with open(STATE_FILE, encoding='utf-8') as handle:
            for line in handle:
                name, _, offset = line.rstrip('\n').partition('\t')
                if name and offset.isdigit():
                    state[name] = int(offset)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"Etat de rotation illisible ({STATE_FILE}): {e}")
    return state


def write_state(state):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as handle:
            for name, offset in sorted(state.items()):
                handle.write(f"{name}\t{offset}\n")
    except OSError as e:
        print(f"Etat de rotation non sauvegarde ({STATE_FILE}): {e}")


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


def is_searchable(record, kind):
    """Skip what cannot possibly be found yet: nothing has been released.

    Searching an unreleased title every single day is pure indexer quota burnt
    for a guaranteed zero result.
    """
    now = datetime.now(timezone.utc).isoformat()
    if kind == 'sonarr':
        air_date = record.get('airDateUtc')
        return bool(air_date) and air_date <= now
    release = (
        record.get('digitalRelease')
        or record.get('physicalRelease')
        or record.get('inCinemas')
    )
    return bool(release) and release <= now


def select_slice(records, offset):
    """Take MAX_SEARCH_PER_INSTANCE items starting at offset, wrapping around.

    The wrap-around is what eventually gives the older backlog its turn instead
    of forever re-searching the same head of the list.
    """
    if not records:
        return [], 0
    if len(records) <= MAX_SEARCH_PER_INSTANCE:
        return records, 0
    offset %= len(records)
    selected = records[offset:offset + MAX_SEARCH_PER_INSTANCE]
    if len(selected) < MAX_SEARCH_PER_INSTANCE:
        selected += records[:MAX_SEARCH_PER_INSTANCE - len(selected)]
    return selected, (offset + MAX_SEARCH_PER_INSTANCE) % len(records)


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


def check_and_search(instance, state):
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

    selected, next_offset = select_slice(searchable, state.get(instance['name'], 0))
    print(
        f"[{instance['name']}] {len(missing_items)} manquants "
        f"({skipped} pas encore sortis) -> recherche de {len(selected)}."
    )

    try:
        trigger_search(instance, selected)
        state[instance['name']] = next_offset
        print(f"[{instance['name']}] Search triggered successfully.")
    except requests.exceptions.RequestException as err:
        print(f"[{instance['name']}] Erreur lors du lancement de la recherche: {err}")


def main():
    check_api_keys()
    state = read_state()
    for index, instance in enumerate(instances):
        if index:
            time.sleep(DELAY_BETWEEN_INSTANCES)
        check_and_search(instance, state)
    write_state(state)


if __name__ == '__main__':
    main()
