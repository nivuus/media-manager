#!/usr/bin/env python3
"""The queue policy of reset-error, checked as a table of rows and verdicts.

classify_item() is pure: a queue row as Radarr/Sonarr return it, the current
time, and whether the row's download is on disk go in; an action comes out.
Every rule is pinned with a literal expectation, the unchanged ones included,
so that a rule moved up or down the order shows up as a failed row rather
than as a download deleted in production.

Run: python3 tests/test_queue_policy.py
"""
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

# A local timezone far from UTC, set before anything reads it: reading a
# naive 'added' value as local time instead of UTC then shifts it by nine
# hours, which the naive-date rows below catch. A POSIX TZ string, so no
# tz database is needed.
os.environ["TZ"] = "JST-9"
time.tzset()

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


def verdict(record, **presence):
    """classify_item's answer; a crash is reported as a wrong answer."""
    try:
        return classify_item(record, NOW, **presence)
    except Exception as error:
        return f"raised {error!r}"


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


PERMISSION = ("Some.Movie.2021.mkv", ["Failed to import movie, Permission denied"])
PATH_MISSING = ("Some.Movie.2021.MULTi.1080p.WEB.x264-GRP",
                ["Import failed, path does not exist or is not accessible by "
                 "Radarr: /data/Downloads/radarr/Some.Movie.2021.MULTi.1080p.WEB.x264-GRP"])
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

    # Failed downloads, with the values the API really returns: status
    # 'failed' from the client, trackedDownloadState 'failed' once Radarr/
    # Sonarr processed it. Checked after the client-error rule, which keeps
    # its grace for a failed row that carries an error message (above).
    ("failed without an error message",
     row(status="failed", added=ago(1)), "remove_blocklist"),
    ("tracked state failed",
     row(status="completed", tracked_status="error", tracked_state="failed",
         added=ago(1)), "remove_blocklist"),

    # A warning without a clearer signal: removed and blocklisted.
    ("tracked warning", row(tracked_status="warning", added=ago(1)),
     "remove_blocklist"),

    # Days in the queue without importing: dead on the client side.
    ("stuck more than 72 h", row(added=ago(80)), "remove_blocklist"),
    ("under 72 h", row(added=ago(70)), "keep"),
    ("delayed release stuck more than 72 h",
     row(status="delay", added=ago(80)), "remove_blocklist"),
    # A release Radarr/Sonarr could not hand to an unreachable client: the
    # release is fine, removing it with a blocklist would throw it away.
    ("client unavailable more than 72 h",
     row(status="downloadClientUnavailable", added=ago(100)), "keep"),

    # An 'added' without an offset is UTC, as Radarr/Sonarr store it.
    ("naive date, 71.5 h in UTC", row(added="2026-09-25T06:30:00"), "keep"),
    ("naive date, 72.5 h in UTC", row(added="2026-09-25T05:30:00"),
     "remove_blocklist"),

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
    check(label, verdict(record), want)

IMPORT_PENDING = dict(status="completed", tracked_status="warning",
                      tracked_state="importPending")
IMPORT_BLOCKED = dict(IMPORT_PENDING, tracked_state="importBlocked")

# (label, record, download on disk, expected action). Whether the download is
# still on disk is established by the caller from the Downloads directory:
# True, False, or None when it could not be (no outputPath, or the directory
# could not be listed).
PRESENCE_TABLE = [
    # Its files are gone: remove the row without blocklisting, whatever the
    # messages or the age say — the release was fine.
    ("absent", row(**IMPORT_PENDING, added=ago(10)), False, "remove_files_gone"),
    ("absent beats the 72 h rule",
     row(**IMPORT_BLOCKED, added=ago(80)), False, "remove_files_gone"),
    ("absent without a date", row(**IMPORT_PENDING), False, "remove_files_gone"),
    ("absent, 'no files found' message",
     row(**IMPORT_PENDING, messages=[NO_FILES], added=ago(1)), False,
     "remove_files_gone"),
    ("absent, 'path does not exist' message",
     row(**IMPORT_BLOCKED, messages=[PATH_MISSING], added=ago(1)), False,
     "remove_files_gone"),

    # On disk: a local problem keeps the import going and is never removed
    # by age; unimportable content is removed + blocklisted; the rest is
    # imported, and removed + blocklisted after 72 h.
    ("present", row(**IMPORT_PENDING, added=ago(10)), True, "try_import"),
    ("present more than 72 h",
     row(**IMPORT_BLOCKED, added=ago(80)), True, "remove_blocklist"),
    ("present without a date", row(**IMPORT_PENDING), True, "try_import"),
    ("present, permission denied",
     row(**IMPORT_PENDING, messages=[PERMISSION], added=ago(10)), True,
     "try_import_recoverable"),
    ("present, access denied for 200 h",
     row(**IMPORT_PENDING, messages=[ACCESS_DENIED], added=ago(200)), True,
     "try_import_recoverable"),
    # A path-mapping fault with the files intact: removing it from the client
    # would delete a good download.
    ("present, 'path does not exist' for 100 h",
     row(**IMPORT_BLOCKED, messages=[PATH_MISSING], added=ago(100)), True,
     "try_import_recoverable"),
    # Unimportable content (archives, say): removing it without a blocklist
    # would re-open the re-grab loop.
    ("present, 'no files found'",
     row(**IMPORT_PENDING, messages=[NO_FILES], added=ago(1)), True,
     "remove_blocklist"),
    ("present, unpacking failed",
     row(**IMPORT_PENDING, error="Unpacking failed", added=ago(1)), True,
     "remove_blocklist"),
    ("present, local problem before unimportable content",
     row(**IMPORT_PENDING, messages=[PERMISSION, NO_FILES], added=ago(1)), True,
     "try_import_recoverable"),
    # 'sample' is not a classification keyword: the manual import is tried.
    ("present, sample undetermined",
     row(**IMPORT_BLOCKED, messages=[SAMPLE_UNKNOWN], added=ago(10)), True,
     "try_import"),

    # Unknown: nothing is concluded from presence, the other rules apply.
    ("unknown", row(**IMPORT_PENDING, added=ago(10)), None, "try_import"),
    ("unknown, more than 72 h",
     row(**IMPORT_PENDING, added=ago(80)), None, "remove_blocklist"),
    ("unknown, permission denied for 100 h",
     row(**IMPORT_PENDING, messages=[PERMISSION], added=ago(100)), None,
     "try_import_recoverable"),
    ("unknown, 'no files found'",
     row(**IMPORT_PENDING, messages=[NO_FILES], added=ago(1)), None,
     "remove_blocklist"),

    # Only an import-pending row is judged on presence: a download that has
    # not started yet has nothing on disk either.
    ("absent, still downloading", row(added=ago(1)), False, "keep"),
    ("absent, queued", row(status="queued", added=ago(1)), False, "keep"),
]

for label, record, present, want in PRESENCE_TABLE:
    check(f"presence: {label}", verdict(record, download_present=present), want)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_queue_policy: OK")
