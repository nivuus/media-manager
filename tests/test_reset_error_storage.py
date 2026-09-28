#!/usr/bin/env python3
"""reset-error's storage guard: nothing changes while storage looks unavailable.

With the media disk unmounted, Docker recreates the bind sources as empty
directories and Radarr/Sonarr report every download as holding no files, so
every rule, not only files-gone, would act on a broken view. Before any
action the run therefore checks that DOWNLOADS_DIR is a directory and that no
root folder of any instance reports accessible: false. When a check fails the
run makes no other call — no download client test, queue refresh or queue
read, so no removal, manual import or dead-metadata deletion — and does not
purge; it records a failure naming the check, and exits 1.

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
    RADARR, RADARR_ROOT, SONARR, UNREFERENCED, case, import_routes,
    make_downloads, run)

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

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_reset_error_storage: OK")
