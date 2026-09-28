#!/usr/bin/env python3
"""The queue policy of reset-error, checked as a table of rows and verdicts.

classify_item() is pure: a queue row as Radarr/Sonarr return it and the
current time go in, an action comes out. Every rule is pinned with a literal
expectation, the unchanged ones included, so that a rule moved up or down the
order shows up as a failed row rather than as a download deleted in
production.

Run: python3 tests/test_queue_policy.py
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stack"))

from maintenance.queue_policy import classify_item  # noqa: E402

NOW = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def ago(hours):
    """An 'added' value as the API sends it: ISO 8601, UTC, 'Z' suffix."""
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row(status="downloading", tracked_status="ok", tracked_state="downloading",
        added=None, error="", messages=()):
    """A queue record with the fields Radarr's /api/v3/queue returns.

    messages: (title, [message, ...]) pairs, the shape of statusMessages.
    """
    record = {
        "id": 1734,
        "movieId": 812,
        "title": "Some.Movie.2021.MULTi.1080p.WEB.x264-GRP",
        "size": 4_800_000_000,
        "sizeleft": 0,
        "status": status,
        "trackedDownloadStatus": tracked_status,
        "trackedDownloadState": tracked_state,
        "statusMessages": [{"title": title, "messages": list(lines)}
                           for title, lines in messages],
        "downloadId": "6F1C0B5E8A2D4E7F9A1B3C5D7E9F1A2B3C4D5E6F",
        "protocol": "torrent",
        "downloadClient": "RDTClient",
        "indexer": "YggTorrent",
        "outputPath": "/data/Downloads/radarr/Some.Movie.2021.MULTi.1080p.WEB.x264-GRP",
    }
    # Radarr/Sonarr leave null fields out of their answers.
    if error:
        record["errorMessage"] = error
    if added is not None:
        record["added"] = added
    return record


ACCESS_DENIED = ("Some.Movie.2021.mkv",
                 ["Access to the path '/data/Movies/Some Movie (2021)' is denied."])
NO_FILES = ("Some.Movie.2021.MULTi.1080p.WEB.x264-GRP",
            ["No files found are eligible for import in "
             "/data/Downloads/radarr/Some.Movie.2021.MULTi.1080p.WEB.x264-GRP"])
SAMPLE_UNKNOWN = ("Some.Movie.2021.mkv", ["Unable to determine if file is a sample"])
CLIENT_ERROR = "qBittorrent is reporting an error"

# (label, record, expected action) — rules that hold whatever the row's
# download looks like on disk.
TABLE = [
    # Still downloading, nothing wrong: left alone.
    ("healthy download", row(added=ago(10)), "keep"),
    ("queued, recent", row(status="queued", added=ago(1)), "keep"),
    # No usable date: no age rule can fire.
    ("no added date", row(), "keep"),
    ("unparseable added date", row(added="yesterday"), "keep"),

    # Local, recoverable problem on our side: never removed.
    ("permission denied on a download",
     row(error="Permission denied", added=ago(100)), "skip_recover"),
    ("access to the path denied",
     row(messages=[ACCESS_DENIED], added=ago(10)), "skip_recover"),
    # The local rule comes before the bad-release rule.
    ("local problem beats bad-release keyword",
     row(error="Permission denied, archive corrupt", added=ago(1)), "skip_recover"),

    # The release itself is unusable: removed and blocklisted.
    ("unpacking failed", row(error="Unpacking failed", added=ago(1)),
     "remove_blocklist"),
    ("corrupt archive", row(messages=[("x.rar", ["CRC error: corrupt"])],
                            added=ago(1)), "remove_blocklist"),

    # The download client reports an error on this grab: 6-hour grace.
    ("client error within the grace",
     row(status="warning", error=CLIENT_ERROR, added=ago(2)), "keep"),
    ("client error after the grace",
     row(status="warning", error=CLIENT_ERROR, added=ago(7)), "remove_blocklist"),
    ("client error without a date",
     row(status="warning", error=CLIENT_ERROR), "remove_blocklist"),
    ("failed with an error message, within the grace",
     row(status="failed", error=CLIENT_ERROR, added=ago(2)), "keep"),
    ("failed with an error message, after the grace",
     row(status="failed", error=CLIENT_ERROR, added=ago(7)), "remove_blocklist"),

    # A warning without a clearer signal: removed and blocklisted.
    ("tracked warning", row(tracked_status="warning", added=ago(1)),
     "remove_blocklist"),

    # Days in the queue without importing: dead on the client side.
    ("stuck more than 72 h", row(added=ago(80)), "remove_blocklist"),
    ("under 72 h", row(added=ago(70)), "keep"),
    ("delayed release stuck more than 72 h",
     row(status="delay", added=ago(80)), "remove_blocklist"),

    # Finished, not imported: import it rather than throw it away.
    ("import pending", row(status="completed", tracked_state="importPending",
                           added=ago(10)), "try_import"),
    ("import pending without a date",
     row(status="completed", tracked_state="importPending"), "try_import"),
    ("import blocked more than 72 h",
     row(status="completed", tracked_state="importBlocked", added=ago(80)),
     "remove_blocklist"),
    # 'sample' is not a classification keyword: the manual import is tried.
    ("sample undetermined",
     row(status="completed", tracked_state="importBlocked",
         messages=[SAMPLE_UNKNOWN], added=ago(10)), "try_import"),
    ("no files found, import pending",
     row(status="completed", tracked_state="importPending",
         messages=[NO_FILES], added=ago(1)), "remove_blocklist"),
]

for label, record, want in TABLE:
    check(label, classify_item(record, NOW), want)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_queue_policy: OK")
