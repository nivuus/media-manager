#!/usr/bin/env python3
"""update_wanted's selection and run, checked against a fake Radarr/Sonarr API.

The fake (maintenance_fakes.py) stands in for requests.request, the network
boundary: no case here ever reaches the network. The fake clock stands in for
the module's `time`, so the DELAY_BETWEEN_INSTANCES sleep never waits either.

The rule that motivated this file (audit, 2026-09-28): a random.sample() over
the backlog needed a median of 96 days to cover Sonarr's 1226 missing episodes
once; ordering the backlog by lastSearchTime ascending instead needs 14, since
every run advances the whole backlog rather than a lucky subset of it. Radarr's
wanted/missing sorts by movieMetadata.sortTitle regardless of the sortKey
asked for, so "newest first" cannot come from the fetch: select_batch() is
what the tests below hold to that order.

Run: python3 tests/test_update_wanted.py
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stack"))

import requests  # noqa: E402

from maintenance import update_wanted  # noqa: E402
from maintenance.arr_api import Instance  # noqa: E402
from maintenance_fakes import FakeApi, FakeClock, LogCapture, reply  # noqa: E402

RADARR = "http://radarr.test:7878/api/v3"
SONARR = "http://sonarr.test:8989/api/v3"
ENV = {
    "RADARR_URL": "http://radarr.test:7878", "RADARR_API_KEY": "radarr-key",
    "SONARR_URL": "http://sonarr.test:8989", "SONARR_API_KEY": "sonarr-key",
}
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
CAPTURE = LogCapture().install()

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def days_ago(n):
    return NOW - timedelta(days=n)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def movie(record_id, released=None, searched=None):
    record = {"id": record_id, "title": f"Movie {record_id}", "monitored": True}
    if released is not None:
        record["digitalRelease"] = iso(released)
    if searched is not None:
        record["lastSearchTime"] = iso(searched)
    return record


def episode(record_id, aired=None, searched=None):
    record = {"id": record_id, "seriesTitle": f"Show {record_id}", "monitored": True}
    if aired is not None:
        record["airDateUtc"] = iso(aired)
    if searched is not None:
        record["lastSearchTime"] = iso(searched)
    return record


def missing_page(records, total=None):
    return {"page": 1, "pageSize": 500,
            "totalRecords": len(records) if total is None else total,
            "records": records}


def paged(records, page_size):
    """A wanted/missing route split into page_size-sized pages, whatever the request asks."""
    total = len(records)

    def answer(params=None, **_):
        page = params["page"]
        start = (page - 1) * page_size
        batch = records[start:start + page_size]
        return reply(200, {"page": page, "pageSize": page_size,
                           "totalRecords": total, "records": batch})
    return answer


def routes(radarr_missing=(), sonarr_missing=()):
    return {
        ("GET", f"{SONARR}/wanted/missing"): reply(200, missing_page(list(sonarr_missing))),
        ("GET", f"{RADARR}/wanted/missing"): reply(200, missing_page(list(radarr_missing))),
        ("POST", f"{SONARR}/command"):
            reply(201, {"id": 1, "name": "EpisodeSearch", "status": "queued"}),
        ("POST", f"{RADARR}/command"):
            reply(201, {"id": 2, "name": "MoviesSearch", "status": "queued"}),
    }


def run_module(table, clock=None, environ=None):
    """One update_wanted.run(); returns (exit status, fake API, fake clock)."""
    api = FakeApi(table)
    clock = clock or FakeClock()
    CAPTURE.lines.clear()
    with mock.patch("requests.request", new=api), \
         mock.patch("maintenance.update_wanted.time", new=clock):
        try:
            code = update_wanted.run(dict(ENV if environ is None else environ))
        except Exception as error:  # a crash fails the case, not the whole file
            code = f"raised {error!r}"
    return code, api, clock


# --- select_batch: recent items first, newest first, ties by id -----------
records = [
    movie(3, released=days_ago(20)),
    movie(1, released=days_ago(5)),
    movie(4, released=days_ago(5)),   # same date as id 1: tie broken by id
    movie(2, released=days_ago(29)),  # still within the 30-day window
    movie(5, released=days_ago(400)),  # outside the window: backlog
]
recent, backlog = update_wanted.select_batch(records, "radarr", now=NOW)
check("recent first, newest first: order", [r["id"] for r in recent], [1, 4, 3, 2])
check("recent first, newest first: rest goes to the backlog",
      [r["id"] for r in backlog], [5])

# --- select_batch: backlog by lastSearchTime ascending, missing first -----
records = [
    movie(10, released=days_ago(400), searched=days_ago(2)),
    movie(11, released=days_ago(400)),                      # never searched
    movie(12, released=days_ago(400), searched=days_ago(10)),
    movie(13, released=days_ago(400)),                      # never searched, tie with 11
]
_, backlog = update_wanted.select_batch(records, "radarr", now=NOW)
check("backlog: never-searched first (ties by id), then oldest lastSearchTime",
      [r["id"] for r in backlog], [11, 13, 12, 10])

# --- select_batch: capped at MAX_SEARCH_PER_INSTANCE, in total ------------
lots_recent = [movie(i, released=days_ago(1)) for i in range(1, 151)]
some_backlog = [movie(i, released=days_ago(400)) for i in range(1000, 1010)]
recent, backlog = update_wanted.select_batch(lots_recent + some_backlog, "radarr", now=NOW)
check("cap: recent alone already fills the budget", len(recent), 100)
check("cap: nothing left for the backlog", len(backlog), 0)

few_recent = [movie(i, released=days_ago(1)) for i in range(1, 31)]
much_backlog = [movie(i, released=days_ago(400)) for i in range(1000, 1150)]
recent, backlog = update_wanted.select_batch(few_recent + much_backlog, "radarr", now=NOW)
check("cap: recent all included when under budget", len(recent), 30)
check("cap: backlog fills exactly what recent left", len(backlog), 70)
check("cap: total never exceeds MAX_SEARCH_PER_INSTANCE",
      len(recent) + len(backlog), update_wanted.MAX_SEARCH_PER_INSTANCE)

# --- is_searchable: unreleased/unaired is skipped --------------------------
check("is_searchable: released in the past",
      update_wanted.is_searchable(movie(1, released=days_ago(1)), "radarr", now=NOW), True)
check("is_searchable: released in the future is skipped",
      update_wanted.is_searchable(movie(2, released=NOW + timedelta(days=5)), "radarr", now=NOW),
      False)
check("is_searchable: no release date is skipped",
      update_wanted.is_searchable({"id": 3}, "radarr", now=NOW), False)

# --- fetch_missing: pagination collects every page -------------------------
RADARR_INSTANCE = Instance("radarr", "Radarr", "http://radarr.test:7878",
                           "radarr-key", "RADARR_API_KEY")
records = [movie(i) for i in range(1, 6)]
with mock.patch("requests.request", new=FakeApi(
        {("GET", f"{RADARR}/wanted/missing"): paged(records, page_size=2)})):
    fetched = update_wanted.fetch_missing(RADARR_INSTANCE)
check("pagination: every record collected across pages",
      [r["id"] for r in fetched], [1, 2, 3, 4, 5])

# --- run(): exit 0, both instances searched, in order, with a sleep between
code, api, clock = run_module(routes(
    radarr_missing=[movie(1, released=days_ago(1))],
    sonarr_missing=[episode(101, aired=days_ago(1))]))
check("run: exit status", code, 0)
check("run: sonarr searched before radarr (the kept instance order)",
      [url for _, url, _ in api.calls if url.endswith("/wanted/missing")],
      [f"{SONARR}/wanted/missing", f"{RADARR}/wanted/missing"])
check("run: one sleep between the two instances",
      clock.sleeps, [update_wanted.DELAY_BETWEEN_INSTANCES])
check("run: sonarr search payload",
      [kwargs["json"] for kwargs in api.made("POST", f"{SONARR}/command")],
      [{"name": "EpisodeSearch", "episodeIds": [101]}])
check("run: radarr search payload",
      [kwargs["json"] for kwargs in api.made("POST", f"{RADARR}/command")],
      [{"name": "MoviesSearch", "movieIds": [1]}])

# --- run(): no missing items anywhere is not a failure ---------------------
code, api, _ = run_module(routes())
check("run: nothing missing, exit status", code, 0)
check("run: nothing missing, no search triggered",
      api.made("POST", f"{SONARR}/command") + api.made("POST", f"{RADARR}/command"), [])

# --- run(): a fetch failure fails the run, the other instance still tried -
FETCH_FAILURES = [
    ("HTTP 500", reply(500, {"message": "boom"})),
    ("connection refused",
     requests.exceptions.ConnectionError("[Errno 111] Connection refused")),
]
for label, answer in FETCH_FAILURES:
    table = routes(sonarr_missing=[episode(101, aired=days_ago(1))])
    table[("GET", f"{RADARR}/wanted/missing")] = answer
    code, api, _ = run_module(table)
    check(f"{label}: exit status", code, 1)
    check(f"{label}: the other instance was still searched",
          len(api.made("POST", f"{SONARR}/command")), 1)

# The first instance in the kept order (Sonarr) failing must not stop Radarr,
# and the inter-instance delay still applies.
table = routes(radarr_missing=[movie(1, released=days_ago(1))])
table[("GET", f"{SONARR}/wanted/missing")] = reply(500, {"message": "boom"})
code, api, clock = run_module(table)
check("first instance fails: exit status", code, 1)
check("first instance fails: second instance still searched",
      len(api.made("POST", f"{RADARR}/command")), 1)
check("first instance fails: the delay still happened",
      clock.sleeps, [update_wanted.DELAY_BETWEEN_INSTANCES])

# --- run(): a search failure fails the run too ------------------------------
table = routes(radarr_missing=[movie(1, released=days_ago(1))])
table[("POST", f"{RADARR}/command")] = reply(500, {"message": "boom"})
code, _, _ = run_module(table)
check("search fails: exit status", code, 1)

# --- run(): missing API keys fails closed, before any call ------------------
no_keys = {"RADARR_URL": ENV["RADARR_URL"], "SONARR_URL": ENV["SONARR_URL"]}
code, api, _ = run_module(routes(), environ=no_keys)
check("missing keys: exit status", code, 1)
check("missing keys: no calls made", api.calls, [])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_update_wanted: OK")
