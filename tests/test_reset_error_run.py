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
import sys
import tempfile

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True

import requests  # noqa: E402

from maintenance_fakes import reply  # noqa: E402
from reset_error_fixtures import (  # noqa: E402
    DOWNLOADING, IMPORT_PENDING, MOVIE, RADARR, SONARR, UNREFERENCED, case,
    import_routes, make_downloads, queue_page, routes, run)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# --- All queues readable, nothing fails: exit 0, the purge ran ------------
with tempfile.TemporaryDirectory() as tmp, case("all readable", failures):
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
    radarr_reads = api.made("GET", f"{RADARR}/queue")
    sonarr_reads = api.made("GET", f"{SONARR}/queue")
    check("all readable: Radarr queue read once", len(radarr_reads), 1)
    check("all readable: Sonarr queue read once", len(sonarr_reads), 1)
    # 'Unknown' items are left out by the API unless asked for, and they are
    # the ones that stay stuck forever.
    check("all readable: unknown movies asked for",
          [read["params"].get("includeUnknownMovieItems") for read in radarr_reads],
          [True])
    check("all readable: unknown series asked for",
          [read["params"].get("includeUnknownSeriesItems") for read in sonarr_reads],
          [True])
    # Removed from the client and blocklisted, and a replacement searched for
    # at once: the next grab is a different release.
    check("all readable: warning row removed and blocklisted",
          [kwargs["params"] for kwargs in api.made("DELETE", f"{SONARR}/queue/202")],
          [{"removeFromClient": True, "blocklist": True, "skipRedownload": False}])
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
    ("records holding a non-object",
     reply(200, {"page": 1, "pageSize": 1000, "totalRecords": 1, "records": ["101"]})),
    ("no totalRecords", reply(200, {"page": 1, "pageSize": 1000,
                                    "records": [DOWNLOADING]})),
    # More rows than one page: the rest would go unprotected.
    ("truncated", reply(200, queue_page([DOWNLOADING], total=1500))),
]
for label, answer in UNREADABLE:
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
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
with tempfile.TemporaryDirectory() as tmp, case("failing DELETE", failures):
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
with tempfile.TemporaryDirectory() as tmp, case("failing manual import", failures):
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
# What the removal of a dead movie sends: its files, if any, stay on disk,
# and it may be added again later.
DEAD_MOVIE_REMOVAL = {"deleteFiles": False, "addImportExclusion": False}
DEAD_METADATA = [
    ("movie list unreadable", {("GET", f"{RADARR}/movie"): reply(500, {})}, []),
    ("movie list not JSON",
     {("GET", f"{RADARR}/movie"): reply(200, raw=b"<html></html>")}, []),
    ("dead movie not removed",
     {("GET", f"{RADARR}/movie"): reply(200, [DEAD_MOVIE]),
      ("DELETE", f"{RADARR}/movie/55"): reply(500, {})}, [DEAD_MOVIE_REMOVAL]),
]
for label, extra, removals in DEAD_METADATA:
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp)
        table = routes()
        table.update(extra)
        code, api, _ = run(table, downloads)
        check(f"{label}: exit status", code, 1)
        check(f"{label}: removal parameters",
              [kwargs["params"] for kwargs in api.made("DELETE", f"{RADARR}/movie/55")],
              removals)
        check(f"{label}: series still checked",
              len(api.made("GET", f"{SONARR}/series")), 1)

# --- Dead entries: only the file-less ones are removed ----------------------
# Deciding what to do with orphaned media stays a human call.
DEAD_SERIES = {"id": 66, "title": "Vanished Show", "tvdbId": 999_002,
               "status": "deleted", "statistics": {"sizeOnDisk": 0}}
with tempfile.TemporaryDirectory() as tmp, case("dead entries", failures):
    downloads = make_downloads(tmp)
    table = routes()
    table[("GET", f"{RADARR}/movie")] = reply(200, [
        DEAD_MOVIE, dict(DEAD_MOVIE, id=56, hasFile=True),
        dict(DEAD_MOVIE, id=57, status="released")])
    table[("GET", f"{SONARR}/series")] = reply(200, [
        DEAD_SERIES, dict(DEAD_SERIES, id=67, statistics={"sizeOnDisk": 1_500_000_000})])
    for path in ("movie/55", "movie/56", "movie/57"):
        table[("DELETE", f"{RADARR}/{path}")] = reply(200, {})
    for path in ("series/66", "series/67"):
        table[("DELETE", f"{SONARR}/{path}")] = reply(200, {})
    code, api, _ = run(table, downloads)
    check("dead entries: exit status", code, 0)
    check("dead entries: file-less movie removed, files kept",
          [kwargs["params"] for kwargs in api.made("DELETE", f"{RADARR}/movie/55")],
          [DEAD_MOVIE_REMOVAL])
    check("dead entries: movie with a file kept",
          api.made("DELETE", f"{RADARR}/movie/56"), [])
    check("dead entries: movie still known upstream kept",
          api.made("DELETE", f"{RADARR}/movie/57"), [])
    check("dead entries: file-less series removed, files kept",
          [kwargs["params"] for kwargs in api.made("DELETE", f"{SONARR}/series/66")],
          [{"deleteFiles": False}])
    check("dead entries: series with files kept",
          api.made("DELETE", f"{SONARR}/series/67"), [])

# --- I/O failures in the purge are failures too ---------------------------
PURGE_IO = [
    ("file removal refused", "os.remove", PermissionError(13, "Permission denied")),
    ("empty directory removal refused", "os.rmdir",
     PermissionError(13, "Permission denied")),
    ("category subdirectory chown refused", "os.chown",
     PermissionError(1, "Operation not permitted")),
]
for label, target, error in PURGE_IO:
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp)
        code, _, _ = run(routes(), downloads, patches=[(target, error)])
        check(f"{label}: exit status", code, 1)

# Only the listing fails. The purge walks the same tree, so in practice both
# fail together and the purge's failure alone already sets the exit status;
# failing the listing alone shows it is recorded in its own right.
with tempfile.TemporaryDirectory() as tmp, case("listing fails", failures):
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
