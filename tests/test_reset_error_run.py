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
    CANDIDATES, DOWNLOADING, IMPORT_PENDING, IMPORT_VANISHED, MOVIE, PENDING,
    RADARR, SONARR, UNREFERENCED, case, commands, import_routes,
    make_downloads, queue_page, routes, run)

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
DEAD_METADATA = [
    ("movie list unreadable", {("GET", f"{RADARR}/movie"): reply(500, {})}),
    ("movie list not JSON",
     {("GET", f"{RADARR}/movie"): reply(200, raw=b"<html></html>")}),
    ("dead movie not removed",
     {("GET", f"{RADARR}/movie"): reply(200, [DEAD_MOVIE]),
      ("DELETE", f"{RADARR}/movie/55"): reply(500, {})}),
]
for label, extra in DEAD_METADATA:
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
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
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp)
        code, _, _ = run(routes(), downloads, patches=[(target, error)])
        check(f"{label}: exit status", code, 1)

# --- Files gone: decided from the Downloads directory ---------------------
# An import-pending row whose download is no longer on disk is removed from
# the client WITHOUT blocklisting: the release was fine, its files vanished.

with tempfile.TemporaryDirectory() as tmp, case("files gone", failures):
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
    imports = commands(api, RADARR, "ManualImport")
    check("download present: import command sent", len(imports), 1)
    if imports:
        sent = imports[0]
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
