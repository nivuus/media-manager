#!/usr/bin/env python3
"""atomic_env.write_env: the atomic .env write shared by install.py and activate.py.

A .env on a live host carries the real API keys and the Plex claim token, and
both hooks may run against an already-configured target more than once. Two
properties matter: a write is never observed half-done (temp file + rename,
never an in-place truncate), and a write that changes nothing must not touch
the file at all — same inode, same mtime — so a reinstall does not make an
operator's timestamp diff lie about what changed.

Run: python3 tests/test_atomic_env.py
"""
import os
import pathlib
import stat
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))

from atomic_env import write_env  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# --- A fresh file is created, mode 0600 -------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    path = pathlib.Path(tmp) / ".env"
    write_env(str(path), "RADARR_API_KEY=abc\n")
    check("fresh file: written", path.read_text(), "RADARR_API_KEY=abc\n")
    check("fresh file: mode 0600",
          stat.S_IMODE(path.stat().st_mode), 0o600)

# --- Changed content replaces the file, still 0600 --------------------------
with tempfile.TemporaryDirectory() as tmp:
    path = pathlib.Path(tmp) / ".env"
    path.write_text("A=1\n")
    os.chmod(path, 0o644)  # simulate a file that predates this mode guarantee
    before_inode = path.stat().st_ino

    write_env(str(path), "A=2\n")
    check("changed content: new content", path.read_text(), "A=2\n")
    check("changed content: mode fixed to 0600",
          stat.S_IMODE(path.stat().st_mode), 0o600)
    # os.replace() over the same directory is a rename: a *different* inode
    # is exactly what proves the write went through a temp file, not an
    # in-place truncate (which a reader could observe half-written).
    check("changed content: inode changed (atomic rename)",
          path.stat().st_ino != before_inode, True)

# --- Unchanged content leaves the file completely untouched -----------------
with tempfile.TemporaryDirectory() as tmp:
    path = pathlib.Path(tmp) / ".env"
    path.write_text("A=1\nB=2\n")
    os.chmod(path, 0o600)
    before = path.stat()

    write_env(str(path), "A=1\nB=2\n")
    after = path.stat()
    check("unchanged: same inode", after.st_ino, before.st_ino)
    check("unchanged: same mtime_ns", after.st_mtime_ns, before.st_mtime_ns)
    check("unchanged: content intact", path.read_text(), "A=1\nB=2\n")

# --- No leftover temporary file in the directory -----------------------------
with tempfile.TemporaryDirectory() as tmp:
    path = pathlib.Path(tmp) / ".env"
    write_env(str(path), "A=1\n")
    write_env(str(path), "A=2\n")
    check("no stray temp files",
          sorted(p.name for p in pathlib.Path(tmp).iterdir()), [".env"])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_atomic_env: OK")
