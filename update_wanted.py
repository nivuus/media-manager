import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configuration from environment variables
SONARR_URL = os.environ.get('SONARR_URL', 'http://localhost:8989')
SONARR_4K_URL = os.environ.get('SONARR_4K_URL', 'http://localhost:8990')
SONARR_API_KEY = os.environ.get('SONARR_API_KEY', '')
SONARR_4K_API_KEY = os.environ.get('SONARR_4K_API_KEY', '')

RADARR_URL = os.environ.get('RADARR_URL', 'http://localhost:7878')
RADARR_4K_URL = os.environ.get('RADARR_4K_URL', 'http://localhost:7879')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY', '')
RADARR_4K_API_KEY = os.environ.get('RADARR_4K_API_KEY', RADARR_API_KEY)

instances = [
    {
        'name': 'Sonarr Instance 1',
        'url': SONARR_URL,
        'api_key': SONARR_API_KEY,
        'type': 'sonarr'
    },
    {
        'name': 'Sonarr Instance 2',
        'url': SONARR_4K_URL,
        'api_key': SONARR_4K_API_KEY,
        'type': 'sonarr'
    },
    {
        'name': 'Radarr Instance 1',
        'url': RADARR_URL,
        'api_key': RADARR_API_KEY,
        'type': 'radarr'
    },
    {
        'name': 'Radarr Instance 2',
        'url': RADARR_4K_URL,
        'api_key': RADARR_4K_API_KEY,
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
    if not SONARR_4K_API_KEY:
        missing.append('SONARR_4K_API_KEY')
    if missing:
        print(f"Error: Missing API keys in environment: {', '.join(missing)}")
        print("Configure them in your .env file or as environment variables.")
        sys.exit(1)


def check_and_search(instance):
    headers = {'X-Api-Key': instance['api_key']}
    if instance['type'] == 'sonarr':
        # Ajuster l'endpoint selon la version de Sonarr
        missing_endpoint = '/api/v3/wanted/missing'
        search_endpoint = '/api/v3/command'
    elif instance['type'] == 'radarr':
        missing_endpoint = '/api/v3/wanted/missing'
        search_endpoint = '/api/v3/command'
    else:
        print(f"Type inconnu pour l'instance {instance['name']}")
        return

    # Récupérer les éléments manquants
    try:
        response = requests.get(instance['url'] + missing_endpoint, headers=headers)
        response.raise_for_status()
        missing_items = response.json()['records']
        if missing_items:
            # Démarrer une recherche pour les éléments manquants
            payload = {'name': 'MissingEpisodeSearch'} if instance['type'] == 'sonarr' else {'name': 'missingMoviesSearch'}
            search_response = requests.post(instance['url'] + search_endpoint, headers=headers, json=payload)
            if search_response.status_code != 201:
                print(f"Erreur lors du lancement de la recherche sur {instance['name']}: {search_response.status_code} - {search_response.text}")
    except requests.exceptions.HTTPError as http_err:
        print(f"Erreur HTTP pour {instance['name']}: {http_err}")
    except Exception as err:
        print(f"Erreur lors de la récupération des éléments manquants de {instance['name']}: {err}")

def main():
    check_api_keys()
    for instance in instances:
        check_and_search(instance)

if __name__ == '__main__':
    main()
