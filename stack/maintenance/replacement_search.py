"""A targeted search after a queue row is removed with its release blocklisted.

Radarr and Sonarr already search again by themselves after such a removal
when their "Redownload failed downloads" setting (autoRedownloadFailed) is
on, which is their default and the reference host's setting: searching here
as well would query the indexers twice for the same title. So the setting is
read first, once per instance and run, and this script only searches when it
is off, for at most REPLACEMENT_SEARCH_BUDGET items per run: the indexers'
daily allowance is shared with update-wanted.
"""
import logging

from maintenance.arr_api import ApiError, call, get_json

log = logging.getLogger(__name__)

REPLACEMENT_SEARCH_BUDGET = 10


def search_payload(instance, rows):
    """The search command for the media of these queue rows (one download), or None."""
    if instance.kind == 'radarr':
        movie_ids = sorted({row['movieId'] for row in rows if row.get('movieId')})
        return {'name': 'MoviesSearch', 'movieIds': movie_ids} if movie_ids else None
    episode_ids = sorted({row['episodeId'] for row in rows if row.get('episodeId')})
    return {'name': 'EpisodeSearch', 'episodeIds': episode_ids} if episode_ids else None


class ReplacementSearches:
    """The replacement searches of one run, within the budget."""

    def __init__(self, failures, budget=REPLACEMENT_SEARCH_BUDGET):
        self.failures = failures
        self.budget = budget
        self.used = 0
        self._app_searches = {}

    def app_searches_itself(self, instance):
        """autoRedownloadFailed of the instance; None when it cannot be read."""
        if instance not in self._app_searches:
            try:
                config = get_json(instance, 'config/downloadclient')
                value = config.get('autoRedownloadFailed') if isinstance(config, dict) else None
                if not isinstance(value, bool):
                    raise ApiError('config/downloadclient has no autoRedownloadFailed')
            except ApiError as error:
                # Unknown: searching blind could double the app's own search.
                self.failures.record(
                    f'[{instance}] cannot tell whether it re-searches after a blocklist', error)
                value = None
            self._app_searches[instance] = value
        return self._app_searches[instance]

    def after_blocklist(self, instance, rows, desc):
        """Search a replacement for the media of `rows`, unless the app does it itself."""
        if self.app_searches_itself(instance) is not False:
            return
        payload = search_payload(instance, rows)
        if payload is None:
            log.info('[%s] no movie/episode to search a replacement for: %s', instance, desc)
            return
        if self.used >= self.budget:
            log.info('[%s] replacement search budget (%d) used up, left to update-wanted: %s',
                     instance, self.budget, desc)
            return
        try:
            call(instance, 'POST', 'command', payload=payload)
        except ApiError as error:
            self.failures.record(f'[{instance}] cannot search a replacement for {desc}', error)
            return
        self.used += 1
        log.info('[%s] replacement search triggered: %s', instance, desc)
