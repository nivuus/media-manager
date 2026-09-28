#!/usr/bin/env python3
"""reset-error's storage guard: nothing changes while storage looks unavailable.

Before any action the run checks that DOWNLOADS_DIR is a directory and that
no root folder of any instance reports accessible: false, or cannot be read.
When a check fails the run makes no other call — no download client test,
queue refresh or queue read, so no removal, manual import or dead-metadata
deletion — and does not purge; it records a failure naming the check, and
exits 1.

A media disk missing at boot passes that guard: Docker recreates every
missing bind source as an empty directory, so the root folders exist and
report accessible. For that case files-gone, the rule that acts on a
download's absence from Downloads, is withheld while neither library
directory (MOVIES_DIR, TV_DIR) holds anything, with a warning rather than
a failure, since a fresh install's library is empty too.

Run: python3 tests/test_reset_error_storage.py
"""
import pathlib
import sys
import tempfile

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True

import requests  # noqa: E402

from maintenance_fakes import reply  # noqa: E402
from reset_error_fixtures import (  # noqa: E402
    RADARR, RADARR_ROOT, SONARR, UNREFERENCED, case, environment,
    import_routes, make_downloads, run)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# The guard's own reads, and all a tripped guard may send: not even a
# queue read, a download client test or a queue refresh follows.
ROOTFOLDER_READS = [("GET", f"{RADARR}/rootfolder"), ("GET", f"{SONARR}/rootfolder")]


def calls(api):
    """(method, url) of every call the run made, in order."""
    return [(method, url) for method, url, _ in api.calls]


def named(log_lines, *words):
    """Whether one error line names all these words: which check failed."""
    return any(line.startswith("ERROR") and all(word in line for word in words)
               for line in log_lines)


def warned(log_lines, *words):
    """Whether one warning line names all these words."""
    return any(line.startswith("WARNING") and all(word in line for word in words)
               for line in log_lines)


def removals(api, url):
    """The parameters of every DELETE sent to one queue row."""
    return [kwargs["params"] for kwargs in api.made("DELETE", url)]


# How a row whose files are gone is removed (the release was fine), and how
# the warning row of the Sonarr queue is (a release that did not work out).
FILES_GONE = {"removeFromClient": True, "blocklist": False, "skipRedownload": True}
BLOCKLIST = {"removeFromClient": True, "blocklist": True, "skipRedownload": False}
# The import-pending Radarr row whose download is not in Downloads.
VANISHED_ROW = f"{RADARR}/queue/104"


# --- The Downloads directory is missing ------------------------------------
with tempfile.TemporaryDirectory() as tmp, case("downloads missing", failures):
    missing = pathlib.Path(tmp) / "Downloads"
    code, api, log = run(import_routes(), missing)
    check("downloads missing: exit status", code, 1)
    check("downloads missing: only the root folders read", calls(api), ROOTFOLDER_READS)
    check("downloads missing: not created", missing.exists(), False)
    check("downloads missing: check named", named(log, str(missing)), True)

# --- A root folder is inaccessible -------------------------------------------
with tempfile.TemporaryDirectory() as tmp, case("root folder inaccessible", failures):
    downloads = make_downloads(tmp)
    table = import_routes()
    table[("GET", f"{RADARR}/rootfolder")] = reply(
        200, [dict(RADARR_ROOT, accessible=False, freeSpace=None)])
    code, api, log = run(table, downloads)
    check("root folder inaccessible: exit status", code, 1)
    check("root folder inaccessible: only the root folders read", calls(api),
          ROOTFOLDER_READS)
    check("root folder inaccessible: purge skipped",
          (downloads / UNREFERENCED).exists(), True)
    check("root folder inaccessible: check named",
          named(log, "/data/Movies", "radarr.test"), True)

# --- A rootfolder endpoint cannot be read --------------------------------------
UNREADABLE = [
    ("connection refused",
     requests.exceptions.ConnectionError("[Errno 111] Connection refused")),
    ("HTTP 503", reply(503, {"message": "Service Unavailable"})),
    ("not JSON", reply(200, raw=b"<html><body>Sonarr is starting</body></html>")),
    ("not a list", reply(200, {"id": 1, "path": "/data/TV Shows"})),
]
for label, answer in UNREADABLE:
    with tempfile.TemporaryDirectory() as tmp, case(f"rootfolder {label}", failures):
        downloads = make_downloads(tmp)
        table = import_routes()
        table[("GET", f"{SONARR}/rootfolder")] = answer
        code, api, log = run(table, downloads)
        check(f"rootfolder {label}: exit status", code, 1)
        check(f"rootfolder {label}: only the root folders read", calls(api),
              ROOTFOLDER_READS)
        check(f"rootfolder {label}: purge skipped",
              (downloads / UNREFERENCED).exists(), True)
        check(f"rootfolder {label}: check named",
              named(log, "rootfolder", "sonarr.test"), True)

