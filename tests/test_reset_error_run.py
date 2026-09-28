#!/usr/bin/env python3
"""reset-error's run, checked against a fake Radarr/Sonarr API and a real
Downloads directory in a temporary tree.

The fake (maintenance_fakes.py) stands in for requests.request, the network
boundary. The purge is never mocked: whether it ran is read on the
filesystem, from an old file no queue references.

The rule that motivated this file (audit C2): with Radarr unreachable for
7 days, the purge ran with an empty protection set and deleted a completed,
unimported 36.8 GB download, while the run exited 0 every day, so the
unit's OnFailure= alert never fired.

Run: python3 tests/test_reset_error_run.py
"""
import contextlib
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
STACK = REPO / "stack"
sys.path.insert(0, str(STACK))

import requests  # noqa: E402

from maintenance import reset_error  # noqa: E402
from maintenance_fakes import FakeApi, LogCapture, reply  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# --- Fixtures -------------------------------------------------------------
RADARR = "http://radarr.test:7878/api/v3"
SONARR = "http://sonarr.test:8989/api/v3"
CAPTURE = LogCapture().install()


def iso(hours_ago):
    """An 'added' value as the API sends it: ISO 8601, UTC, 'Z' suffix."""
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def queue_page(records, total=None):
    """The paging resource /api/v3/queue answers with."""
    return {"page": 1, "pageSize": 1000, "sortKey": "timeleft",
            "sortDirection": "ascending",
            "totalRecords": len(records) if total is None else total,
            "records": records}


MOVIE = "Some.Movie.2021.MULTi.1080p.WEB.x264-GRP"
PENDING = "Other.Movie.2020.MULTi.1080p.BluRay.x264-GRP"
EPISODE = "Some.Show.S02E03.1080p.WEB.h264-GRP"
UNREFERENCED = "radarr/Old.Unreferenced.Movie.2019/movie.mkv"

# A Radarr row still downloading: kept, and its files protected from the purge.
DOWNLOADING = {
    "id": 101, "movieId": 812, "title": MOVIE,
    "status": "downloading", "trackedDownloadStatus": "ok",
    "trackedDownloadState": "downloading", "statusMessages": [],
    "downloadId": "A1B2C3D4E5F60718293A4B5C6D7E8F9012345678",
    "protocol": "torrent", "downloadClient": "RDTClient", "indexer": "YggTorrent",
    "outputPath": f"/data/Downloads/radarr/{MOVIE}",
    "size": 4_800_000_000, "sizeleft": 1_200_000_000, "added": iso(2),
}
# A Radarr row whose import is pending and that Radarr cannot date (no grab in
# its history): the manual import is what should happen to it.
IMPORT_PENDING = {
    "id": 103, "movieId": 813, "title": PENDING,
    "status": "completed", "trackedDownloadStatus": "warning",
    "trackedDownloadState": "importPending",
    "statusMessages": [{"title": PENDING, "messages": [
        "Movie title mismatch, automatic import is not possible. "
        "Manual Import required."]}],
    "downloadId": "C3D4E5F60718293A4B5C6D7E8F901234567890AB",
    "protocol": "torrent", "downloadClient": "RDTClient", "indexer": "YggTorrent",
    "outputPath": f"/data/Downloads/radarr/{PENDING}",
    "size": 8_000_000_000, "sizeleft": 0,
}
# A Sonarr row with a warning and no clearer signal: removed + blocklisted.
STALLED = {
    "id": 202, "seriesId": 77, "episodeId": 9001, "title": EPISODE,
    "status": "downloading", "trackedDownloadStatus": "warning",
    "trackedDownloadState": "downloading",
    "statusMessages": [{"title": EPISODE, "messages": [
        "The download is stalled with no connections"]}],
    "downloadId": "B2C3D4E5F60718293A4B5C6D7E8F90123456789A",
    "protocol": "torrent", "downloadClient": "RDTClient", "indexer": "YggTorrent",
    "outputPath": f"/data/Downloads/tv-sonarr/{EPISODE}",
    "size": 1_500_000_000, "sizeleft": 1_500_000_000, "added": iso(30),
    "episode": {"seasonNumber": 2, "episodeNumber": 3},
}


