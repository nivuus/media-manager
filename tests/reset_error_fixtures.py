"""Fixtures for the tests of reset-error's run: a healthy stack, and how to run against it.

The rows mirror what Radarr/Sonarr's /api/v3/queue returns, the routes answer
as a healthy stack would — download clients that pass their test, a queue
refresh that completes — and the Downloads tree is real, in a temporary
directory, so that the purge's effect is read on the filesystem. Not a test
itself: the Makefile only runs the test_* files. Import it after setting
sys.dont_write_bytecode, so that no __pycache__ lands in stack/ or tests/.
"""
import contextlib
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

STACK = pathlib.Path(__file__).resolve().parents[1] / "stack"
if str(STACK) not in sys.path:
    sys.path.insert(0, str(STACK))

from maintenance import reset_error  # noqa: E402
from maintenance_fakes import FakeApi, FakeClock, LogCapture, reply  # noqa: E402

RADARR = "http://radarr.test:7878/api/v3"
SONARR = "http://sonarr.test:8989/api/v3"
CAPTURE = LogCapture().install()


def iso(hours_ago):
    """An 'added' value as the API sends it: ISO 8601, UTC, 'Z' suffix."""
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def queue_page(records, total=None):
    """The paging resource /api/v3/queue answers with."""
    return {"page": 1, "pageSize": 1000, "sortKey": "timeleft",
            "sortDirection": "ascending",
            "totalRecords": len(records) if total is None else total,
            "records": records}


MOVIE = "Some.Movie.2021.MULTi.1080p.WEB.x264-GRP"
PENDING = "Other.Movie.2020.MULTi.1080p.BluRay.x264-GRP"
EPISODE = "Some.Show.S02E03.1080p.WEB.h264-GRP"
VANISHED = "Gone.Movie.2019.MULTi.1080p.WEB.x264-GRP"
UNREFERENCED = "radarr/Old.Unreferenced.Movie.2019/movie.mkv"

# A Radarr row still downloading: kept, and its files protected from the purge.
DOWNLOADING = {
    "id": 101, "movieId": 812, "title": MOVIE,
    "status": "downloading", "trackedDownloadStatus": "ok",
    "trackedDownloadState": "downloading", "statusMessages": [],
    "downloadId": "A1B2C3D4E5F60718293A4B5C6D7E8F9012345678",
    "protocol": "torrent", "downloadClient": "RDTClient", "indexer": "YggTorrent",
    "outputPath": f"/data/Downloads/radarr/{MOVIE}",
    "size": 4_800_000_000, "sizeleft": 1_200_000_000, "added": iso(2),
}
# A Radarr row whose import is pending and that Radarr cannot date (no grab in
# its history): the manual import is what should happen to it.
IMPORT_PENDING = {
    "id": 103, "movieId": 813, "title": PENDING,
    "status": "completed", "trackedDownloadStatus": "warning",
    "trackedDownloadState": "importPending",
    "statusMessages": [{"title": PENDING, "messages": [
        "Movie title mismatch, automatic import is not possible. "
        "Manual Import required."]}],
    "downloadId": "C3D4E5F60718293A4B5C6D7E8F901234567890AB",
    "protocol": "torrent", "downloadClient": "RDTClient", "indexer": "YggTorrent",
    "outputPath": f"/data/Downloads/radarr/{PENDING}",
    "size": 8_000_000_000, "sizeleft": 0,
}
# A Radarr row whose import is pending and whose download is not on disk.
IMPORT_VANISHED = {
    "id": 104, "movieId": 814, "title": VANISHED,
    "status": "completed", "trackedDownloadStatus": "warning",
    "trackedDownloadState": "importPending",
    "statusMessages": [{"title": VANISHED, "messages": [
        "One or more movies expected in this release were not imported or missing"]}],
    "downloadId": "D4E5F60718293A4B5C6D7E8F901234567890ABCD",
    "protocol": "torrent", "downloadClient": "RDTClient", "indexer": "YggTorrent",
    "outputPath": f"/data/Downloads/radarr/{VANISHED}",
    "size": 6_000_000_000, "sizeleft": 0, "added": iso(5),
}
# A Sonarr row with a warning and no clearer signal: removed + blocklisted.
STALLED = {
    "id": 202, "seriesId": 77, "episodeId": 9001, "title": EPISODE,
    "status": "downloading", "trackedDownloadStatus": "warning",
    "trackedDownloadState": "downloading",
    "statusMessages": [{"title": EPISODE, "messages": [
        "The download is stalled with no connections"]}],
    "downloadId": "B2C3D4E5F60718293A4B5C6D7E8F90123456789A",
    "protocol": "torrent", "downloadClient": "RDTClient", "indexer": "YggTorrent",
    "outputPath": f"/data/Downloads/tv-sonarr/{EPISODE}",
    "size": 1_500_000_000, "sizeleft": 1_500_000_000, "added": iso(30),
    "episode": {"seasonNumber": 2, "episodeNumber": 3},
}
# What /api/v3/rootfolder answers while the media disk is mounted.
RADARR_ROOT = {"id": 1, "path": "/data/Movies", "accessible": True,
               "freeSpace": 1_234_567_890_000, "unmappedFolders": []}
