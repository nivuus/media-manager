#!/usr/bin/env python3
"""
Media Cleanup Script for MediaManager
Automatically removes stale media when disk space is low.

NOTE: This setup uses SHARED storage between standard and 4K instances.
      Both Radarr instances point to /data/Movies, both Sonarr to /data/TV Shows.
      Duplicate detection is DISABLED because there are no separate files.
"""

import argparse
import logging
import os
import shutil
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =============================================================================
# Configuration (from environment variables)
# =============================================================================

DISK_PATH = os.environ.get('MEDIA_ROOT', '/media/data')
SPACE_THRESHOLD_PERCENT = 25  # Delete media when free space < 25%

TAUTULLI_URL = os.environ.get('TAUTULLI_URL', 'http://localhost:8181') + '/api/v2'
TAUTULLI_API_KEY = os.environ.get('TAUTULLI_API_KEY', '')

OVERSEERR_URL = os.environ.get('OVERSEERR_URL', 'http://localhost:5055') + '/api/v1'
OVERSEERR_API_KEY = os.environ.get('OVERSEERR_API_KEY', '')

# NOTE: Both instances share the same storage - use only 4K instances for deletion
# to avoid deleting files that are referenced by both instances
RADARR_4K_URL = os.environ.get('RADARR_4K_URL', 'http://localhost:7879')
RADARR_4K_API_KEY = os.environ.get('RADARR_4K_API_KEY', os.environ.get('RADARR_API_KEY', ''))

SONARR_4K_URL = os.environ.get('SONARR_4K_URL', 'http://localhost:8990')
SONARR_4K_API_KEY = os.environ.get('SONARR_4K_API_KEY', '')

RADARR_INSTANCES = [
    {'name': 'Radarr-4K', 'url': f'{RADARR_4K_URL}/api/v3', 'api_key': RADARR_4K_API_KEY, 'is_4k': True}
]

SONARR_INSTANCES = [
    {'name': 'Sonarr-4K', 'url': f'{SONARR_4K_URL}/api/v3', 'api_key': SONARR_4K_API_KEY, 'is_4k': True}
]

# Standard instances (for reference only, NOT used for deletion)
RADARR_URL = os.environ.get('RADARR_URL', 'http://localhost:7878')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY', '')
SONARR_URL = os.environ.get('SONARR_URL', 'http://localhost:8989')
SONARR_API_KEY = os.environ.get('SONARR_API_KEY', '')

RADARR_STANDARD = {'name': 'Radarr', 'url': f'{RADARR_URL}/api/v3', 'api_key': RADARR_API_KEY}
SONARR_STANDARD = {'name': 'Sonarr', 'url': f'{SONARR_URL}/api/v3', 'api_key': SONARR_API_KEY}


def check_api_keys():
    """Verify that required API keys are configured."""
    missing = []
    if not RADARR_4K_API_KEY:
        missing.append('RADARR_4K_API_KEY (or RADARR_API_KEY)')
    if not SONARR_4K_API_KEY:
        missing.append('SONARR_4K_API_KEY')
    if not TAUTULLI_API_KEY:
        missing.append('TAUTULLI_API_KEY')
    if missing:
        print(f"Error: Missing API keys in environment: {', '.join(missing)}")
        print("Configure them in your .env file or as environment variables.")
        sys.exit(1)


# =============================================================================
# Disk Space Functions
# =============================================================================

def get_disk_usage(path: str) -> Dict:
    """Check disk space on the given path."""
    usage = shutil.disk_usage(path)
    return {
        'total': usage.total,
        'used': usage.used,
        'free': usage.free,
        'percent_free': (usage.free / usage.total) * 100,
        'percent_used': (usage.used / usage.total) * 100
    }


def space_below_threshold(path: str, threshold_percent: float) -> bool:
    """Check if free space is below the threshold."""
    usage = get_disk_usage(path)
    return usage['percent_free'] < threshold_percent


# =============================================================================
# Tautulli Integration
# =============================================================================