def make_downloads(tmp, library=True):
    """A Downloads directory as rdtclient leaves it, every file two days old,
    next to a mounted media library unless library=False."""
    root = pathlib.Path(tmp) / "Downloads"
    old = time.time() - 48 * 3600
    for rel in (f"radarr/{MOVIE}/movie.mkv", f"radarr/{PENDING}/movie.mkv",
                UNREFERENCED):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 16)
        os.utime(path, (old, old))
    (root / "tv-sonarr").mkdir()
    if library:
        make_library(tmp)
    return root


def make_library(tmp):
    """The media library of a mounted disk: one movie, no show yet."""
    movie = pathlib.Path(tmp) / "Movies" / "Some Movie (2021)" / "Some Movie (2021).mkv"
    movie.parent.mkdir(parents=True)
    movie.write_bytes(b"\0" * 16)
    (pathlib.Path(tmp) / "TV Shows").mkdir()


def environment(downloads):
    return {
        "RADARR_URL": "http://radarr.test:7878", "RADARR_API_KEY": "radarr-key",
        "SONARR_URL": "http://sonarr.test:8989", "SONARR_API_KEY": "sonarr-key",
        "DOWNLOADS_DIR": str(downloads),
        "MOVIES_DIR": str(downloads.parent / "Movies"),
        "TV_DIR": str(downloads.parent / "TV Shows"),
        # chown to ourselves works as any user; the test must not need root.
        "PUID": str(os.getuid()), "PGID": str(os.getgid()),
    }


def routes(radarr_queue=None, sonarr_queue=None):
    """Every route a run uses, answering as a healthy stack would."""
    return {
        ("GET", f"{RADARR}/queue"): reply(200, queue_page(
            [DOWNLOADING] if radarr_queue is None else radarr_queue)),
        ("GET", f"{SONARR}/queue"): reply(200, queue_page(
            [STALLED] if sonarr_queue is None else sonarr_queue)),
        ("DELETE", f"{SONARR}/queue/202"): reply(200, {}),
        ("GET", f"{RADARR}/movie"): reply(200, []),
        ("GET", f"{SONARR}/series"): reply(200, []),
    }


def run(table, downloads, patches=()):
    """One reset-error run; returns (exit status, fake API, log lines)."""
    api = FakeApi(table)
    CAPTURE.lines.clear()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("requests.request", new=api))
        for target, error in patches:
            stack.enter_context(mock.patch(target, side_effect=error))
        try:
            code = reset_error.run(environment(downloads))
        except Exception as error:  # a crash fails this case, not the whole file
            code = f"raised {error!r}"
    return code, api, list(CAPTURE.lines)


@contextlib.contextmanager
def case(label):
    """Attach the run's log to the failures of one case, and only then."""
    before = len(failures)
    yield
    if len(failures) > before:
        failures.append(f"  [{label}] log:\n    " + "\n    ".join(CAPTURE.lines))


# --- All queues readable, nothing fails: exit 0, the purge ran ------------
with tempfile.TemporaryDirectory() as tmp, case("all readable"):
    downloads = make_downloads(tmp)
    code, api, _ = run(routes(), downloads)
    check("all readable: exit status", code, 0)
    check("all readable: unreferenced old file purged",
          (downloads / UNREFERENCED).exists(), False)
    check("all readable: referenced old file kept",
          (downloads / f"radarr/{MOVIE}/movie.mkv").exists(), True)
    check("all readable: category subdirectory recreated",
          (downloads / "tv-sonarr").is_dir(), True)
    # One read per queue feeds both the protection set and the processing:
    # the purge protects exactly what the processing then acts on.
    check("all readable: Radarr queue read once",
          len(api.made("GET", f"{RADARR}/queue")), 1)
    check("all readable: Sonarr queue read once",
          len(api.made("GET", f"{SONARR}/queue")), 1)
    # 'Unknown' items are left out by the API unless asked for, and they are
    # the ones that stay stuck forever.
    check("all readable: unknown movies asked for",
          api.made("GET", f"{RADARR}/queue")[0]["params"].get(
              "includeUnknownMovieItems"), True)
    check("all readable: unknown series asked for",
          api.made("GET", f"{SONARR}/queue")[0]["params"].get(
              "includeUnknownSeriesItems"), True)
    deletes = api.made("DELETE", f"{SONARR}/queue/202")
    check("all readable: warning row removed", len(deletes), 1)
    if deletes:
        check("all readable: removed from the client",
              deletes[0]["params"].get("removeFromClient"), True)
        check("all readable: release blocklisted",
              deletes[0]["params"].get("blocklist"), True)
    # requests waits forever without a timeout; a hung call would hold the
    # unit until systemd kills it.
    check("all readable: every call has a timeout",
          [url for _, url, kwargs in api.calls if kwargs.get("timeout") is None], [])