SONARR_ROOT = {"id": 1, "path": "/data/TV Shows", "accessible": True,
               "freeSpace": 1_234_567_890_000, "unmappedFolders": []}
# What /api/v3/downloadclient/testall answers when every enabled client passes.
TESTALL_PASSED = [{"id": 1, "isValid": True, "validationFailures": []}]
# The id each instance gives the RefreshMonitoredDownloads command.
REFRESH_ID = {RADARR: 4242, SONARR: 5151}
# What Radarr answers /manualimport with for the pending download on disk.
CANDIDATES = [{
    "id": 1, "path": f"/data/Downloads/radarr/{PENDING}/movie.mkv",
    "relativePath": "movie.mkv", "folderName": PENDING, "name": "movie",
    "size": 8_000_000_000,
    "movie": {"id": 813, "title": "Other Movie", "year": 2020},
    "quality": {"quality": {"id": 7, "name": "Bluray-1080p"},
                "revision": {"version": 1, "real": 0, "isRepack": False}},
    "languages": [{"id": 2, "name": "French"}],
    "releaseGroup": "GRP", "downloadId": IMPORT_PENDING["downloadId"],
    "rejections": [],
}]


# The library directories sit next to Downloads, under the media root, as
# hooks/install.py derives MOVIES_DIR and TV_DIR from MEDIA_ROOT; with the
# media disk mounted, each holds a title.
LIBRARY_FILES = {
    "Movies": "Some Movie (2021)/Some.Movie.2021.MULTi.1080p.WEB.x264-GRP.mkv",
    "TV Shows": "Some Show/Season 02/Some.Show.S02E01.1080p.WEB.h264-GRP.mkv",
}


def make_downloads(tmp, library=True):
    """A media root as the stack leaves it; returns its Downloads directory.

    Downloads is as rdtclient leaves it, every file two days old. Next to it
    are the library directories: each holding one title when `library` is
    true, both empty otherwise, as Docker recreates them when the media disk
    is missing at boot.
    """
    media = pathlib.Path(tmp)
    root = media / "Downloads"
    old = time.time() - 48 * 3600
    for rel in (f"radarr/{MOVIE}/movie.mkv", f"radarr/{PENDING}/movie.mkv",
                UNREFERENCED):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 16)
        os.utime(path, (old, old))
    (root / "tv-sonarr").mkdir()
    for directory, rel in LIBRARY_FILES.items():
        (media / directory).mkdir()
        if library:
            title = media / directory / rel
            title.parent.mkdir(parents=True)
            title.write_bytes(b"\0" * 16)
    return root


