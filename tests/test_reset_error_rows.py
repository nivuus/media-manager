#!/usr/bin/env python3
"""What reset-error sends for each queue row, checked against a fake Radarr/Sonarr API.

These are the destructive or irreversible calls, so their parameters are
pinned exactly: which files a manual import sends and how, which rows are
removed, with or without a blocklist, and that a season pack, one download
spread over several rows, is removed once. The fake (maintenance_fakes.py)
stands in for requests.request; the Downloads tree is real, in a temporary
directory, since it decides which downloads are still on disk.

Run: python3 tests/test_reset_error_rows.py
"""
import sys
import tempfile

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True

from maintenance_fakes import reply  # noqa: E402
from reset_error_fixtures import (  # noqa: E402
    CANDIDATES, DOWNLOADING, IMPORT_PENDING, PENDING, RADARR, SONARR, STALLED,
    case, commands, import_routes, iso, make_downloads, routes, run)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# The parameters of a removal that blocklists the release, and of one that
# does not (its files are gone, the release was fine).
BLOCKLIST = {"removeFromClient": True, "blocklist": True, "skipRedownload": False}
FILES_GONE = {"removeFromClient": True, "blocklist": False, "skipRedownload": True}

# --- Files gone: decided from the Downloads directory ---------------------
# An import-pending row whose download is no longer on disk is removed from
# the client WITHOUT blocklisting: the release was fine, its files vanished.
with tempfile.TemporaryDirectory() as tmp, case("files gone", failures):
    downloads = make_downloads(tmp)  # holds PENDING, not VANISHED
    table = import_routes()
    table[("GET", f"{RADARR}/manualimport")] = reply(200, CANDIDATES)
    code, api, _ = run(table, downloads)
    check("files gone: exit status", code, 0)
    check("files gone: removal parameters",
          [kwargs["params"] for kwargs in api.made("DELETE", f"{RADARR}/queue/104")],
          [FILES_GONE])
    lookups = [kwargs["params"] for kwargs in api.made("GET", f"{RADARR}/manualimport")]
    # The download still on disk goes through the manual import instead;
    # the vanished one is not looked up.
    check("download present: manual import looked up", lookups,
          [{"downloadId": IMPORT_PENDING["downloadId"], "filterExistingFiles": True}])
    imports = commands(api, RADARR, "ManualImport")
    check("download present: import command sent", len(imports), 1)
    if imports:
        check("download present: imported in auto mode", imports[0]["importMode"], "auto")
        check("download present: file imported as its movie",
              [(f["path"], f["movieId"]) for f in imports[0]["files"]],
              [(f"/data/Downloads/radarr/{PENDING}/movie.mkv", 813)])
    check("download present: row not removed",
          api.made("DELETE", f"{RADARR}/queue/103"), [])
    # The manual import makes the app scan and ffprobe the download: it
    # alone gets the longer timeout.
    check("manual import: (10, 120) on its lookup and its command",
          [(method, url, (kwargs.get("json") or {}).get("name"))
           for method, url, kwargs in api.calls if kwargs.get("timeout") == (10, 120)],
          [("GET", f"{RADARR}/manualimport", None),
           ("POST", f"{RADARR}/command", "ManualImport")])
    check("manual import: (10, 60) on every other call",
          {kwargs.get("timeout") for _, _, kwargs in api.calls
           if kwargs.get("timeout") != (10, 120)}, {(10, 60)})

# --- A candidate a rejection rules out is never imported -------------------
# Importing it would be wrong, whatever the match: a sample, a file whose
# runtime ffprobe could not read, a downgrade. A dated row then waits.
REJECTED = [
    ("sample", "Sample"),
    ("sample undetermined", "Unable to determine if file is a sample"),
    ("not an upgrade", "Not an upgrade for existing movie file(s)"),
]
for label, reason in REJECTED:
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp)
        table = routes(radarr_queue=[DOWNLOADING, dict(IMPORT_PENDING, added=iso(5))])
        table[("GET", f"{RADARR}/manualimport")] = reply(200, [dict(
            CANDIDATES[0], rejections=[{"reason": reason, "type": "permanent"}])])
        table[("DELETE", f"{RADARR}/queue/103")] = reply(200, {})
        code, api, _ = run(table, downloads)
        check(f"{label}: exit status", code, 0)
        check(f"{label}: looked up", len(api.made("GET", f"{RADARR}/manualimport")), 1)
        check(f"{label}: not imported", commands(api, RADARR, "ManualImport"), [])
        check(f"{label}: row kept", api.made("DELETE", f"{RADARR}/queue/103"), [])

# --- A Sonarr candidate is imported as its episodes ------------------------
# A file Sonarr could not match to an episode is left out: importing it
# would file the release under the wrong number.
SEASON = "Some.Show.S03.1080p.WEB.h264-GRP"
SEASON_ID = "F60718293A4B5C6D7E8F901234567890ABCDEF12"
SONARR_PENDING = {
    "id": 204, "seriesId": 77, "episodeId": 9101, "title": SEASON,
    "status": "completed", "trackedDownloadStatus": "warning",
    "trackedDownloadState": "importPending",
    "statusMessages": [{"title": SEASON, "messages": [
        "Found matching series via grab history, but release was matched to "
        "series by ID. Automatic import is not possible."]}],
    "downloadId": SEASON_ID, "protocol": "torrent",
    "downloadClient": "RDTClient", "indexer": "YggTorrent",
    "outputPath": f"/data/Downloads/tv-sonarr/{SEASON}",
    "size": 12_000_000_000, "sizeleft": 0, "added": iso(5),
    "episode": {"seasonNumber": 3, "episodeNumber": 1},
}
QUALITY = {"quality": {"id": 3, "name": "WEBDL-1080p"},
           "revision": {"version": 1, "real": 0, "isRepack": False}}