def tautulli_request(cmd: str, params: Dict = None) -> Optional[Dict]:
    """Make a Tautulli API request."""
    if params is None:
        params = {}
    params.update({
        'apikey': TAUTULLI_API_KEY,
        'cmd': cmd
    })
    try:
        response = requests.get(TAUTULLI_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Tautulli API error: {e}")
        return None


def get_tautulli_libraries() -> List[Dict]:
    """Get all library sections from Tautulli."""
    result = tautulli_request('get_libraries')
    if result:
        return result.get('response', {}).get('data', [])
    return []


def get_library_media_info(section_id: int) -> List[Dict]:
    """Get media info with play counts from Tautulli."""
    params = {
        'section_id': section_id,
        'length': 10000
    }
    result = tautulli_request('get_library_media_info', params)
    if result:
        return result.get('response', {}).get('data', {}).get('data', [])
    return []


def build_watch_data_map() -> Dict[str, Dict]:
    """
    Build a map of title -> {last_watched, play_count} from Tautulli.
    """
    title_map = {}

    libraries = get_tautulli_libraries()
    if not libraries:
        logging.warning("Could not fetch Tautulli libraries, using fallback (date added)")
        return title_map

    for library in libraries:
        section_id = library.get('section_id')
        media_info = get_library_media_info(section_id)

        for item in media_info:
            title = item.get('title', '').lower().strip()
            play_count = item.get('play_count', 0) or 0
            last_played = item.get('last_played', 0) or 0

            if title:
                title_map[title] = {
                    'last_watched': int(last_played),
                    'play_count': int(play_count)
                }

    logging.info(f"Fetched watch data for {len(title_map)} items from Tautulli")
    return title_map


# =============================================================================
# Radarr Integration
# =============================================================================

def radarr_request(instance: Dict, endpoint: str, method: str = 'GET', params: Dict = None) -> Optional[requests.Response]:
    """Make a Radarr API request."""
    url = f"{instance['url']}{endpoint}"
    headers = {'X-Api-Key': instance['api_key']}

    try:
        if method == 'GET':
            return requests.get(url, headers=headers, params=params, timeout=30)
        elif method == 'DELETE':
            return requests.delete(url, headers=headers, params=params, timeout=30)
    except Exception as e:
        logging.error(f"[{instance['name']}] Request error: {e}")
        return None


def get_all_movies() -> List[Dict]:
    """Get all movies from both Radarr instances."""
    all_movies = []

    for instance in RADARR_INSTANCES:
        response = radarr_request(instance, '/movie')
        if response and response.status_code == 200:
            movies = response.json()
            for movie in movies:
                if movie.get('hasFile', False):
                    size = movie.get('sizeOnDisk', 0)
                    if movie.get('movieFile'):
                        size = movie['movieFile'].get('size', size)

                    movie['_instance'] = instance
                    movie['_instance_name'] = instance['name']
                    movie['_is_4k'] = instance['is_4k']
                    movie['_size'] = size
                    all_movies.append(movie)

            logging.info(f"[{instance['name']}] Found {len([m for m in movies if m.get('hasFile')])} movies with files")
        else:
            logging.error(f"[{instance['name']}] Failed to fetch movies")

    return all_movies


def delete_movie(movie: Dict, dry_run: bool = False) -> bool:
    """Delete a movie via Radarr API."""
    instance = movie['_instance']
    movie_id = movie['id']
    title = movie.get('title', 'Unknown')
    size_gb = movie['_size'] / 1e9

    if dry_run:
        logging.info(f"[DRY-RUN] Would delete movie: {title} ({size_gb:.2f} GB) from {instance['name']}")
        return True

    response = radarr_request(
        instance,
        f'/movie/{movie_id}',
        method='DELETE',
        params={'deleteFiles': 'true', 'addImportExclusion': 'false'}
    )

    if response and response.status_code in [200, 202, 204]:
        logging.info(f"Deleted movie: {title} ({size_gb:.2f} GB) from {instance['name']}")
        return True
    else:
        logging.error(f"Failed to delete movie {title} from {instance['name']}")
        return False


# =============================================================================
# Sonarr Integration
# =============================================================================

def sonarr_request(instance: Dict, endpoint: str, method: str = 'GET', params: Dict = None) -> Optional[requests.Response]:
    """Make a Sonarr API request."""
    url = f"{instance['url']}{endpoint}"
    headers = {'X-Api-Key': instance['api_key']}

    try:
        if method == 'GET':
            return requests.get(url, headers=headers, params=params, timeout=30)
        elif method == 'DELETE':
            return requests.delete(url, headers=headers, params=params, timeout=30)
    except Exception as e:
        logging.error(f"[{instance['name']}] Request error: {e}")
        return None


def get_all_series() -> List[Dict]:
    """Get all TV series from both Sonarr instances."""
    all_series = []

    for instance in SONARR_INSTANCES:
        response = sonarr_request(instance, '/series')
        if response and response.status_code == 200:
            series_list = response.json()
            for series in series_list:
                size = series.get('statistics', {}).get('sizeOnDisk', 0)
                if size > 0:
                    series['_instance'] = instance
                    series['_instance_name'] = instance['name']
                    series['_is_4k'] = instance['is_4k']
                    series['_size'] = size
                    all_series.append(series)

            logging.info(f"[{instance['name']}] Found {len([s for s in series_list if s.get('statistics', {}).get('sizeOnDisk', 0) > 0])} series with files")
        else:
            logging.error(f"[{instance['name']}] Failed to fetch series")

    return all_series


def delete_series(series: Dict, dry_run: bool = False) -> bool:
    """Delete a TV series via Sonarr API."""
    instance = series['_instance']
    series_id = series['id']
    title = series.get('title', 'Unknown')
    size_gb = series['_size'] / 1e9

    if dry_run:
        logging.info(f"[DRY-RUN] Would delete series: {title} ({size_gb:.2f} GB) from {instance['name']}")
        return True

    response = sonarr_request(
        instance,
        f'/series/{series_id}',
        method='DELETE',
        params={'deleteFiles': 'true', 'addImportListExclusion': 'false'}
    )

    if response and response.status_code in [200, 202, 204]:
        logging.info(f"Deleted series: {title} ({size_gb:.2f} GB) from {instance['name']}")
        return True
    else:
        logging.error(f"Failed to delete series {title} from {instance['name']}")
        return False


# =============================================================================
# Overseerr Integration
# =============================================================================

def overseerr_request(endpoint: str, method: str = 'GET', params: Dict = None) -> Optional[requests.Response]:
    """Make an Overseerr API request."""
    url = f"{OVERSEERR_URL}{endpoint}"
    headers = {'X-Api-Key': OVERSEERR_API_KEY}

    try:
        if method == 'GET':
            return requests.get(url, headers=headers, params=params, timeout=30)
        elif method == 'DELETE':
            return requests.delete(url, headers=headers, timeout=30)
    except Exception as e:
        logging.error(f"Overseerr API error: {e}")
        return None


def reset_overseerr_request(tmdb_id: int, media_type: str, dry_run: bool = False) -> bool:
    """
    Reset/delete an Overseerr request by TMDB ID.
    media_type: 'movie' or 'tv'
    """
    # Get all requests and find matching one
    response = overseerr_request('/request', params={'take': 1000})
    if not response or response.status_code != 200:
        logging.warning("Could not fetch Overseerr requests")
        return False

    requests_data = response.json()
    results = requests_data.get('results', [])

    # Find request matching tmdb_id and type
    for req in results:
        media = req.get('media', {})
        if media.get('tmdbId') == tmdb_id and media.get('mediaType') == media_type:
            request_id = req.get('id')
            if dry_run:
                logging.info(f"[DRY-RUN] Would reset Overseerr request #{request_id} for {media_type} tmdb:{tmdb_id}")
                return True

            del_response = overseerr_request(f'/request/{request_id}', method='DELETE')
            if del_response and del_response.status_code in [200, 204]:
                logging.info(f"Reset Overseerr request #{request_id} for {media_type} tmdb:{tmdb_id}")
                return True
            else:
                logging.warning(f"Failed to reset Overseerr request #{request_id}")
                return False

    logging.debug(f"No Overseerr request found for {media_type} tmdb:{tmdb_id}")
    return False


# =============================================================================
# Media Correlation and Priority
# =============================================================================

def correlate_media_with_watch_data(
    movies: List[Dict],
    series: List[Dict],
    watch_data: Dict[str, Dict]
) -> List[Dict]:
    """
    Correlate Radarr/Sonarr media with Tautulli watch data.
    Returns unified list with reference_date for sorting.
    """
    media_list = []

    for movie in movies:
        title = movie.get('title', '').lower().strip()
        watch_info = watch_data.get(title, {})

        last_watched = watch_info.get('last_watched', 0)
        play_count = watch_info.get('play_count', 0)

        # Parse added date
        added_str = movie.get('added', '')
        try:
            added_ts = int(datetime.fromisoformat(added_str.replace('Z', '+00:00')).timestamp()) if added_str else 0
        except:
            added_ts = 0

        # Reference date: last_watched if watched, else added date
        reference_date = last_watched if last_watched > 0 else added_ts

        media_list.append({
            'type': 'movie',
            'id': movie['id'],
            'title': movie.get('title', 'Unknown'),
            'tmdb_id': movie.get('tmdbId'),
            'size': movie.get('_size', 0),
            'instance': movie['_instance'],
            'instance_name': movie['_instance_name'],
            'last_watched': last_watched,
            'play_count': play_count,
            'added': added_ts,
            'reference_date': reference_date,
            '_raw': movie
        })

    for series_item in series:
        title = series_item.get('title', '').lower().strip()
        watch_info = watch_data.get(title, {})

        last_watched = watch_info.get('last_watched', 0)
        play_count = watch_info.get('play_count', 0)

        added_str = series_item.get('added', '')
        try:
            added_ts = int(datetime.fromisoformat(added_str.replace('Z', '+00:00')).timestamp()) if added_str else 0
        except:
            added_ts = 0

        reference_date = last_watched if last_watched > 0 else added_ts

        media_list.append({
            'type': 'series',
            'id': series_item['id'],
            'title': series_item.get('title', 'Unknown'),
            'tmdb_id': series_item.get('tvdbId'),  # Using tvdbId for series
            'size': series_item.get('_size', 0),
            'instance': series_item['_instance'],
            'instance_name': series_item['_instance_name'],
            'last_watched': last_watched,
            'play_count': play_count,
            'added': added_ts,
            'reference_date': reference_date,
            '_raw': series_item
        })

    return media_list


def sort_for_deletion(media_list: List[Dict]) -> List[Dict]:
    """
    Sort media by deletion priority (most stale first).
    Primary: reference_date ascending (oldest first)
    Secondary: play_count ascending (least watched first)
    """
    return sorted(media_list, key=lambda x: (x['reference_date'], x['play_count']))


# =============================================================================
# Main Cleanup Logic
# =============================================================================

def cleanup_media(dry_run: bool = False, verbose: bool = False, threshold: float = SPACE_THRESHOLD_PERCENT) -> Dict:
    """Main cleanup function."""
    stats = {
        'stale_movies': 0,
        'stale_series': 0,
        'space_freed': 0,
        'initial_free_percent': 0,
        'final_free_percent': 0,
        'items_deleted': []
    }

    # Step 1: Check disk space
    logging.info("=" * 60)
    logging.info("Step 1: Checking disk space")
    logging.info("=" * 60)

    disk = get_disk_usage(DISK_PATH)
    stats['initial_free_percent'] = disk['percent_free']

    logging.info(f"Disk: {DISK_PATH}")
    logging.info(f"  Used:  {disk['percent_used']:.1f}% ({disk['used'] / 1e12:.2f} TB)")
    logging.info(f"  Free:  {disk['percent_free']:.1f}% ({disk['free'] / 1e12:.2f} TB)")
    logging.info(f"  Threshold: {threshold}%")

    if not space_below_threshold(DISK_PATH, threshold):
        logging.info(f"Free space ({disk['percent_free']:.1f}%) is above threshold ({threshold}%). No cleanup needed.")
        stats['final_free_percent'] = disk['percent_free']
        return stats

    logging.info(f"Free space below {threshold}%. Starting cleanup...")

    # Step 2: Fetch data
    logging.info("")
    logging.info("=" * 60)
    logging.info("Step 2: Fetching media and watch data")
    logging.info("=" * 60)

    watch_data = build_watch_data_map()
    movies = get_all_movies()
    series = get_all_series()

    # Step 4: Correlate and sort
    media_list = correlate_media_with_watch_data(movies, series, watch_data)
    sorted_media = sort_for_deletion(media_list)

    logging.info(f"Total media available for cleanup: {len(sorted_media)}")

    if verbose and sorted_media:
        logging.info("\nMedia sorted by deletion priority (first 10):")
        for i, item in enumerate(sorted_media[:10]):
            ref_date_str = datetime.fromtimestamp(item['reference_date']).strftime('%Y-%m-%d') if item['reference_date'] else 'Unknown'
            watched_str = "watched" if item['last_watched'] > 0 else "never watched"
            logging.info(f"  {i+1}. [{item['type']}] {item['title']} - {ref_date_str} ({watched_str}), Plays: {item['play_count']}, Size: {item['size']/1e9:.2f} GB")

    # Step 3: Delete until threshold met
    logging.info("")
    logging.info("=" * 60)
    logging.info("Step 3: Deleting stale media")
    logging.info("=" * 60)

    for media_item in sorted_media:
        # Re-check disk space (skip in dry-run since nothing actually deleted)
        if not dry_run and not space_below_threshold(DISK_PATH, threshold):
            logging.info("Threshold reached. Stopping cleanup.")
            break

        # In dry-run, stop after simulating enough space freed
        if dry_run:
            simulated_free = disk['free'] + stats['space_freed']
            simulated_percent = (simulated_free / disk['total']) * 100
            if simulated_percent >= threshold:
                logging.info(f"[DRY-RUN] Simulated threshold reached ({simulated_percent:.1f}%). Stopping.")
                break

        # Delete the media item
        success = False
        if media_item['type'] == 'movie':
            success = delete_movie(media_item['_raw'], dry_run=dry_run)
            if success:
                stats['stale_movies'] += 1
        else:
            success = delete_series(media_item['_raw'], dry_run=dry_run)
            if success:
                stats['stale_series'] += 1

        if success:
            stats['space_freed'] += media_item['size']
            stats['items_deleted'].append({
                'type': media_item['type'],
                'title': media_item['title'],
                'size': media_item['size']
            })

            # Reset Overseerr request
            if media_item['tmdb_id']:
                media_type = 'movie' if media_item['type'] == 'movie' else 'tv'
                reset_overseerr_request(media_item['tmdb_id'], media_type, dry_run=dry_run)

    # Final stats
    disk = get_disk_usage(DISK_PATH)
    stats['final_free_percent'] = disk['percent_free']

    return stats


# =============================================================================
# CLI
# =============================================================================

def setup_logging(log_file: str = None, verbose: bool = False):
    """Configure logging."""
    log_level = logging.DEBUG if verbose else logging.INFO

    handlers = [logging.StreamHandler(sys.stdout)]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers
    )