def environment(downloads):
    media = pathlib.Path(downloads).parent
    return {
        "RADARR_URL": "http://radarr.test:7878", "RADARR_API_KEY": "radarr-key",
        "SONARR_URL": "http://sonarr.test:8989", "SONARR_API_KEY": "sonarr-key",
        "DOWNLOADS_DIR": str(downloads),
        "MOVIES_DIR": str(media / "Movies"), "TV_DIR": str(media / "TV Shows"),
        # chown to ourselves works as any user; the test must not need root.
        "PUID": str(os.getuid()), "PGID": str(os.getgid()),
    }


def command(command_id, name, status):
    """A command as /api/v3/command answers it."""
    return {"id": command_id, "name": name, "commandName": name,
            "status": status, "priority": "normal", "trigger": "manual"}


def created(command_id):
    """The POST /api/v3/command route: 201, the command queued under the name sent."""
    def answer(json=None, **_):
        return reply(201, command(command_id, json["name"], "queued"))
    return answer


def refresh_status(base, status):
    """What GET /api/v3/command/{id} answers for the queue refresh."""
    return reply(200, command(REFRESH_ID[base], "RefreshMonitoredDownloads", status))


def routes(radarr_queue=None, sonarr_queue=None):
    """Every route a run uses, answering as a healthy stack would."""
    table = {
        ("GET", f"{RADARR}/rootfolder"): reply(200, [RADARR_ROOT]),
        ("GET", f"{SONARR}/rootfolder"): reply(200, [SONARR_ROOT]),
        ("GET", f"{RADARR}/queue"): reply(200, queue_page(
            [DOWNLOADING] if radarr_queue is None else radarr_queue)),
        ("GET", f"{SONARR}/queue"): reply(200, queue_page(
            [STALLED] if sonarr_queue is None else sonarr_queue)),
        ("DELETE", f"{SONARR}/queue/202"): reply(200, {}),
        ("GET", f"{RADARR}/movie"): reply(200, []),
        ("GET", f"{SONARR}/series"): reply(200, []),
    }
    for base in (RADARR, SONARR):
        table[("POST", f"{base}/downloadclient/testall")] = reply(200, TESTALL_PASSED)
        table[("GET", f"{base}/health")] = reply(200, [])
        table[("POST", f"{base}/command")] = created(REFRESH_ID[base])
        table[("GET", f"{base}/command/{REFRESH_ID[base]}")] = refresh_status(
            base, "completed")
    return table


def import_routes():
    """The routes, with import-pending rows: one on disk, one vanished."""
    table = routes(radarr_queue=[DOWNLOADING, IMPORT_PENDING, IMPORT_VANISHED])
    table[("GET", f"{RADARR}/manualimport")] = reply(200, [])
    table[("DELETE", f"{RADARR}/queue/103")] = reply(200, {})
    table[("DELETE", f"{RADARR}/queue/104")] = reply(200, {})
    return table


def run(table, downloads, patches=(), clock=None, environ=None):
    """One reset-error run; returns (exit status, fake API, log lines).

    The run sees environment(downloads) unless `environ` is given. The queue
    refresh is polled on `clock`, a fresh FakeClock by default: no case ever
    sleeps for real.
    """
    api = FakeApi(table)
    CAPTURE.lines.clear()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("requests.request", new=api))
        stack.enter_context(mock.patch("maintenance.queue_trust.time",
                                       new=clock or FakeClock()))
        for target, error in patches:
            stack.enter_context(mock.patch(target, side_effect=error))
        try:
            code = reset_error.run(environment(downloads) if environ is None else environ)
        except Exception as error:  # a crash fails this case, not the whole file
            code = f"raised {error!r}"
    return code, api, list(CAPTURE.lines)


def commands(api, base, name):
    """The body of every command of that name POSTed to one instance."""
    return [kwargs["json"] for kwargs in api.made("POST", f"{base}/command")
            if kwargs["json"]["name"] == name]


@contextlib.contextmanager
def case(label, failures):
    """Attach the run's log to the failures of one case, and only then."""
    before = len(failures)
    yield
    if len(failures) > before:
        failures.append(f"  [{label}] log:\n    " + "\n    ".join(CAPTURE.lines))
