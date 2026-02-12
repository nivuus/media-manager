import os
import sys
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configuration
DOWNLOADS_DIR = os.environ.get('DOWNLOADS_DIR', '/media/data/Downloads')
MAX_FILE_AGE_HOURS = 24
REQUIRED_SUBDIRS = ['tv-sonarr', 'radarr']
PUID = int(os.environ.get('PUID', 1000))
PGID = int(os.environ.get('PGID', 1000))

# Instances de Radarr
RADARR_URL = os.environ.get('RADARR_URL', 'http://localhost:7878')
RADARR_4K_URL = os.environ.get('RADARR_4K_URL', 'http://localhost:7879')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY', '')
RADARR_4K_API_KEY = os.environ.get('RADARR_4K_API_KEY', RADARR_API_KEY)

RADARR_INSTANCES = [
    {'url': f'{RADARR_URL}/api/v3', 'api_key': RADARR_API_KEY},
    {'url': f'{RADARR_4K_URL}/api/v3', 'api_key': RADARR_4K_API_KEY}
]

# Instances de Sonarr
SONARR_URL = os.environ.get('SONARR_URL', 'http://localhost:8989')
SONARR_4K_URL = os.environ.get('SONARR_4K_URL', 'http://localhost:8990')
SONARR_API_KEY = os.environ.get('SONARR_API_KEY', '')
SONARR_4K_API_KEY = os.environ.get('SONARR_4K_API_KEY', '')

SONARR_INSTANCES = [
    {'url': f'{SONARR_URL}/api/v3', 'api_key': SONARR_API_KEY},
    {'url': f'{SONARR_4K_URL}/api/v3', 'api_key': SONARR_4K_API_KEY}
]


def check_api_keys():
    """Verify that required API keys are configured."""
    missing = []
    if not RADARR_API_KEY:
        missing.append('RADARR_API_KEY')
    if not RADARR_4K_API_KEY:
        missing.append('RADARR_4K_API_KEY')
    if not SONARR_API_KEY:
        missing.append('SONARR_API_KEY')
    if not SONARR_4K_API_KEY:
        missing.append('SONARR_4K_API_KEY')
    if missing:
        print(f"Error: Missing API keys in environment: {', '.join(missing)}")
        print("Configure them in your .env file or as environment variables.")
        sys.exit(1)


def cleanup_downloads_directory():
    """Supprime les fichiers de plus de 24h dans le dossier Downloads."""
    current_time = time.time()
    max_age_seconds = MAX_FILE_AGE_HOURS * 3600

    for root, dirs, files in os.walk(DOWNLOADS_DIR):
        for file in files:
            file_path = os.path.join(root, file)
            try:
                file_age = current_time - os.path.getmtime(file_path)
                if file_age > max_age_seconds:
                    os.remove(file_path)
                    print(f"[Cleanup] Suppression (>{MAX_FILE_AGE_HOURS}h): {file_path}")
            except Exception as e:
                print(f"Erreur lors de la suppression du fichier {file_path}: {e}")

    # Suppression des dossiers vides
    for root, dirs, files in os.walk(DOWNLOADS_DIR, topdown=False):
        for dir in dirs:
            dir_path = os.path.join(root, dir)
            try:
                os.rmdir(dir_path)
            except OSError:
                pass

    # Recréer les sous-dossiers requis par Sonarr/Radarr
    for subdir in REQUIRED_SUBDIRS:
        subdir_path = os.path.join(DOWNLOADS_DIR, subdir)
        os.makedirs(subdir_path, exist_ok=True)
        os.chown(subdir_path, PUID, PGID)


def delete_error_downloads_radarr():
    for instance in RADARR_INSTANCES:
        try:
            response = requests.get(f"{instance['url']}/queue", headers={'X-Api-Key': instance['api_key']})
            response.raise_for_status()
            queue = response.json()

            records = queue.get('records', [])
            if not isinstance(records, list):
                print(f"[Radarr {instance['url']}] La clé 'records' n'est pas une liste : {queue}")
                continue

            for item in records:
                if 'status' in item and item['status'] == 'error':
                    print(f"[Radarr {instance['url']}] Suppression de : {item['title']}")
                    requests.delete(f"{instance['url']}/queue/{item['id']}", headers={'X-Api-Key': instance['api_key']})
        except requests.exceptions.RequestException as e:
            print(f"Erreur lors de la connexion à Radarr {instance['url']}: {e}")

def delete_error_downloads_sonarr():
    for instance in SONARR_INSTANCES:
        try:
            response = requests.get(f"{instance['url']}/queue", headers={'X-Api-Key': instance['api_key']})
            response.raise_for_status()
            queue = response.json()

            records = queue.get('records', [])
            if not isinstance(records, list):
                print(f"[Sonarr {instance['url']}] La clé 'records' n'est pas une liste : {queue}")
                continue

            for item in records:
                if 'status' in item and item['status'] == 'error':
                    print(f"[Sonarr {instance['url']}] Suppression de : {item['title']} - Épisode: S{item['episode']['seasonNumber']}E{item['episode']['episodeNumber']}")
                    requests.delete(f"{instance['url']}/queue/{item['id']}", headers={'X-Api-Key': instance['api_key']})
        except requests.exceptions.RequestException as e:
            print(f"Erreur lors de la connexion à Sonarr {instance['url']}: {e}")

if __name__ == "__main__":
    check_api_keys()
    cleanup_downloads_directory()
    delete_error_downloads_radarr()
    delete_error_downloads_sonarr()
