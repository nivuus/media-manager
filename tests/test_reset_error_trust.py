#!/usr/bin/env python3
"""reset-error's purge trusts a queue only when its download clients demonstrably fed it.

Radarr/Sonarr keep the queue in memory and replace it whole each time they
refresh it from the download clients. It is empty at startup, and a client
that is down, or blocked by the provider back-off, contributes no row while
GET /queue still succeeds. The purge acts on absence, so such a queue lets it
delete a completed, unimported download: audit C2, through a read that
succeeded. The timer is Persistent=true, so a missed 06:00 run fires at boot,
exactly when the queues are cold.

Per instance the run checks the clients (testall passes for a non-empty list
and health reports no DownloadClientStatusCheck), refreshes the queue and
waits for the command to complete, reads the queue, then checks the clients
again. When a step fails the purge is skipped, a failure is recorded and the
run exits 1; the rows that were read are still processed, since the rows
present are real and only absence is unreliable. The refresh is polled on a
fake clock: no case waits.

Run: python3 tests/test_reset_error_trust.py
"""
import sys
import tempfile

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True

import requests  # noqa: E402

from maintenance_fakes import FakeClock, reply  # noqa: E402
from reset_error_fixtures import (  # noqa: E402
    DOWNLOADING, IMPORT_VANISHED, RADARR, REFRESH_ID, SONARR, UNREFERENCED,
    case, commands, make_downloads, refresh_status, routes, run)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def calls_to(api, base):
    """(method, path) of every call made to one instance, in order."""
    return [(method, url[len(base):]) for method, url, _ in api.calls
            if url.startswith(base)]


def warned(log_lines, *words):
    """Whether one warning line holds all these words."""
    return any(line.startswith("WARNING") and all(word in line for word in words)
               for line in log_lines)


def radarr_routes():
    """Healthy routes, with a Radarr row that processing removes (files gone)."""
    table = routes(radarr_queue=[DOWNLOADING, IMPORT_VANISHED])
    table[("DELETE", f"{RADARR}/queue/104")] = reply(200, {})
    return table


# What health reports for a download client the provider back-off blocked.
BLOCKED = {"id": 3, "source": "DownloadClientStatusCheck", "type": "error",
           "message": "All download clients are unavailable due to failures",
           "wikiUrl": "https://wiki.servarr.com/radarr/system#download-clients-are-unavailable-due-to-failures"}
# Other sources, as production shows them: source is the check's class name,
# never localised, while the message is.
OTHER_SOURCES = [
    {"id": 1, "source": "IndexerLongTermStatusCheck", "type": "warning",
     "message": "Tous les indexeurs sont indisponibles en raison d'échecs depuis plus de 6 heures",
     "wikiUrl": "https://wiki.servarr.com/radarr/system#indexers-are-unavailable-due-to-failures"},
    {"id": 2, "source": "DownloadClientRootFolderCheck", "type": "warning",
     "message": "Download client RDTClient places downloads in the root folder /data/Movies",
     "wikiUrl": "https://wiki.servarr.com/radarr/system#downloads-in-root-folder"},
]

# --- Every check passes: the purge runs -------------------------------------
with tempfile.TemporaryDirectory() as tmp, case("all pass", failures):
    downloads = make_downloads(tmp)
    table = routes()
    # The refresh takes two polls to complete on Radarr.
    table[("GET", f"{RADARR}/command/{REFRESH_ID[RADARR]}")] = [
        refresh_status(RADARR, "queued"), refresh_status(RADARR, "started"),
        refresh_status(RADARR, "completed")]
    clock = FakeClock()
    code, api, _ = run(table, downloads, clock=clock)
    check("all pass: exit status", code, 0)
    check("all pass: purge ran", (downloads / UNREFERENCED).exists(), False)
    radarr_poll = ("GET", f"/command/{REFRESH_ID[RADARR]}")
    check("all pass: Radarr call order", calls_to(api, RADARR), [
        ("GET", "/rootfolder"),
        ("POST", "/downloadclient/testall"), ("GET", "/health"),
        ("POST", "/command"), radarr_poll, radarr_poll, radarr_poll,
        ("GET", "/queue"),
        ("POST", "/downloadclient/testall"), ("GET", "/health"),
        ("GET", "/movie"),
    ])
    check("all pass: Sonarr call order", calls_to(api, SONARR), [
        ("GET", "/rootfolder"),
        ("POST", "/downloadclient/testall"), ("GET", "/health"),
        ("POST", "/command"), ("GET", f"/command/{REFRESH_ID[SONARR]}"),
        ("GET", "/queue"),
        ("POST", "/downloadclient/testall"), ("GET", "/health"),
        ("DELETE", "/queue/202"),
        ("GET", "/series"),
    ])
    check("all pass: refresh command sent as is",
          [kwargs["json"] for kwargs in api.made("POST", f"{RADARR}/command")],
          [{"name": "RefreshMonitoredDownloads"}])
    check("all pass: one poll every 2 s", clock.sleeps, [2, 2])

