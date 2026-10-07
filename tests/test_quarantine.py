#!/usr/bin/env python3
"""The 24-hour purge moves files into the quarantine instead of deleting them,
and the quarantine is emptied only after its own grace period, on a real
temporary tree.

A purge verdict is only as good as the queues it was made on. Moved rather
than deleted, a wrong one is a file to move back for QUARANTINE_HOURS.

Run: python3 tests/test_quarantine.py
"""
import errno
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stack"))

from maintenance import quarantine  # noqa: E402
from maintenance.arr_api import Failures  # noqa: E402
from maintenance.downloads_purge import names_under, purge  # noqa: E402
from maintenance_fakes import LogCapture  # noqa: E402

LogCapture().install()

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


NOW = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)
BATCH = quarantine.batch_name(NOW)


def old_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * 8)
    old = time.time() - 48 * 3600
    os.utime(path, (old, old))


# --- Old unreferenced files are moved, keeping their relative path ----------
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp) / "Downloads"
    old_file(root / "radarr" / "Old.Movie" / "movie.mkv")
    old_file(root / "radarr" / "Kept.Movie" / "movie.mkv")
    recorded = Failures()
    purge(str(root), {"Kept.Movie"}, (os.getuid(), os.getgid()), recorded, BATCH)
    moved = root / ".quarantine" / BATCH / "radarr" / "Old.Movie" / "movie.mkv"
    check("move: no failure", recorded.count, 0)
    check("move: gone from Downloads", (root / "radarr" / "Old.Movie").exists(), False)
    check("move: in the quarantine, same relative path", moved.is_file(), True)
    check("move: protected file untouched",
          (root / "radarr" / "Kept.Movie" / "movie.mkv").is_file(), True)

    # The quarantine is not Downloads: its names never make a row look present,
    # and a second purge neither moves it again nor removes its directory.
    check("listing ignores the quarantine", "Old.Movie" in names_under(str(root)), False)
    check("listing ignores the quarantine dir",
          ".quarantine" in names_under(str(root)), False)
    purge(str(root), set(), (os.getuid(), os.getgid()), recorded,
          quarantine.batch_name(NOW + timedelta(days=1)))
    check("second purge: quarantined file left in its batch", moved.is_file(), True)
    check("second purge: no nested quarantine",
          (root / ".quarantine" / quarantine.batch_name(NOW + timedelta(days=1))
           / ".quarantine").exists(), False)

    # An empty quarantine directory is kept: the purge removes empty
    # directories, never the quarantine itself.
    for batch_dir in (root / ".quarantine").iterdir():
        if not any(batch_dir.rglob("*.mkv")):
            batch_dir.rmdir()
    moved.unlink()
    purge(str(root), set(), (os.getuid(), os.getgid()), recorded, BATCH)
    check("quarantine dir survives the empty-directory sweep",
          (root / ".quarantine").is_dir(), True)

# --- The empty-directory sweep never enters the quarantine ------------------
# Its batches are the quarantine's own business: kept whole until they expire.
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp) / "Downloads"
    empty_in_batch = root / ".quarantine" / BATCH / "radarr" / "Restored.Movie"
    empty_in_batch.mkdir(parents=True)
    (root / "radarr" / "Empty.Leftover").mkdir(parents=True)
    purge(str(root), set(), (os.getuid(), os.getgid()), Failures(), BATCH)
    check("sweep: empty directory in Downloads removed",
          (root / "radarr" / "Empty.Leftover").exists(), False)
    check("sweep: empty directory inside a batch left alone", empty_in_batch.is_dir(), True)

# --- A rename that would cross a mount point: the file stays, recorded -------
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp) / "Downloads"
    source = root / "radarr" / "Other.Fs" / "movie.mkv"
    old_file(source)
    recorded = Failures()
    with mock.patch("os.rename", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
        purge(str(root), set(), (os.getuid(), os.getgid()), recorded, BATCH)
    check("cross-device: failure recorded", recorded.count, 1)
    check("cross-device: file left in place, never copied nor deleted",
          source.is_file(), True)

# --- Expiry: only batches older than the grace period, only ours -------------
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp) / "Downloads"
    qroot = root / ".quarantine"
    expired = quarantine.batch_name(NOW - timedelta(hours=quarantine.QUARANTINE_HOURS + 1))
    recent = quarantine.batch_name(NOW - timedelta(hours=quarantine.QUARANTINE_HOURS - 1))
    for batch in (expired, recent, "not-a-batch"):
        old_file(qroot / batch / "radarr" / "X" / "x.mkv")
    recorded = Failures()
    quarantine.expire(str(root), NOW, recorded)
    check("expiry: no failure", recorded.count, 0)
    check("expiry: old batch deleted", (qroot / expired).exists(), False)
    check("expiry: recent batch kept", (qroot / recent).is_dir(), True)
    check("expiry: a directory it did not write is left alone",
          (qroot / "not-a-batch").is_dir(), True)

    # No quarantine yet: nothing to do, nothing recorded.
    recorded = Failures()
    quarantine.expire(str(pathlib.Path(tmp) / "Elsewhere"), NOW, recorded)
    check("expiry without a quarantine: silent", recorded.count, 0)

check("batch name round-trips", quarantine.batch_time(BATCH), NOW)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_quarantine: OK")
