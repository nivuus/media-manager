#!/usr/bin/env python3
"""reset-error manual-imports an import-pending row only when a second read of
the queue, CONFIRM_DELAY_SECONDS later, still shows it pending, and only once
the instance is not importing by itself. Against the fake API and a real
Downloads tree; the waits run on a fake clock.

importPending is often transient: Radarr/Sonarr import most downloads a
minute after they finish. A manual import racing their own reads files that
are being moved.

Run: python3 tests/test_import_guard.py
"""
import sys
import tempfile

sys.dont_write_bytecode = True

from reset_error_fixtures import (  # noqa: E402  (puts stack/ on sys.path)
    CANDIDATES, DOWNLOADING, IMPORT_PENDING, RADARR, case, commands, command,
    make_downloads, queue_page, routes, run)
from maintenance import import_guard  # noqa: E402
from maintenance_fakes import FakeClock, reply  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def pending_routes(second_read=None, command_list=None):
    """A Radarr queue with one pending row on disk, importable."""
    table = routes(radarr_queue=[DOWNLOADING, IMPORT_PENDING])
    table[("GET", f"{RADARR}/manualimport")] = reply(200, CANDIDATES)
    first = reply(200, queue_page([DOWNLOADING, IMPORT_PENDING]))
    table[("GET", f"{RADARR}/queue")] = [first, reply(200, queue_page(
        [DOWNLOADING, IMPORT_PENDING] if second_read is None else second_read))]
    if command_list is not None:
        table[("GET", f"{RADARR}/command")] = command_list
    return table


# --- Confirmed and idle: imported, after the confirmation delay -----------
with tempfile.TemporaryDirectory() as tmp, case("confirmed", failures):
    clock = FakeClock()
    code, api, _ = run(pending_routes(), make_downloads(tmp), clock=clock)
    check("confirmed: exit status", code, 0)
    check("confirmed: queue read twice", len(api.made("GET", f"{RADARR}/queue")), 2)
    check("confirmed: waited the confirmation delay",
          import_guard.CONFIRM_DELAY_SECONDS in clock.sleeps, True)
    check("confirmed: manual import sent", len(commands(api, RADARR, "ManualImport")), 1)

# --- Imported by Radarr in between: left alone ------------------------------
with tempfile.TemporaryDirectory() as tmp, case("transient", failures):
    code, api, lines = run(pending_routes(second_read=[DOWNLOADING]), make_downloads(tmp))
    check("transient: exit status", code, 0)
    check("transient: no manual-import lookup", api.made("GET", f"{RADARR}/manualimport"), [])
    check("transient: no removal", api.made("DELETE", f"{RADARR}/queue/103"), [])
    check("transient: said", any("not cleared" in line for line in lines), True)

# --- Radarr importing, then idle: imported once it is done ----------------
BUSY = reply(200, [command(9, "ProcessMonitoredDownloads", "started"),
                   command(10, "RssSync", "started")])
IDLE = reply(200, [command(9, "ProcessMonitoredDownloads", "completed"),
                   command(10, "RssSync", "started")])
with tempfile.TemporaryDirectory() as tmp, case("busy then idle", failures):
    clock = FakeClock()
    code, api, _ = run(pending_routes(command_list=[BUSY, IDLE]), make_downloads(tmp),
                       clock=clock)
    check("busy then idle: exit status", code, 0)
    check("busy then idle: polled twice", len(api.made("GET", f"{RADARR}/command")), 2)
    check("busy then idle: imported", len(commands(api, RADARR, "ManualImport")), 1)

# --- Radarr importing for longer than the timeout: kept for the next run ----
with tempfile.TemporaryDirectory() as tmp, case("still busy", failures):
    clock = FakeClock()
    code, api, _ = run(pending_routes(command_list=[BUSY]), make_downloads(tmp), clock=clock)
    check("still busy: not a failure", code, 0)
    check("still busy: no import", commands(api, RADARR, "ManualImport"), [])
    check("still busy: no removal", api.made("DELETE", f"{RADARR}/queue/103"), [])
    polls = len(api.made("GET", f"{RADARR}/command"))
    check("still busy: bounded wait",
          polls, import_guard.IDLE_TIMEOUT_SECONDS // import_guard.IDLE_POLL_SECONDS + 1)

# A command unrelated to imports (matched on its name) never holds them back.
with tempfile.TemporaryDirectory() as tmp, case("unrelated command", failures):
    code, api, _ = run(pending_routes(command_list=[IDLE]), make_downloads(tmp))
    check("unrelated command: imported", len(commands(api, RADARR, "ManualImport")), 1)

# --- The command list unreadable: a failure, and no import ---------------
with tempfile.TemporaryDirectory() as tmp, case("commands unreadable", failures):
    code, api, _ = run(pending_routes(command_list=reply(500, {"message": "boom"})),
                       make_downloads(tmp))
    check("commands unreadable: exit status", code, 1)
    check("commands unreadable: no import", commands(api, RADARR, "ManualImport"), [])
    check("commands unreadable: no removal", api.made("DELETE", f"{RADARR}/queue/103"), [])

# --- Nothing pending: no delay, no second read ------------------------------
with tempfile.TemporaryDirectory() as tmp, case("nothing pending", failures):
    clock = FakeClock()
    code, api, _ = run(routes(), make_downloads(tmp), clock=clock)
    check("nothing pending: queue read once", len(api.made("GET", f"{RADARR}/queue")), 1)
    check("nothing pending: no confirmation delay",
          import_guard.CONFIRM_DELAY_SECONDS in clock.sleeps, False)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_import_guard: OK")
