#!/usr/bin/env python3
"""reset-error's purge trusts a queue only when its download clients demonstrably fed it.

Radarr/Sonarr keep the queue in memory and replace it whole each time they
refresh it from the download clients. It is empty at startup, and a client
that is down, or blocked by the provider back-off, contributes no row while
GET /queue still succeeds. The purge acts on absence, so such a queue lets it
delete a completed, unimported download: audit C2, through a read that
succeeded. The timer is Persistent=true, so a missed 06:00 run fires at boot,
exactly when the queues are cold.

Per instance the run checks the clients (testall passes for a non-empty list,
a client whose failures are all warnings included, and health reports
neither DownloadClientStatusCheck nor DownloadClientCheck), refreshes the
queue and waits for the command to complete, reads the queue, then checks
the clients again. When a step fails the purge is skipped, a failure is recorded and the
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
    DOWNLOADING, IMPORT_VANISHED, RADARR, REFRESH_ID, SONARR, TESTALL_PASSED,
    UNREFERENCED, case, commands, make_downloads, refresh_status, routes, run)

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


def client_warning(log_lines, *words):
    """Whether a warning other than the purge skip holds all these words."""
    return any(line.startswith("WARNING")
               and not line.startswith("WARNING Downloads purge skipped")
               and all(word in line for word in words) for line in log_lines)


def skipped_for(log_lines, *words):
    """Whether the purge-skip warning holds all these words."""
    return any(line.startswith("WARNING Downloads purge skipped")
               and all(word in line for word in words) for line in log_lines)


def radarr_routes():
    """Healthy routes, with a Radarr row that processing removes (files gone)."""
    table = routes(radarr_queue=[DOWNLOADING, IMPORT_VANISHED])
    table[("DELETE", f"{RADARR}/queue/104")] = reply(200, {})
    return table


# What testall answers when one of two clients fails: 400, and the same list
# of results as a 200, the failing one with its validation failures.
TESTALL_REJECTED = [
    {"id": 1, "isValid": True, "validationFailures": []},
    {"id": 3, "isValid": False, "validationFailures": [
        {"propertyName": "Host", "errorMessage": "Unable to connect to qBittorrent",
         "attemptedValue": "rdtclient", "severity": "error"},
        {"propertyName": "", "errorMessage": "Connection refused (rdtclient:6500)",
         "attemptedValue": None, "severity": "error"}]},
]
# What health reports for a download client that cannot feed the queue:
# blocked by the provider back-off, failing to list its downloads, or no
# client available at all.
BLOCKING = [
    {"id": 3, "source": "DownloadClientStatusCheck", "type": "error",
     "message": "All download clients are unavailable due to failures",
     "wikiUrl": "https://wiki.servarr.com/radarr/system#download-clients-are-unavailable-due-to-failures"},
    {"id": 4, "source": "DownloadClientCheck", "type": "error",
     "message": "Unable to communicate with RDTClient. Connection refused (rdtclient:6500)",
     "wikiUrl": "https://wiki.servarr.com/radarr/system#unable-to-communicate-with-download-client"},
    {"id": 5, "source": "DownloadClientCheck", "type": "warning",
     "message": "No download client is available",
     "wikiUrl": "https://wiki.servarr.com/radarr/system#no-download-client-is-available"},
]
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
# Where a first check fails, the second one would pass: the queue was read
# without a refresh, and the first verdict alone has to keep it untrusted.
with tempfile.TemporaryDirectory() as tmp, case("testall 400", failures):
    downloads = make_downloads(tmp)
    table = radarr_routes()
    table[("POST", f"{RADARR}/downloadclient/testall")] = [
        reply(400, TESTALL_REJECTED), reply(200, TESTALL_PASSED)]
    code, api, log = run(table, downloads)
    check("testall 400: exit status", code, 1)
    check("testall 400: purge skipped", (downloads / UNREFERENCED).exists(), True)
    # The body of the 400 says which client failed and why: the alert has to.
    check("testall 400: failing client and its messages named",
          warned(log, "radarr.test", "client 3", "Unable to connect to qBittorrent",
                 "Connection refused (rdtclient:6500)"), True)
    check("testall 400: passing client not named", warned(log, "client 1"), False)
    check("testall 400: no refresh",
          commands(api, RADARR, "RefreshMonitoredDownloads"), [])
    check("testall 400: queue still read",
          len(api.made("GET", f"{RADARR}/queue")), 1)
    check("testall 400: Radarr rows still processed",
          len(api.made("DELETE", f"{RADARR}/queue/104")), 1)
    check("testall 400: Sonarr rows still processed",
          len(api.made("DELETE", f"{SONARR}/queue/202")), 1)

# A 400 that names no failing client, or another error status, keeps the
# error's own message as the reason; the check fails all the same.
UNNAMED = [
    ("testall 400 not a list", reply(400, {"message": "Bad Request"}), "400 Client Error"),
    ("testall 400 not JSON", reply(400, raw=b"<html><body>Bad Request</body></html>"),
     "400 Client Error"),
    ("testall 400 naming no failing client", reply(400, TESTALL_PASSED), "400 Client Error"),
    ("testall 500 with results", reply(500, TESTALL_REJECTED), "500 Server Error"),
]
for label, answer, reason in UNNAMED:
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp)
        table = radarr_routes()
        table[("POST", f"{RADARR}/downloadclient/testall")] = [
            answer, reply(200, TESTALL_PASSED)]
        code, api, log = run(table, downloads)
        check(f"{label}: exit status", code, 1)
        check(f"{label}: purge skipped", (downloads / UNREFERENCED).exists(), True)
        check(f"{label}: reason kept", warned(log, "radarr.test", reason), True)

# --- Warnings alone do not hold a client against the queue (Ruling 15) --------
# testall answers 400 on any validation failure, a warning included. A
# failure counts as a warning when the API marks it so, isWarning true or a
# warning severity; it is then logged, and no failure is recorded.
TESTALL_WARNED = [
    {"id": 1, "isValid": True, "validationFailures": []},
    {"id": 3, "isValid": False, "validationFailures": [
        {"propertyName": "MovieCategory", "severity": "warning",
         "errorMessage": "Adding a category specific to Radarr is recommended"}]},
    {"id": 4, "isValid": False, "validationFailures": [
        {"propertyName": "", "isWarning": True, "severity": "error",
         "errorMessage": "Removing torrents that reached their ratio limit is enabled"}]},
]
with tempfile.TemporaryDirectory() as tmp, case("warnings only", failures):
    downloads = make_downloads(tmp)
    table = radarr_routes()
    table[("POST", f"{RADARR}/downloadclient/testall")] = reply(400, TESTALL_WARNED)
    code, api, log = run(table, downloads)
    check("warnings only: exit status", code, 0)
    check("warnings only: purge ran", (downloads / UNREFERENCED).exists(), False)
    check("warnings only: nothing recorded",
          [line for line in log if line.startswith("ERROR")], [])
    check("warnings only: queue refreshed",
          len(commands(api, RADARR, "RefreshMonitoredDownloads")), 1)
    check("warnings only: warning severity logged",
          client_warning(log, "radarr.test", "client 3", "Adding a category"), True)
    check("warnings only: isWarning logged",
          client_warning(log, "radarr.test", "client 4", "Removing torrents"), True)

# A warning next to an error: the error client alone is held against the queue.
with tempfile.TemporaryDirectory() as tmp, case("warning and error", failures):
    downloads = make_downloads(tmp)
    table = radarr_routes()
    table[("POST", f"{RADARR}/downloadclient/testall")] = [
        reply(400, [TESTALL_WARNED[1], {"id": 5, "isValid": False, "validationFailures": [
            {"propertyName": "Host", "severity": "error",
             "errorMessage": "Unable to connect to qBittorrent"}]}]),
        reply(200, TESTALL_PASSED)]
    code, api, log = run(table, downloads)
    check("warning and error: exit status", code, 1)
    check("warning and error: purge skipped", (downloads / UNREFERENCED).exists(), True)
    check("warning and error: error client named",
          skipped_for(log, "radarr.test", "client 5", "Unable to connect to qBittorrent"),
          True)
    check("warning and error: warning client not held", skipped_for(log, "client 3"), False)
    check("warning and error: warning logged",
          client_warning(log, "radarr.test", "client 3", "Adding a category"), True)

# A failure the check cannot read as a warning is an error. Radarr/Sonarr
# write enums as camelCase strings, so a number is not their warning.
UNRECOGNISED = [
    ("no marker", {"propertyName": "", "errorMessage": "Unexpected state"}),
    ("unknown severity",
     {"propertyName": "", "errorMessage": "Unexpected state", "severity": "notice"}),
    ("numeric severity",
     {"propertyName": "", "errorMessage": "Unexpected state", "severity": 1}),
    ("isWarning not true",
     {"propertyName": "", "errorMessage": "Unexpected state", "isWarning": "yes"}),
    ("not an object", "Unexpected state"),
]
for label, failure in UNRECOGNISED:
    label = f"failure with {label}"
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp)
        table = radarr_routes()
        table[("POST", f"{RADARR}/downloadclient/testall")] = [
            reply(400, [{"id": 6, "isValid": False, "validationFailures": [failure]}]),
            reply(200, TESTALL_PASSED)]
        code, api, log = run(table, downloads)
        check(f"{label}: exit status", code, 1)
        check(f"{label}: purge skipped", (downloads / UNREFERENCED).exists(), True)
        check(f"{label}: client named", skipped_for(log, "radarr.test", "client 6"), True)

# --- Health reports a client that cannot feed the queue, at one check only ----
for item in BLOCKING:
    for when, bodies in (("before the refresh", [[item], []]),
                         ("after the read", [[], [item]])):
        label = f"{item['source']} {item['type']} {when}"
        with tempfile.TemporaryDirectory() as tmp, case(label, failures):
            downloads = make_downloads(tmp)
            table = radarr_routes()
            table[("GET", f"{RADARR}/health")] = [reply(200, body) for body in bodies]
            code, api, log = run(table, downloads)
            check(f"{label}: exit status", code, 1)
            check(f"{label}: purge skipped", (downloads / UNREFERENCED).exists(), True)
            check(f"{label}: reason named",
                  warned(log, "radarr.test", item["source"], item["message"]), True)
            check(f"{label}: rows still processed",
                  len(api.made("DELETE", f"{RADARR}/queue/104")), 1)
            if when == "before the refresh":
                check(f"{label}: no refresh",
                      commands(api, RADARR, "RefreshMonitoredDownloads"), [])
            else:
                check(f"{label}: health read twice",
                      len(api.made("GET", f"{RADARR}/health")), 2)

# --- No enabled download client: a queue no client feeds protects nothing ----
with tempfile.TemporaryDirectory() as tmp, case("no client", failures):
    downloads = make_downloads(tmp)
    table = radarr_routes()
    table[("POST", f"{RADARR}/downloadclient/testall")] = [
        reply(200, []), reply(200, TESTALL_PASSED)]
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
# What the clients checks read when all is well: an unreadable first answer
# is followed by a good one, so that its verdict has to stand on its own.
CLEAN = {("POST", "/downloadclient/testall"): reply(200, TESTALL_PASSED),
         ("GET", "/health"): reply(200, [])}
for label, (method, path), answer in UNREADABLE:
    with tempfile.TemporaryDirectory() as tmp, case(label, failures):
        downloads = make_downloads(tmp)
        table = radarr_routes()
        table[(method, f"{RADARR}{path}")] = (
            [answer, CLEAN[(method, path)]] if (method, path) in CLEAN else answer)
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