# --- No root folder at all: no evidence either way, the run goes on --------
with tempfile.TemporaryDirectory() as tmp, case("no root folder", failures):
    downloads = make_downloads(tmp)
    table = import_routes()
    table[("GET", f"{RADARR}/rootfolder")] = reply(200, [])
    table[("GET", f"{SONARR}/rootfolder")] = reply(200, [])
    code, api, log = run(table, downloads)
    check("no root folder: exit status", code, 0)
    check("no root folder: purge ran", (downloads / UNREFERENCED).exists(), False)
    check("no root folder: queues processed",
          len(api.made("DELETE", f"{SONARR}/queue/202")), 1)

# --- No library on disk: files-gone is never concluded (Ruling 27) -----------
# A media disk missing at boot passes every check above: Docker recreates each
# missing bind source as an empty directory on the root filesystem (the
# library directories bazarr and plex bind one by one, the Downloads directory
# rdtclient and prowlarr bind), so the root folders exist and report
# accessible. Read against that empty Downloads, every import-pending row
# would look like files gone and be removed from its client, and the purge
# would delete those downloads once the disk is back (audit C2). So while
# neither library directory holds anything, files-gone is withheld and a
# warning says why. Not a failure: a fresh install has an empty library too.
# Only files-gone is withheld; the other rules and the purge are unchanged.
with tempfile.TemporaryDirectory() as tmp, case("empty library", failures):
    downloads = make_downloads(tmp, library=False)
    code, api, log = run(import_routes(), downloads)
    check("empty library: exit status", code, 0)
    check("empty library: no files-gone removal", removals(api, VANISHED_ROW), [])
    check("empty library: warning names both directories",
          warned(log, "files-gone", str(pathlib.Path(tmp) / "Movies"),
                 str(pathlib.Path(tmp) / "TV Shows")), True)
    check("empty library: other rules still applied",
          removals(api, f"{SONARR}/queue/202"), [BLOCKLIST])
    check("empty library: purge still ran", (downloads / UNREFERENCED).exists(), False)

# The library directories not there at all: no bind source recreated yet.
with tempfile.TemporaryDirectory() as tmp, case("no library directory", failures):
    downloads = make_downloads(tmp, library=False)
    for directory in ("Movies", "TV Shows"):
        (pathlib.Path(tmp) / directory).rmdir()
    code, api, log = run(import_routes(), downloads)
    check("no library directory: exit status", code, 0)
    check("no library directory: no files-gone removal", removals(api, VANISHED_ROW), [])
    check("no library directory: warning names both directories",
          warned(log, "files-gone", str(pathlib.Path(tmp) / "Movies"),
                 str(pathlib.Path(tmp) / "TV Shows")), True)

# An environment naming no library at all proves nothing either, whatever
# sits on disk.
with tempfile.TemporaryDirectory() as tmp, case("no library configured", failures):
    downloads = make_downloads(tmp)
    unconfigured = environment(downloads)
    del unconfigured["MOVIES_DIR"], unconfigured["TV_DIR"]
    code, api, log = run(import_routes(), downloads, environ=unconfigured)
    check("no library configured: exit status", code, 0)
    check("no library configured: no files-gone removal", removals(api, VANISHED_ROW), [])
    check("no library configured: warning names both variables",
          warned(log, "files-gone", "MOVIES_DIR", "TV_DIR"), True)

# One library directory holding a title is enough: files-gone fires as before.
for directory in ("Movies", "TV Shows"):
    label = f"only {directory} holds a title"
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp, library=False)
        (pathlib.Path(tmp) / directory / "Some Title (2020)").mkdir()
        code, api, log = run(import_routes(), downloads)
        check(f"{label}: exit status", code, 0)
        check(f"{label}: files-gone removal", removals(api, VANISHED_ROW), [FILES_GONE])
        check(f"{label}: no warning", warned(log, "files-gone"), False)

# A library directory that cannot be listed says nothing about the disk: a
# failure, and files-gone withheld.
with tempfile.TemporaryDirectory() as tmp, case("library unlistable", failures):
    downloads = make_downloads(tmp)
    movies = str(pathlib.Path(tmp) / "Movies")
    code, api, log = run(import_routes(), downloads, patches=[
        ("os.listdir", PermissionError(13, "Permission denied", movies))])
    check("library unlistable: exit status", code, 1)
    check("library unlistable: no files-gone removal", removals(api, VANISHED_ROW), [])
    check("library unlistable: directory named", named(log, "files-gone", movies), True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_reset_error_storage: OK")
