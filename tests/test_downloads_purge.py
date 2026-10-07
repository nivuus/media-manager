#!/usr/bin/env python3
"""What reset-error sees in the Downloads directory, on a real temporary tree.

An import-pending row whose download is absent is removed from its client,
so reading "absent" wrongly deletes a good download. The listing is
therefore every name at any depth — SABnzbd files a finished job under
complete/<category>/, not directly in a category subdirectory — and a
directory that cannot be listed is an error, never an empty answer.

Run: python3 tests/test_downloads_purge.py
"""
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

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_downloads_purge: OK")