# --- A queue that cannot be read: purge skipped, failure, exit 1 ----------
# Whatever the reason, the protection set would be incomplete, and a purge
# with an incomplete protection set is exactly audit C2.
UNREADABLE = [
    ("connection refused",
     requests.exceptions.ConnectionError("[Errno 111] Connection refused")),
    ("HTTP 503", reply(503, {"message": "Service Unavailable"})),
    ("not JSON", reply(200, raw=b"<html><body>Radarr is starting</body></html>")),
    ("JSON but not an object", reply(200, [DOWNLOADING])),
    ("records not a list",
     reply(200, {"page": 1, "pageSize": 1000, "totalRecords": 1,
                 "records": {"id": 101}})),
    ("no totalRecords", reply(200, {"page": 1, "pageSize": 1000,
                                    "records": [DOWNLOADING]})),
    # More rows than one page: the rest would go unprotected.
    ("truncated", reply(200, queue_page([DOWNLOADING], total=1500))),
]
for label, answer in UNREADABLE:
    with tempfile.TemporaryDirectory() as tmp, case(label):
        downloads = make_downloads(tmp)
        table = routes()
        table[("GET", f"{RADARR}/queue")] = answer
        code, api, _ = run(table, downloads)
        check(f"{label}: exit status", code, 1)
        check(f"{label}: purge skipped", (downloads / UNREFERENCED).exists(), True)
        # The queues that were read are still processed.
        check(f"{label}: Sonarr queue still processed",
              len(api.made("DELETE", f"{SONARR}/queue/202")), 1)

# --- A queue row that cannot be removed: exit 1 ---------------------------
with tempfile.TemporaryDirectory() as tmp, case("failing DELETE"):
    downloads = make_downloads(tmp)
    table = routes()
    table[("DELETE", f"{SONARR}/queue/202")] = reply(500, {"message": "boom"})
    code, api, _ = run(table, downloads)
    check("failing DELETE: exit status", code, 1)
    # The run goes on with the steps that follow.
    check("failing DELETE: dead metadata still checked",
          len(api.made("GET", f"{SONARR}/series")), 1)

# --- A manual import that cannot be asked for: row kept, exit 1 -----------
# Without a date and with nothing importable, a row is a ghost and gets
# removed; an API failure says nothing about importability, so the row must
# stay until a run can actually ask.
with tempfile.TemporaryDirectory() as tmp, case("failing manual import"):
    downloads = make_downloads(tmp)
    table = routes(radarr_queue=[DOWNLOADING, IMPORT_PENDING])
    table[("GET", f"{RADARR}/manualimport")] = reply(500, {"message": "boom"})
    table[("DELETE", f"{RADARR}/queue/103")] = reply(200, {})
    code, api, _ = run(table, downloads)
    check("failing manual import: exit status", code, 1)
    check("failing manual import: row kept",
          api.made("DELETE", f"{RADARR}/queue/103"), [])

# --- Dead-metadata calls that fail: exit 1, the next instance still checked
DEAD_MOVIE = {"id": 55, "title": "Vanished Movie", "tmdbId": 999_001,
              "status": "deleted", "hasFile": False, "monitored": True}