def main():
    parser = argparse.ArgumentParser(
        description='MediaManager Automatic Cleanup Script',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python3 media_cleanup.py                    # Run cleanup
  python3 media_cleanup.py --dry-run          # Simulate without deleting
  python3 media_cleanup.py --threshold 15     # Custom threshold (15%)
  python3 media_cleanup.py --log cleanup.log  # Log to file
  python3 media_cleanup.py --status           # Show disk status only
        '''
    )

    parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Simulate cleanup without actually deleting files'
    )
    parser.add_argument(
        '--threshold', '-t',
        type=float,
        default=SPACE_THRESHOLD_PERCENT,
        help=f'Free space threshold percentage (default: {SPACE_THRESHOLD_PERCENT})'
    )
    parser.add_argument(
        '--log', '-l',
        type=str,
        help='Log file path'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose output'
    )
    parser.add_argument(
        '--status', '-s',
        action='store_true',
        help='Show current disk status and exit'
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(log_file=args.log, verbose=args.verbose)

    # Check API keys
    if not args.status:
        check_api_keys()

    # Status mode
    if args.status:
        disk = get_disk_usage(DISK_PATH)
        print(f"Disk: {DISK_PATH}")
        print(f"  Total: {disk['total'] / 1e12:.2f} TB")
        print(f"  Used:  {disk['used'] / 1e12:.2f} TB ({disk['percent_used']:.1f}%)")
        print(f"  Free:  {disk['free'] / 1e12:.2f} TB ({disk['percent_free']:.1f}%)")
        print(f"  Threshold: {args.threshold}%")
        print(f"  Cleanup needed: {'Yes' if disk['percent_free'] < args.threshold else 'No'}")
        return

    # Run cleanup
    if args.dry_run:
        logging.info("=" * 60)
        logging.info("DRY-RUN MODE - No files will be deleted")
        logging.info("=" * 60)

    stats = cleanup_media(dry_run=args.dry_run, verbose=args.verbose, threshold=args.threshold)

    # Print summary
    logging.info("")
    logging.info("=" * 60)
    logging.info("CLEANUP SUMMARY")
    logging.info("=" * 60)
    logging.info(f"Media removed: {stats['stale_movies']} movies, {stats['stale_series']} series")
    logging.info(f"Total space freed: {stats['space_freed'] / 1e9:.2f} GB")
    logging.info(f"Disk free: {stats['initial_free_percent']:.1f}% -> {stats['final_free_percent']:.1f}%")


if __name__ == '__main__':
    main()
