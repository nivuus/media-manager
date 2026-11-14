#!/usr/bin/env python3
"""What reset-error sees in the Downloads directory, on a real temporary tree.

An import-pending row whose download is absent is removed from its client,
so reading "absent" wrongly deletes a good download. The listing is
therefore every name at any depth — SABnzbd files a finished job under
complete/<category>/, not directly in a category subdirectory — and a
directory that cannot be listed is an error, never an empty answer.

Run: python3 tests/test_downloads_purge.py
"""
import os
import pathlib
import sys
import tempfile

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stack"))

from maintenance.arr_api import Failures  # noqa: E402
from maintenance.downloads_purge import (  # noqa: E402
    download_present, names_under, purge)
from maintenance_fakes import LogCapture  # noqa: E402

LogCapture().install()  # recorded failures are logged: keep them off the output

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp) / "Downloads"
    (root / "radarr" / "A.Movie").mkdir(parents=True)
    (root / "radarr" / "A.Movie" / "a.mkv").write_bytes(b"")
    (root / "tv-sonarr").mkdir()
    (root / "tv-sonarr" / "B.Show.S01E01.mkv").write_bytes(b"")
    (root / "complete" / "movies" / "C.Movie").mkdir(parents=True)
    names = names_under(str(root))

    def present(output_path, listing=names):
        return download_present({"outputPath": output_path}, listing)

    # outputPath is the download client's path as Radarr/Sonarr see it, not
    # the host's: only its last component can be matched.
    check("folder download", present("/data/Downloads/radarr/A.Movie"), True)
    check("single-file download",
          present("/data/Downloads/tv-sonarr/B.Show.S01E01.mkv"), True)
    check("job nested under complete/<category>/",
          present("/data/downloads/complete/movies/C.Movie"), True)
    check("trailing slash", present("/data/Downloads/radarr/A.Movie/"), True)
    check("vanished download", present("/data/Downloads/radarr/Gone.Movie"), False)
    # Unknown, never absent: nothing may be concluded from these.
    check("row without outputPath", download_present({"title": "A.Movie"}, names), None)
    check("no listing", present("/data/Downloads/radarr/A.Movie", None), None)
    check("outputPath of the root only", present("/"), None)

    try:
        names_under(str(root / "absent"))
        failures.append("unlistable directory: nothing raised")
    except OSError:
        pass

# --- The purge never creates a missing Downloads directory -----------------
# The storage guard stops a run before the purge when Downloads is missing;
# the purge keeps its own contract anyway: report, and create nothing, since
# with the media disk unmounted that would write into the bare mount point.
with tempfile.TemporaryDirectory() as tmp:
    missing = pathlib.Path(tmp) / "Downloads"
    recorded = Failures()
    purge(str(missing), set(), (0, 0), recorded, "20261007T040000Z")
    check("purge of a missing directory: failure recorded", recorded.count, 1)
    check("purge of a missing directory: not created", missing.exists(), False)

# --- The category directories are restored, never through a symlink -------
# The purge runs as root inside a directory the download clients write. An
# ownership change made by path would follow a symlink planted in place of a
# category directory and hand its target to PUID:PGID. The spy sees every
# chown/fchown, so the check holds without root: a run as an ordinary user
# would "change" an owner to the same value, which a stat cannot tell apart.
OWNER = (os.getuid(), os.getgid())


class RecordedFailures(Failures):
    """Failures that also keep each context, to tell which step recorded what."""

    def __init__(self):
        super().__init__()
        self.contexts = []

    def record(self, context, error):
        super().record(context, error)
        self.contexts.append(context)


class OwnershipSpy:
    """Records the inode of every target os.chown / os.fchown is called on."""

    def __init__(self):
        self.inodes = []
        self._chown, self._fchown = os.chown, os.fchown

    def __enter__(self):
        def chown(path, uid, gid, *args, **kwargs):
            follow = kwargs.get("follow_symlinks", True)
            self.inodes.append(os.stat(path, dir_fd=kwargs.get("dir_fd"),
                                       follow_symlinks=follow).st_ino)
            return self._chown(path, uid, gid, *args, **kwargs)

        def fchown(fd, uid, gid):
            self.inodes.append(os.fstat(fd).st_ino)
            return self._fchown(fd, uid, gid)

        os.chown, os.fchown = chown, fchown
        return self

    def __exit__(self, *exc):
        os.chown, os.fchown = self._chown, self._fchown


with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp) / "Downloads"
    root.mkdir()
    (root / "radarr").mkdir()  # tv-sonarr missing: the purge creates it
    recorded = Failures()
    with OwnershipSpy() as spy:
        purge(str(root), set(), OWNER, recorded, "20261007T040000Z")
    check("restore: no failure", recorded.count, 0)
    for name in ("radarr", "tv-sonarr"):
        path = root / name
        check(f"restore: {name} is a directory",
              path.is_dir() and not path.is_symlink(), True)
        check(f"restore: {name} owner", (path.stat().st_uid, path.stat().st_gid), OWNER)
        check(f"restore: {name} ownership set", path.stat().st_ino in spy.inodes, True)

with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp) / "Downloads"
    root.mkdir()
    outside = pathlib.Path(tmp) / "outside"
    outside.mkdir(mode=0o700)
    os.chmod(outside, 0o700)
    before = outside.stat()
    (root / "radarr").symlink_to(outside)
    (root / "tv-sonarr").write_bytes(b"")  # a file where a category belongs
    recorded = RecordedFailures()
    with OwnershipSpy() as spy:
        purge(str(root), set(), OWNER, recorded, "20261007T040000Z")
    after = outside.stat()
    check("symlinked category: target never chowned", before.st_ino in spy.inodes, False)
    check("symlinked category: target owner and mode unchanged",
          (after.st_uid, after.st_gid, after.st_mode),
          (before.st_uid, before.st_gid, before.st_mode))
    check("symlinked category: link left in place", (root / "radarr").is_symlink(), True)
    restore_failures = sorted(c for c in recorded.contexts if "cannot restore" in c)
    check("symlinked category and file category: each restore refused and recorded",
          restore_failures,
          [f"Downloads purge: cannot restore {root / 'radarr'}",
           f"Downloads purge: cannot restore {root / 'tv-sonarr'}"])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_downloads_purge: OK")
