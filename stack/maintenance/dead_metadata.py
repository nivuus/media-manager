"""Library entries whose metadata was deleted upstream (TMDb for Radarr, TheTVDB for Sonarr).

Radarr/Sonarr mark them with status='deleted' and spam refresh errors
forever. Entries WITH files on disk are only logged, never removed here:
deciding what to do with orphaned media stays a human call.
"""
import logging

import requests

from maintenance.arr_api import call

log = logging.getLogger(__name__)


def remove_dead_entries(instances):
    """Remove the file-less entries whose upstream metadata is gone."""
    for instance in instances:
        if instance.kind == 'radarr':
            _remove_dead_movies(instance)
        else:
            _remove_dead_series(instance)


def _remove_dead_movies(instance):
    try:
        response = call(instance, 'GET', 'movie')
    except requests.exceptions.RequestException as error:
        log.error('[%s] cannot list the movies: %s', instance, error)
        return
    for movie in response.json():
        if movie.get('status') != 'deleted':
            continue
        desc = f"{movie.get('title')} (tmdb {movie.get('tmdbId')})"
        if movie.get('hasFile'):
            log.info('[%s] metadata deleted from TMDb but the file is present, kept: %s',
                     instance, desc)
            continue
        try:
            call(instance, 'DELETE', f"movie/{movie['id']}",
                 params={'deleteFiles': False, 'addImportExclusion': False})
            log.info('[%s] removed entry deleted from TMDb: %s', instance, desc)
        except requests.exceptions.RequestException as error:
            log.error('[%s] cannot remove %s: %s', instance, desc, error)


def _remove_dead_series(instance):
    try:
        response = call(instance, 'GET', 'series')
    except requests.exceptions.RequestException as error:
        log.error('[%s] cannot list the series: %s', instance, error)
        return
    for series in response.json():
        if series.get('status') != 'deleted':
            continue
        desc = f"{series.get('title')} (tvdb {series.get('tvdbId')})"
        if series.get('statistics', {}).get('sizeOnDisk', 0) > 0:
            log.info('[%s] metadata deleted from TheTVDB but files are present, kept: %s',
                     instance, desc)
            continue
        try:
            call(instance, 'DELETE', f"series/{series['id']}",
                 params={'deleteFiles': False})
            log.info('[%s] removed entry deleted from TheTVDB: %s', instance, desc)
        except requests.exceptions.RequestException as error:
            log.error('[%s] cannot remove %s: %s', instance, desc, error)
