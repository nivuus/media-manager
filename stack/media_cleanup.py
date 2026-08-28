#!/usr/bin/env python3
"""
Media Cleanup Script for MediaManager
Automatically removes stale media when disk space is low.

NOTE: One Radarr and one Sonarr instance since the 4K instances were merged
      in (2026-08-18). Radarr points to /data/Movies, Sonarr to /data/TV Shows.
      Duplicate detection is DISABLED: one title, one file.
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
SPACE_THRESHOLD_PERCENT = 5  # Delete media when free space < 5%

TAUTULLI_URL = os.environ.get('TAUTULLI_URL', 'http://localhost:8181') + '/api/v2'
TAUTULLI_API_KEY = os.environ.get('TAUTULLI_API_KEY', '')

OVERSEERR_URL = os.environ.get('OVERSEERR_URL', 'http://localhost:5055') + '/api/v1'
OVERSEERR_API_KEY = os.environ.get('OVERSEERR_API_KEY', '')

# Every configured instance is scanned, and entries are de-duplicated by
# physical folder path (see get_all_movies/get_all_series) with the deletion
# issued to every instance referencing that path. Kept as-is after the 4K merge:
# a single instance is simply the degenerate case, and the structure survives
# adding an instance back.
RADARR_URL = os.environ.get('RADARR_URL', 'http://localhost:7878')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY', '')


SONARR_URL = os.environ.get('SONARR_URL', 'http://localhost:8989')
SONARR_API_KEY = os.environ.get('SONARR_API_KEY', '')


RADARR_INSTANCES = [
    {'name': 'Radarr', 'url': f'{RADARR_URL}/api/v3', 'api_key': RADARR_API_KEY},
]

SONARR_INSTANCES = [
    {'name': 'Sonarr', 'url': f'{SONARR_URL}/api/v3', 'api_key': SONARR_API_KEY},
]


def check_api_keys():
    """Verify that required API keys are configured."""
    missing = []
    for inst in RADARR_INSTANCES + SONARR_INSTANCES:
        if not inst['api_key']:
            missing.append(f"{inst['name']} API key")
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


def get_real_size(path: str) -> int:
    """Get actual size on disk for a file or directory."""
    if not path or not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


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


def get_watch_history() -> List[Dict]:
    """Get watch history from Tautulli (actual play events)."""
    result = tautulli_request('get_history', {'length': 10000})
    if result:
        return result.get('response', {}).get('data', {}).get('data', [])
    return []


def watch_key(title: str, year=None) -> str:
    """Normalized lookup key. Year disambiguates remakes/same-title works."""
    title = (title or '').lower().strip()
    try:
        year = int(year)
    except (TypeError, ValueError):
        year = 0
    return f"{title}|{year}" if year else title


def _merge_watch_entry(title_map: Dict[str, Dict], key: str, last_watched: int, play_count: int):
    """Insert/merge a watch entry, keeping the most recent date and highest count."""
    if key in title_map:
        title_map[key]['last_watched'] = max(title_map[key]['last_watched'], last_watched)
        title_map[key]['play_count'] = max(title_map[key]['play_count'], play_count)
    else:
        title_map[key] = {'last_watched': last_watched, 'play_count': play_count}


def build_watch_data_map() -> Dict[str, Dict]:
    """
    Build a map of "title|year" -> {last_watched, play_count} from Tautulli.

    Keying on title AND year avoids correlating the wrong work when two share a
    title (remakes, reboots) — which previously risked deleting a recently
    watched item. Each entry is also indexed by bare title as a fallback for
    when the year is missing on one side.
    Sources: get_library_media_info (play counts) + get_history (play events).
    """
    title_map: Dict[str, Dict] = {}

    def index(title, year, last_watched, play_count):
        title = (title or '').lower().strip()
        if not title:
            return
        _merge_watch_entry(title_map, watch_key(title, year), last_watched, play_count)
        # Bare-title fallback key (only helps when unambiguous on lookup side).
        if year:
            _merge_watch_entry(title_map, title, last_watched, play_count)

    # Source 1: Library media info (may have gaps)
    libraries = get_tautulli_libraries()
    lib_items = 0
    if not libraries:
        logging.warning("Could not fetch Tautulli libraries")
    else:
        for library in libraries:
            for item in get_library_media_info(library.get('section_id')):
                index(item.get('title', ''), item.get('year'),
                      int(item.get('last_played', 0) or 0),
                      int(item.get('play_count', 0) or 0))
                lib_items += 1
        logging.info(f"Library media info: {lib_items} items")

    # Source 2: Watch history (fills gaps + more accurate recent play dates)
    history = get_watch_history()
    history_play_counts: Dict[str, int] = {}
    for entry in history:
        # For TV shows, grandparent_title is the show name; for movies, use title
        title = (entry.get('grandparent_title') or entry.get('title', '')).lower().strip()
        if not title:
            continue
        key = watch_key(title, entry.get('year'))
        history_play_counts[key] = history_play_counts.get(key, 0) + 1
        index(title, entry.get('year'), int(entry.get('date', 0) or 0), 1)

    # Promote history-derived play counts where they exceed the library figure.
    for key, count in history_play_counts.items():
        if key in title_map and title_map[key]['play_count'] < count:
            title_map[key]['play_count'] = count

    logging.info(f"Total watch data: {len(title_map)} keys")
    return title_map


def lookup_watch_info(watch_data: Dict[str, Dict], title: str, year=None) -> Dict:
    """Look up watch info by (title, year), falling back to bare title."""
    return (watch_data.get(watch_key(title, year))
            or watch_data.get((title or '').lower().strip())
            or {})


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
    """Get all movies from every Radarr instance, de-duplicated by folder path.

    We keep ONE entry per physical path (size counted once) and record every
    (instance, id) that references it, so deletion removes the file AND every DB
    record pointing at it — whatever the number of instances configured.
    """
    by_path: Dict[str, Dict] = {}

    for instance in RADARR_INSTANCES:
        response = radarr_request(instance, '/movie')
        if not (response and response.status_code == 200):
            logging.error(f"[{instance['name']}] Failed to fetch movies")
            continue

        movies = response.json()
        with_files = 0
        for movie in movies:
            if not movie.get('hasFile', False):
                continue
            with_files += 1
            path = (movie.get('path') or '').rstrip('/')
            key = path or f"{instance['name']}:{movie['id']}"  # fallback: never merge unknown paths

            ref = {'instance': instance, 'id': movie['id']}
            if key in by_path:
                by_path[key]['_instances'].append(ref)
                continue

            # Real disk size (accurate after Tdarr), counted once per folder.
            real_size = get_real_size(path)
            if real_size == 0:
                real_size = movie.get('sizeOnDisk', 0)
                if movie.get('movieFile'):
                    real_size = movie['movieFile'].get('size', real_size)

            movie['_instances'] = [ref]
            movie['_instance'] = instance          # primary (back-compat)
            movie['_instance_name'] = instance['name']
            movie['_size'] = real_size
            by_path[key] = movie

        logging.info(f"[{instance['name']}] Found {with_files} movies with files")

    return list(by_path.values())


def delete_movie(movie: Dict, dry_run: bool = False) -> bool:
    """Delete a movie from every instance referencing it (removes files once)."""
    title = movie.get('title', 'Unknown')
    size_gb = movie['_size'] / 1e9
    refs = movie.get('_instances') or [{'instance': movie['_instance'], 'id': movie['id']}]

    if dry_run:
        names = ', '.join(r['instance']['name'] for r in refs)
        logging.info(f"[DRY-RUN] Would delete movie: {title} ({size_gb:.2f} GB) from {names}")
        return True

    ok = False
    for ref in refs:
        instance = ref['instance']
        response = radarr_request(
            instance,
            f"/movie/{ref['id']}",
            method='DELETE',
            params={'deleteFiles': 'true', 'addImportExclusion': 'false'}
        )
        if response and response.status_code in [200, 202, 204]:
            logging.info(f"Deleted movie: {title} ({size_gb:.2f} GB) from {instance['name']}")
            ok = True
        else:
            logging.error(f"Failed to delete movie {title} from {instance['name']}")
    return ok


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
    """Get all TV series from every Sonarr instance, de-duplicated by folder path."""
    by_path: Dict[str, Dict] = {}

    for instance in SONARR_INSTANCES:
        response = sonarr_request(instance, '/series')
        if not (response and response.status_code == 200):
            logging.error(f"[{instance['name']}] Failed to fetch series")
            continue

        series_list = response.json()
        with_files = 0
        for series in series_list:
            path = (series.get('path') or '').rstrip('/')
            key = path or f"{instance['name']}:{series['id']}"
            ref = {'instance': instance, 'id': series['id']}

            if key in by_path:
                by_path[key]['_instances'].append(ref)
                continue

            # Real disk size (accurate after Tdarr), counted once per folder.
            real_size = get_real_size(path)
            if real_size == 0:
                real_size = series.get('statistics', {}).get('sizeOnDisk', 0)
            if real_size <= 0:
                continue

            with_files += 1
            series['_instances'] = [ref]
            series['_instance'] = instance          # primary (back-compat)
            series['_instance_name'] = instance['name']
            series['_size'] = real_size
            by_path[key] = series

        logging.info(f"[{instance['name']}] Found {with_files} series with files")

    return list(by_path.values())


def delete_series(series: Dict, dry_run: bool = False) -> bool:
    """Delete a series from every instance referencing it (removes files once)."""
    title = series.get('title', 'Unknown')
    size_gb = series['_size'] / 1e9
    refs = series.get('_instances') or [{'instance': series['_instance'], 'id': series['id']}]

    if dry_run:
        names = ', '.join(r['instance']['name'] for r in refs)
        logging.info(f"[DRY-RUN] Would delete series: {title} ({size_gb:.2f} GB) from {names}")
        return True

    ok = False
    for ref in refs:
        instance = ref['instance']
        response = sonarr_request(
            instance,
            f"/series/{ref['id']}",
            method='DELETE',
            params={'deleteFiles': 'true', 'addImportListExclusion': 'false'}
        )
        if response and response.status_code in [200, 202, 204]:
            logging.info(f"Deleted series: {title} ({size_gb:.2f} GB) from {instance['name']}")
            ok = True
        else:
            logging.error(f"Failed to delete series {title} from {instance['name']}")
    return ok


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
        watch_info = lookup_watch_info(watch_data, movie.get('title', ''), movie.get('year'))

        last_watched = watch_info.get('last_watched', 0)
        play_count = watch_info.get('play_count', 0)

        # Parse added date
        added_str = movie.get('added', '')
        try:
            added_ts = int(datetime.fromisoformat(added_str.replace('Z', '+00:00')).timestamp()) if added_str else 0
        except (ValueError, TypeError, AttributeError):
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
        watch_info = lookup_watch_info(watch_data, series_item.get('title', ''), series_item.get('year'))

        last_watched = watch_info.get('last_watched', 0)
        play_count = watch_info.get('play_count', 0)

        added_str = series_item.get('added', '')
        try:
            added_ts = int(datetime.fromisoformat(added_str.replace('Z', '+00:00')).timestamp()) if added_str else 0
        except (ValueError, TypeError, AttributeError):
            added_ts = 0

        reference_date = last_watched if last_watched > 0 else added_ts

        media_list.append({
            'type': 'series',
            'id': series_item['id'],
            'title': series_item.get('title', 'Unknown'),
            'tmdb_id': series_item.get('tmdbId'),
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
