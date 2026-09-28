"""Daily search for Radarr/Sonarr's missing movies and episodes.

Run by media-manager-update-wanted.service through stack/update_wanted.py,
which only calls main(): this module is what the tests import.

Searching every missing item on every run emptied the indexers' daily API
allowance in minutes (429 TooManyRequests, Prowlarr disabling them for
hours), so the searches that mattered found nothing. Instead each run
searches a bounded, deterministic slice per instance: see select_batch()
for how the slice is chosen. Any API failure is recorded, the run does what
it still safely can, and the exit status is 1 so that systemd marks the
unit failed.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from maintenance import run_log
from maintenance.arr_api import (ApiError, Failures, call, check_api_keys,
                                 exit_status, get_page, parse_time,
                                 radarr_instances, sonarr_instances)

log = logging.getLogger(__name__)

# Searching everything at once is what triggered the indexer 429s (see the
# module docstring): a bounded, deterministic slice per run instead.
MAX_SEARCH_PER_INSTANCE = 100
# Anything aired/released within this window is searched on EVERY run: that is
# where a release still has a real chance of surfacing. The rest of the budget
# goes to the backlog.
#
# Measured on 2026-09-16, Sonarr's 1224 missing episodes: 970 aired more than
# ten years ago (532 of them one single show), and 6 within the last thirty
# days. A random draw over the backlog needed a median of 96 days to cover all
# 1226 episodes once; ordering it by lastSearchTime ascending instead needs
# 14, since every run advances the whole backlog rather than a lucky subset
# of it, with no state file to keep in sync between runs.
RECENT_WINDOW_DAYS = 30
# Seconds between instances, so they don't hit the same indexers through
# Prowlarr at the same second.
DELAY_BETWEEN_INSTANCES = 120


def fetch_missing(instance):
    """Every monitored missing item of an instance, in the API's own order.

    Radarr's wanted/missing sorts by movieMetadata.sortTitle and Sonarr's by
    episodes.airDateUtc descending, regardless of what sortKey is requested:
    neither is an order this script can rely on, so select_batch() below
    always sorts explicitly instead of trusting the fetch order. Raises
    ApiError when a page cannot be read, is not a list of objects, or the
    total is missing: a partial answer here would silently starve part of
    the backlog instead of failing loudly.
    """
    records = []
    page = 1
    while True:
        batch, total = get_page(instance, 'wanted/missing',
                                params={'page': page, 'pageSize': 500, 'monitored': True})
        records.extend(batch)
        if not batch or len(records) >= total:
            break
        page += 1
    return records


def release_date(record, kind):
    """When the item aired (Sonarr) or was released (Radarr), or None."""
    if kind == 'sonarr':
        raw = record.get('airDateUtc')
    else:
        raw = (record.get('digitalRelease') or record.get('physicalRelease')
              or record.get('inCinemas'))
    return parse_time(raw)


def last_search_time(record):
    """When Radarr/Sonarr last searched this item, or None: the same field on both apps."""
    return parse_time(record.get('lastSearchTime'))


def is_searchable(record, kind, now=None):
    """Skip what cannot possibly be found yet: nothing has been released.

    Searching an unreleased title every single day is pure indexer quota burnt
    for a guaranteed zero result.
    """
    released = release_date(record, kind)
    return released is not None and released <= (now or datetime.now(timezone.utc))


def select_batch(records, kind, now=None):
    """Choose up to MAX_SEARCH_PER_INSTANCE items to search this run.

    Assumes `records` was already filtered to what is_searchable(): a
    future-dated record would otherwise land in "recent" too, since the only
    check made here is `released >= cutoff`, not `released <= now`.

    The recent items (released/aired within RECENT_WINDOW_DAYS) come first,
    newest first; the rest of the budget is filled from the backlog, ordered
    by lastSearchTime ascending with items never searched first, so every
    missing item eventually gets its turn (see the module docstring). Both
    groups break ties on the record id, so the same input always yields the
    same selection: stateless and deterministic, unlike the random.sample()
    this replaces.

    Returns (recent, backlog), each already capped, so the caller can log
    the split.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RECENT_WINDOW_DAYS)
    recent, backlog = [], []
    for record in records:
        released = release_date(record, kind)
        (recent if released and released >= cutoff else backlog).append(record)

    # Two stable sorts: ascending id first, then descending date. Python's
    # sort is stable, so the id order survives among equal dates, breaking
    # ties on it deterministically.
    recent.sort(key=lambda r: r['id'])
    recent.sort(key=lambda r: release_date(r, kind), reverse=True)
    recent = recent[:MAX_SEARCH_PER_INSTANCE]

    never_searched = datetime.min.replace(tzinfo=timezone.utc)
    backlog.sort(key=lambda r: (last_search_time(r) or never_searched, r['id']))
    budget_left = MAX_SEARCH_PER_INSTANCE - len(recent)
    backlog = backlog[:budget_left]
    return recent, backlog


def trigger_search(instance, records):
    """Search exactly the given items, instead of the whole missing list."""
    if instance.kind == 'sonarr':
        payload = {'name': 'EpisodeSearch', 'episodeIds': [r['id'] for r in records]}
    else:
        payload = {'name': 'MoviesSearch', 'movieIds': [r['id'] for r in records]}
    call(instance, 'POST', 'command', payload=payload)


def search_instance(instance, failures, now):
    """Fetch one instance's missing list and search the selected slice.

    Failures during the fetch or the search are recorded rather than raised:
    the caller must still try every other instance.
    """
    try:
        missing = fetch_missing(instance)
    except ApiError as error:
        failures.record(f'[{instance}] cannot fetch the missing list', error)
        return

    if not missing:
        log.info('[%s] no missing items.', instance)
        return

    searchable = [r for r in missing if is_searchable(r, instance.kind, now)]
    skipped = len(missing) - len(searchable)
    if not searchable:
        log.info('[%s] %d missing, none released/aired yet.', instance, len(missing))
        return

    recent, backlog = select_batch(searchable, instance.kind, now)
    selected = recent + backlog
    log.info('[%s] %d missing (%d not yet released/aired) -> searching %d '
             '(%d recent < %d days, %d from the backlog).',
             instance, len(missing), skipped, len(selected),
             len(recent), RECENT_WINDOW_DAYS, len(backlog))

    try:
        trigger_search(instance, selected)
    except ApiError as error:
        failures.record(f'[{instance}] cannot trigger the search', error)
        return
    log.info('[%s] search triggered.', instance)


def run(environ):
    """One run: search a bounded, deterministic slice of each instance's missing list."""
    instances = sonarr_instances(environ) + radarr_instances(environ)
    if not check_api_keys(instances):
        return 1

    failures = Failures()
    now = datetime.now(timezone.utc)
    for index, instance in enumerate(instances):
        if index:
            time.sleep(DELAY_BETWEEN_INSTANCES)
        search_instance(instance, failures, now)

    return exit_status(failures)


def main():
    """The unit's entry point: run_log's shared sequence, logging to update-wanted.log."""
    return run_log.entry_point('update-wanted', run)