DEAD_METADATA = [
    ("movie list unreadable", {("GET", f"{RADARR}/movie"): reply(500, {})}),
    ("movie list not JSON",
     {("GET", f"{RADARR}/movie"): reply(200, raw=b"<html></html>")}),
    ("dead movie not removed",
     {("GET", f"{RADARR}/movie"): reply(200, [DEAD_MOVIE]),
      ("DELETE", f"{RADARR}/movie/55"): reply(500, {})}),
]
for label, extra in DEAD_METADATA:
    with tempfile.TemporaryDirectory() as tmp, case(label):
        downloads = make_downloads(tmp)
        table = routes()
        table.update(extra)
        code, api, _ = run(table, downloads)
        check(f"{label}: exit status", code, 1)
        check(f"{label}: series still checked",
              len(api.made("GET", f"{SONARR}/series")), 1)

# --- I/O failures in the purge are failures too ---------------------------
PURGE_IO = [
    ("file removal refused", "os.remove", PermissionError(13, "Permission denied")),
    ("empty directory removal refused", "os.rmdir",
     PermissionError(13, "Permission denied")),
    ("category subdirectory chown refused", "os.chown",
     PermissionError(1, "Operation not permitted")),
]
for label, target, error in PURGE_IO:
    with tempfile.TemporaryDirectory() as tmp, case(label):
        downloads = make_downloads(tmp)
        code, _, _ = run(routes(), downloads, patches=[(target, error)])
        check(f"{label}: exit status", code, 1)

# A missing Downloads directory is reported, never created: when the media
# disk is not mounted, creating it would write into the bare mount point.
with tempfile.TemporaryDirectory() as tmp, case("missing Downloads"):
    make_library(tmp)
    missing = pathlib.Path(tmp) / "Downloads"
    code, _, _ = run(routes(), missing)
    check("missing Downloads: exit status", code, 1)
    check("missing Downloads: not created", missing.exists(), False)

# --- Files gone: decided from the Downloads directory ---------------------
# An import-pending row whose download is no longer on disk is removed from
# the client WITHOUT blocklisting: the release was fine, its files vanished.
VANISHED = "Gone.Movie.2019.MULTi.1080p.WEB.x264-GRP"
IMPORT_VANISHED = {
    "id": 104, "movieId": 814, "title": VANISHED,
    "status": "completed", "trackedDownloadStatus": "warning",
    "trackedDownloadState": "importPending",
    "statusMessages": [{"title": VANISHED, "messages": [
        "One or more movies expected in this release were not imported or missing"]}],
    "downloadId": "D4E5F60718293A4B5C6D7E8F901234567890ABCD",
    "protocol": "torrent", "downloadClient": "RDTClient", "indexer": "YggTorrent",
    "outputPath": f"/data/Downloads/radarr/{VANISHED}",
    "size": 6_000_000_000, "sizeleft": 0, "added": iso(5),
}
# What Radarr answers /manualimport with for the download that is on disk.
CANDIDATES = [{
    "id": 1, "path": f"/data/Downloads/radarr/{PENDING}/movie.mkv",
    "relativePath": "movie.mkv", "folderName": PENDING, "name": "movie",
    "size": 8_000_000_000,
    "movie": {"id": 813, "title": "Other Movie", "year": 2020},
    "quality": {"quality": {"id": 7, "name": "Bluray-1080p"},
                "revision": {"version": 1, "real": 0, "isRepack": False}},
    "languages": [{"id": 2, "name": "French"}],
    "releaseGroup": "GRP", "downloadId": IMPORT_PENDING["downloadId"],
    "rejections": [],
}]


def import_routes():
    table = routes(radarr_queue=[DOWNLOADING, IMPORT_PENDING, IMPORT_VANISHED])
    table[("GET", f"{RADARR}/manualimport")] = reply(200, [])
    table[("POST", f"{RADARR}/command")] = reply(
        201, {"id": 4242, "name": "ManualImport", "status": "queued"})
    table[("DELETE", f"{RADARR}/queue/103")] = reply(200, {})
    table[("DELETE", f"{RADARR}/queue/104")] = reply(200, {})
    return table