LANGUAGES = [{"id": 2, "name": "French"}]
EPISODE_FILE = f"/data/Downloads/tv-sonarr/{SEASON}/Some.Show.S03E01.1080p.WEB.h264-GRP.mkv"
SONARR_CANDIDATES = [
    {"id": 1, "path": EPISODE_FILE, "series": {"id": 77, "title": "Some Show"},
     "seasonNumber": 3, "episodes": [{"id": 9101, "seasonNumber": 3, "episodeNumber": 1}],
     "quality": QUALITY, "languages": LANGUAGES, "releaseGroup": "GRP",
     "downloadId": SEASON_ID, "rejections": []},
    {"id": 2, "path": f"/data/Downloads/tv-sonarr/{SEASON}/Some.Show.S03.Bonus.mkv",
     "series": {"id": 77, "title": "Some Show"}, "seasonNumber": 3, "episodes": [],
     "quality": QUALITY, "languages": LANGUAGES, "releaseGroup": "GRP",
     "downloadId": SEASON_ID, "rejections": []},
]
with tempfile.TemporaryDirectory() as tmp, case("sonarr import", failures):
    downloads = make_downloads(tmp)
    (downloads / "tv-sonarr" / SEASON).mkdir()
    (downloads / "tv-sonarr" / SEASON / "episode.mkv").write_bytes(b"\0" * 16)
    table = routes(sonarr_queue=[SONARR_PENDING])
    table[("GET", f"{SONARR}/manualimport")] = reply(200, SONARR_CANDIDATES)
    table[("DELETE", f"{SONARR}/queue/204")] = reply(200, {})
    code, api, _ = run(table, downloads)
    check("sonarr import: exit status", code, 0)
    check("sonarr import: command sent", commands(api, SONARR, "ManualImport"), [{
        "name": "ManualImport", "importMode": "auto", "files": [{
            "path": EPISODE_FILE, "quality": QUALITY, "languages": LANGUAGES,
            "releaseGroup": "GRP", "downloadId": SEASON_ID,
            "seriesId": 77, "episodeIds": [9101]}]}])
    check("sonarr import: row not removed", api.made("DELETE", f"{SONARR}/queue/204"), [])

# --- A season pack is removed once ------------------------------------------
# Its rows share one downloadId: removing the first removes the download, and
# a second DELETE would 404 on the sibling row.
SIBLING = dict(STALLED, id=203, episodeId=9002,
               episode={"seasonNumber": 2, "episodeNumber": 4})
with tempfile.TemporaryDirectory() as tmp, case("season pack", failures):
    downloads = make_downloads(tmp)
    table = routes(sonarr_queue=[STALLED, SIBLING])
    table[("DELETE", f"{SONARR}/queue/203")] = reply(200, {})
    code, api, _ = run(table, downloads)
    check("season pack: exit status", code, 0)
    check("season pack: removed once, blocklisted",
          [kwargs["params"] for kwargs in api.made("DELETE", f"{SONARR}/queue/202")],
          [BLOCKLIST])
    check("season pack: sibling row not removed again",
          api.made("DELETE", f"{SONARR}/queue/203"), [])

# --- A ghost row is removed and blocklisted ---------------------------------
# No 'added' date and nothing importable: every age rule needs a date, so
# keeping it would be forever. Removed with a blocklist, so the next search
# picks another release.
with tempfile.TemporaryDirectory() as tmp, case("ghost", failures):
    downloads = make_downloads(tmp)  # holds PENDING: not a files-gone case
    table = routes(radarr_queue=[DOWNLOADING, IMPORT_PENDING])
    table[("GET", f"{RADARR}/manualimport")] = reply(200, [])
    table[("DELETE", f"{RADARR}/queue/103")] = reply(200, {})
    code, api, _ = run(table, downloads)
    check("ghost: exit status", code, 0)
    check("ghost: nothing imported", commands(api, RADARR, "ManualImport"), [])
    check("ghost: removed and blocklisted",
          [kwargs["params"] for kwargs in api.made("DELETE", f"{RADARR}/queue/103")],
          [BLOCKLIST])

# --- A local problem keeps its row whatever the manual import finds ---------
# Without a date and with nothing importable it is not a ghost: fixing the
# problem is what gets it imported.
DENIED = "Denied.Movie.2018.MULTi.1080p.WEB.x264-GRP"
IMPORT_DENIED = dict(
    IMPORT_PENDING, id=105, movieId=815, title=DENIED,
    downloadId="E5F60718293A4B5C6D7E8F901234567890ABCDEF",
    outputPath=f"/data/Downloads/radarr/{DENIED}",
    statusMessages=[{"title": DENIED, "messages": [
        "Failed to import movie, Permission denied"]}])
with tempfile.TemporaryDirectory() as tmp, case("local problem", failures):
    downloads = make_downloads(tmp)
    # A folder with its file: an empty one would be purged as empty, and a
    # download without files is rightly taken for a vanished one.
    (downloads / "radarr" / DENIED).mkdir()
    (downloads / "radarr" / DENIED / "movie.mkv").write_bytes(b"\0" * 16)
    table = routes(radarr_queue=[DOWNLOADING, IMPORT_DENIED])
    table[("GET", f"{RADARR}/manualimport")] = reply(200, [])
    table[("DELETE", f"{RADARR}/queue/105")] = reply(200, {})
    code, api, _ = run(table, downloads)
    check("local problem: exit status", code, 0)
    check("local problem: manual import attempted",
          len(api.made("GET", f"{RADARR}/manualimport")), 1)
    check("local problem: row kept", api.made("DELETE", f"{RADARR}/queue/105"), [])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_reset_error_rows: OK")