# --- testall fails before the refresh: purge skipped, rows processed ---------
with tempfile.TemporaryDirectory() as tmp, case("testall 400", failures):
    downloads = make_downloads(tmp)
    table = radarr_routes()
    table[("POST", f"{RADARR}/downloadclient/testall")] = reply(400, [{
        "id": 1, "isValid": False, "validationFailures": [{
            "propertyName": "", "errorMessage": "Unable to connect to RDTClient",
            "severity": "error"}]}])
    code, api, log = run(table, downloads)
    check("testall 400: exit status", code, 1)
    check("testall 400: purge skipped", (downloads / UNREFERENCED).exists(), True)
    check("testall 400: reason named", warned(log, "radarr.test", "testall"), True)
    check("testall 400: no refresh",
          commands(api, RADARR, "RefreshMonitoredDownloads"), [])
    check("testall 400: queue still read",
          len(api.made("GET", f"{RADARR}/queue")), 1)
    check("testall 400: Radarr rows still processed",
          len(api.made("DELETE", f"{RADARR}/queue/104")), 1)
    check("testall 400: Sonarr rows still processed",
          len(api.made("DELETE", f"{SONARR}/queue/202")), 1)

# --- Health reports a blocked client only after the read ---------------------
with tempfile.TemporaryDirectory() as tmp, case("blocked after the read", failures):
    downloads = make_downloads(tmp)
    table = radarr_routes()
    table[("GET", f"{RADARR}/health")] = [reply(200, []), reply(200, [BLOCKED])]
    code, api, log = run(table, downloads)
    check("blocked after the read: exit status", code, 1)
    check("blocked after the read: purge skipped",
          (downloads / UNREFERENCED).exists(), True)
    check("blocked after the read: reason named",
          warned(log, "radarr.test", "DownloadClientStatusCheck"), True)
    check("blocked after the read: health read twice",
          len(api.made("GET", f"{RADARR}/health")), 2)
    check("blocked after the read: rows still processed",
          len(api.made("DELETE", f"{RADARR}/queue/104")), 1)

# --- No enabled download client: a queue no client feeds protects nothing ----
with tempfile.TemporaryDirectory() as tmp, case("no client", failures):
    downloads = make_downloads(tmp)
    table = radarr_routes()
    table[("POST", f"{RADARR}/downloadclient/testall")] = reply(200, [])
    code, api, log = run(table, downloads)
    check("no client: exit status", code, 1)
    check("no client: purge skipped", (downloads / UNREFERENCED).exists(), True)
    check("no client: reason named", warned(log, "radarr.test", "download client"), True)

# --- The refresh does not complete ---------------------------------------------
for status in ("failed", "aborted", "cancelled", "orphaned"):
    label = f"refresh {status}"
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp)
        table = radarr_routes()
        table[("GET", f"{RADARR}/command/{REFRESH_ID[RADARR]}")] = [
            refresh_status(RADARR, "started"), refresh_status(RADARR, status)]
        code, api, log = run(table, downloads)
        check(f"{label}: exit status", code, 1)
        check(f"{label}: purge skipped", (downloads / UNREFERENCED).exists(), True)
        check(f"{label}: reason named",
              warned(log, "radarr.test", "RefreshMonitoredDownloads", f"ended {status}"),
              True)
        check(f"{label}: rows still processed",
              len(api.made("DELETE", f"{RADARR}/queue/104")), 1)