with tempfile.TemporaryDirectory() as tmp, case("files gone"):
    downloads = make_downloads(tmp)  # holds PENDING, not VANISHED
    table = import_routes()
    table[("GET", f"{RADARR}/manualimport")] = reply(200, CANDIDATES)
    code, api, _ = run(table, downloads)
    check("files gone: exit status", code, 0)
    deletes = api.made("DELETE", f"{RADARR}/queue/104")
    check("files gone: row removed once", len(deletes), 1)
    if deletes:
        check("files gone: removal parameters", deletes[0]["params"],
              {"removeFromClient": True, "blocklist": False, "skipRedownload": True})
    lookups = [kwargs["params"]["downloadId"]
               for kwargs in api.made("GET", f"{RADARR}/manualimport")]
    check("files gone: no manual import attempted",
          IMPORT_VANISHED["downloadId"] in lookups, False)
    # The download still on disk goes through the manual import instead.
    check("download present: manual import looked up",
          lookups, [IMPORT_PENDING["downloadId"]])
    commands = api.made("POST", f"{RADARR}/command")
    check("download present: import command sent", len(commands), 1)
    if commands:
        sent = commands[0]["json"]
        check("download present: command", sent["name"], "ManualImport")
        check("download present: file imported as its movie",
              [(f["path"], f["movieId"]) for f in sent["files"]],
              [(f"/data/Downloads/radarr/{PENDING}/movie.mkv", 813)])
    check("download present: row not removed",
          api.made("DELETE", f"{RADARR}/queue/103"), [])

# A local problem keeps its row whatever the manual import finds: without a
# date and with nothing importable it is not a ghost, and fixing the problem
# is what gets it imported.
DENIED = "Denied.Movie.2018.MULTi.1080p.WEB.x264-GRP"
IMPORT_DENIED = dict(
    IMPORT_PENDING, id=105, movieId=815, title=DENIED,
    downloadId="E5F60718293A4B5C6D7E8F901234567890ABCDEF",
    outputPath=f"/data/Downloads/radarr/{DENIED}",
    statusMessages=[{"title": DENIED, "messages": [
        "Failed to import movie, Permission denied"]}])
with tempfile.TemporaryDirectory() as tmp, case("local problem"):
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

# Downloads cannot be listed: presence is unknown, nothing is concluded from
# it, and the failure is recorded.
with tempfile.TemporaryDirectory() as tmp, case("presence unknown"):
    make_library(tmp)
    missing = pathlib.Path(tmp) / "Downloads"
    code, api, _ = run(import_routes(), missing)
    check("presence unknown: exit status", code, 1)
    check("presence unknown: no files-gone removal",
          api.made("DELETE", f"{RADARR}/queue/104"), [])
    lookups = [kwargs["params"]["downloadId"]
               for kwargs in api.made("GET", f"{RADARR}/manualimport")]
    check("presence unknown: manual import attempted instead",
          IMPORT_VANISHED["downloadId"] in lookups, True)

# The media library must evidently be mounted before anything on disk is
# judged. Unmounted, Docker recreates the bind sources as empty directories:
# every download would look vanished, be removed from its client, and be
# purged as an orphan 24 h after the disk comes back.
def empty_library(tmp):
    for name in ("Movies", "TV Shows"):
        (pathlib.Path(tmp) / name).mkdir()


for label, prepare in (("library missing", None), ("library empty", empty_library)):
    with tempfile.TemporaryDirectory() as tmp, case(label):
        downloads = make_downloads(tmp, library=False)
        if prepare:
            prepare(tmp)
        code, api, _ = run(import_routes(), downloads)
        check(f"{label}: exit status", code, 1)
        check(f"{label}: no files-gone removal",
              api.made("DELETE", f"{RADARR}/queue/104"), [])
        # Nothing on disk is touched in such a run either.
        check(f"{label}: purge skipped", (downloads / UNREFERENCED).exists(), True)

# Only the listing fails. The purge walks the same tree, so in practice both
# fail together and the purge's failure alone already sets the exit status;
# failing the listing alone shows it is recorded in its own right.
with tempfile.TemporaryDirectory() as tmp, case("listing fails"):
    downloads = make_downloads(tmp)
    code, api, _ = run(import_routes(), downloads, patches=[
        ("maintenance.reset_error.names_under", PermissionError(13, "Permission denied"))])
    check("listing fails: exit status", code, 1)
    check("listing fails: no files-gone removal",
          api.made("DELETE", f"{RADARR}/queue/104"), [])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_reset_error_run: OK")
