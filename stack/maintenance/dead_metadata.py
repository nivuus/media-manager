"""Library entries whose metadata was deleted upstream (TMDb for Radarr, TheTVDB for Sonarr).

Radarr/Sonarr mark them with status='deleted' and spam refresh errors
forever. Entries WITH files on disk are only logged, never removed here:
deciding what to do with orphaned media stays a human call.
"""
import logging

from maintenance.arr_api import ApiError, call, get_json

log = logging.getLogger(__name__)


def remove_dead_entries(instances, failures):
    """Remove the file-less entries whose upstream metadata is gone."""
    for instance in instances:
        if instance.kind == 'radarr':
            _remove_dead(
                instance, failures, 'movie', 'TMDb',
                describe=lambda movie: f"{movie.get('title')} (tmdb {movie.get('tmdbId')})",
                has_files=lambda movie: bool(movie.get('hasFile')),
                delete_params={'deleteFiles': False, 'addImportExclusion': False})
        else:
            _remove_dead(
                instance, failures, 'series', 'TheTVDB',
                describe=lambda series: f"{series.get('title')} (tvdb {series.get('tvdbId')})",
                has_files=lambda series: (series.get('statistics') or {}).get('sizeOnDisk', 0) > 0,
                delete_params={'deleteFiles': False})


def _remove_dead(instance, failures, resource, upstream, describe, has_files,
                 delete_params):
    try:
        entries = get_json(instance, resource)
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise ApiError(f'the {resource} answer is not a list of entries')
    except ApiError as error:
        failures.record(f'[{instance}] cannot list the {resource} entries', error)
        return
    for entry in entries:
        if entry.get('status') != 'deleted':
            continue
        desc = describe(entry)
        if has_files(entry):
            log.info('[%s] metadata deleted from %s but files are present, kept: %s',
                     instance, upstream, desc)
            continue
        try:
            call(instance, 'DELETE', f"{resource}/{entry['id']}", params=delete_params)
        except ApiError as error:
            failures.record(f'[{instance}] cannot remove {desc}', error)
            continue
        log.info('[%s] removed an entry deleted from %s: %s', instance, upstream, desc)
