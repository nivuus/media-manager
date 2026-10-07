#!/usr/bin/env python3
"""After a queue row is removed with its release blocklisted, reset-error
searches a replacement only when the app will not: Radarr/Sonarr re-search
by themselves when autoRedownloadFailed is on (their default), and a second
search would spend the indexers' quota twice.

Run: python3 tests/test_replacement_search.py
"""
import sys
import tempfile
from unittest import mock

sys.dont_write_bytecode = True

from reset_error_fixtures import (  # noqa: E402  (puts stack/ on sys.path)
    SONARR, STALLED, case, commands, make_downloads, routes, run)
from maintenance import replacement_search  # noqa: E402
from maintenance.arr_api import Failures, Instance  # noqa: E402
from maintenance_fakes import FakeApi, reply  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def app_redownloads(table, value):
    table[("GET", f"{SONARR}/config/downloadclient")] = reply(
        200, {"id": 1, "autoRedownloadFailed": value})
    return table


# --- The app re-searches by itself: nothing sent --------------------------
with tempfile.TemporaryDirectory() as tmp, case("app searches", failures):
    code, api, _ = run(app_redownloads(routes(), True), make_downloads(tmp))
    check("app searches: exit status", code, 0)
    check("app searches: row removed", len(api.made("DELETE", f"{SONARR}/queue/202")), 1)
    check("app searches: no search of ours", commands(api, SONARR, "EpisodeSearch"), [])

# --- It does not: the episode is searched right away ---------------------
with tempfile.TemporaryDirectory() as tmp, case("app does not", failures):
    code, api, _ = run(app_redownloads(routes(), False), make_downloads(tmp))
    check("app does not: exit status", code, 0)
    check("app does not: episode searched",
          commands(api, SONARR, "EpisodeSearch"),
          [{"name": "EpisodeSearch", "episodeIds": [STALLED["episodeId"]]}])

# --- Season pack: one search for every episode of the download ------------
second = dict(STALLED, id=203, episodeId=9002, episode={"seasonNumber": 2, "episodeNumber": 4})
with tempfile.TemporaryDirectory() as tmp, case("season pack", failures):
    code, api, _ = run(app_redownloads(routes(sonarr_queue=[STALLED, second]), False),
                       make_downloads(tmp))
    check("season pack: one search, both episodes",
          commands(api, SONARR, "EpisodeSearch"),
          [{"name": "EpisodeSearch", "episodeIds": [9001, 9002]}])

# --- The setting unreadable: a failure, and no blind search ---------------
with tempfile.TemporaryDirectory() as tmp, case("setting unreadable", failures):
    table = routes()
    table[("GET", f"{SONARR}/config/downloadclient")] = reply(500, {"message": "boom"})
    code, api, _ = run(table, make_downloads(tmp))
    check("setting unreadable: exit status", code, 1)
    check("setting unreadable: no search", commands(api, SONARR, "EpisodeSearch"), [])

# --- The budget, and the setting read once per instance --------------------
SONARR_INSTANCE = Instance("sonarr", "Sonarr", "http://sonarr.test:8989", "k", "SONARR_API_KEY")
api = FakeApi({
    ("GET", f"{SONARR}/config/downloadclient"): reply(200, {"autoRedownloadFailed": False}),
    ("POST", f"{SONARR}/command"): reply(201, {"id": 1}),
})
searches = replacement_search.ReplacementSearches(Failures(), budget=2)
with mock.patch("requests.request", new=api):
    for episode_id in (1, 2, 3):
        searches.after_blocklist(SONARR_INSTANCE, [{"episodeId": episode_id}], f"ep {episode_id}")
    searches.after_blocklist(SONARR_INSTANCE, [{"title": "unknown"}], "unknown row")
check("budget: two searches at most", len(api.made("POST", f"{SONARR}/command")), 2)
check("budget: setting read once", len(api.made("GET", f"{SONARR}/config/downloadclient")), 1)
check("no movie/episode: nothing to search",
      replacement_search.search_payload(SONARR_INSTANCE, [{"title": "x"}]), None)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_replacement_search: OK")