# Still running when the bound expires: 120 s, one poll every 2 s.
with tempfile.TemporaryDirectory() as tmp, case("refresh never completes", failures):
    downloads = make_downloads(tmp)
    table = radarr_routes()
    table[("GET", f"{RADARR}/command/{REFRESH_ID[RADARR]}")] = refresh_status(
        RADARR, "started")
    clock = FakeClock()
    code, api, log = run(table, downloads, clock=clock)
    check("refresh never completes: exit status", code, 1)
    check("refresh never completes: purge skipped",
          (downloads / UNREFERENCED).exists(), True)
    check("refresh never completes: polled every 2 s",
          set(clock.sleeps), {2})
    check("refresh never completes: gave up after 120 s", sum(clock.sleeps), 120)
    check("refresh never completes: reason named",
          warned(log, "radarr.test", "RefreshMonitoredDownloads", "120"), True)
    check("refresh never completes: rows still processed",
          len(api.made("DELETE", f"{RADARR}/queue/104")), 1)

# --- An answer the checks cannot read fails them -------------------------------
UNREADABLE = [
    ("testall refused", ("POST", "/downloadclient/testall"),
     requests.exceptions.ConnectionError("[Errno 111] Connection refused")),
    ("testall timeout", ("POST", "/downloadclient/testall"),
     requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")),
    ("testall not a list", ("POST", "/downloadclient/testall"),
     reply(200, {"id": 1, "isValid": True})),
    ("testall client not valid", ("POST", "/downloadclient/testall"),
     reply(200, [{"id": 1, "isValid": True}, {"id": 2, "isValid": False}])),
    ("testall client without verdict", ("POST", "/downloadclient/testall"),
     reply(200, [{"id": 1, "validationFailures": []}])),
    ("health HTTP 500", ("GET", "/health"), reply(500, {"message": "boom"})),
    ("health not JSON", ("GET", "/health"), reply(200, raw=b"<html></html>")),
    ("refresh refused", ("POST", "/command"), reply(500, {"message": "boom"})),
    ("refresh without id", ("POST", "/command"), reply(201, {"status": "queued"})),
    ("refresh status unreadable", ("GET", f"/command/{REFRESH_ID[RADARR]}"),
     reply(404, {"message": "NotFound"})),
    ("refresh status unknown", ("GET", f"/command/{REFRESH_ID[RADARR]}"),
     reply(200, {"id": REFRESH_ID[RADARR], "status": "paused"})),
    ("refresh status missing", ("GET", f"/command/{REFRESH_ID[RADARR]}"),
     reply(200, {"id": REFRESH_ID[RADARR]})),
]
for label, (method, path), answer in UNREADABLE:
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp)
        table = radarr_routes()
        table[(method, f"{RADARR}{path}")] = answer
        clock = FakeClock()
        code, api, log = run(table, downloads, clock=clock)
        check(f"{label}: exit status", code, 1)
        check(f"{label}: purge skipped", (downloads / UNREFERENCED).exists(), True)
        check(f"{label}: instance named", warned(log, "radarr.test"), True)
        # Not an answer to wait on: it fails at once.
        check(f"{label}: no wait", clock.sleeps, [])
        check(f"{label}: rows still processed",
              len(api.made("DELETE", f"{RADARR}/queue/104")), 1)

# --- Health reporting other sources does not block the purge -----------------
with tempfile.TemporaryDirectory() as tmp, case("other sources", failures):
    downloads = make_downloads(tmp)
    table = routes()
    table[("GET", f"{RADARR}/health")] = reply(200, OTHER_SOURCES)
    code, api, _ = run(table, downloads)
    check("other sources: exit status", code, 0)
    check("other sources: purge ran", (downloads / UNREFERENCED).exists(), False)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_reset_error_trust: OK")
