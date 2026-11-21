#!/usr/bin/env python3
"""Radarr's and Sonarr's recycle bin (hooks/recycle_bin.py), and its wiring
into the activate hook.

Against a fake HTTP opener and scratch directories only: the reference host
runs the production Radarr/Sonarr on the loopback and its media under
/media/data, and nothing here may reach either.

- the setting is applied only while it is empty, with every other field of
  the media management config sent back unchanged;
- a path chosen by hand is left alone, and said so;
- an app without an API key is reported, not called;
- an unreachable app, or one refusing the setting, fails by name;
- the directories are created per app under MEDIA_ROOT/.recycle-bin, a
  symlink at either level refused;
- the activate hook runs the step with the effective .env, and fails the
  phase (before the timers) when it fails.

Run: python3 tests/test_recycle_bin.py
"""
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import urllib.error
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "tests"))

import activate_fakes  # noqa: E402
import recycle_bin  # noqa: E402
from activate_fakes import CONFIG, CONFIG_XML, PS_A, VERSION, completed  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


RADARR, SONARR = (recycle_bin.APPS[app][0] for app in ("radarr", "sonarr"))
KEYS = {"RADARR_API_KEY": "rk", "SONARR_API_KEY": "sk"}


def config(bin_value):
    return {"id": 1, "recycleBin": bin_value, "recycleBinCleanupDays": 7,
            "autoUnmonitorPreviouslyDownloadedMovies": False,
            "createEmptyMovieFolders": True}


class FakeArr:
    """Answers (method, url) from a table; records (method, url, key, body)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, request, timeout=None):
        key = (request.get_method(), request.full_url)
        body = json.loads(request.data) if request.data else None
        self.calls.append((*key, request.get_header("X-api-key"), body))
        if key not in self.answers:
            raise AssertionError(f"unexpected call: {key}")
        answer = self.answers[key]
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode() if answer is not None else b"")


def arrs(radarr_bin, sonarr_bin, extra=None):
    answers = {}
    for base, current in ((RADARR, radarr_bin), (SONARR, sonarr_bin)):
        answers[("GET", f"{base}/api/v3/system/status")] = {"version": "6"}
        answers[("GET", f"{base}/api/v3/config/mediamanagement")] = config(current)
        answers[("PUT", f"{base}/api/v3/config/mediamanagement/1")] = config("x")
    answers.update(extra or {})
    return answers


def puts(fake):
    return [(url, body) for method, url, _, body in fake.calls if method == "PUT"]


# --- Empty: set, every other field sent back as it was ------------------
fake = FakeArr(arrs("", ""))
messages = recycle_bin.configure(KEYS, opener=fake, sleep=lambda s: None)
sent = dict(puts(fake))
check("empty: one PUT per app", len(sent), 2)
radarr_body = sent[f"{RADARR}/api/v3/config/mediamanagement/1"]
check("empty: radarr path", radarr_body["recycleBin"], "/data/.recycle-bin/radarr")
check("empty: sonarr path",
      sent[f"{SONARR}/api/v3/config/mediamanagement/1"]["recycleBin"],
      "/data/.recycle-bin/sonarr")
check("empty: cleanup days", radarr_body["recycleBinCleanupDays"],
      recycle_bin.CLEANUP_DAYS)
check("empty: other fields kept",
      {k: v for k, v in radarr_body.items()
       if k not in ("recycleBin", "recycleBinCleanupDays")},
      {k: v for k, v in config("").items()
       if k not in ("recycleBin", "recycleBinCleanupDays")})
check("empty: each app called with its own key",
      {(url.split("/api")[0], key) for _, url, key, _ in fake.calls},
      {(RADARR, "rk"), (SONARR, "sk")})
check("empty: one message per app", len(messages), 2)
check("cleanup stays below Maintainerr's 14-day grace",
      recycle_bin.CLEANUP_DAYS < 14, True)

# --- Already ours: nothing written, nothing said -------------------------
fake = FakeArr(arrs("/data/.recycle-bin/radarr", "/data/.recycle-bin/sonarr"))
check("ours: no message",
      recycle_bin.configure(KEYS, opener=fake, sleep=lambda s: None), [])
check("ours: no PUT", puts(fake), [])

# --- Chosen by hand: left alone, said so ---------------------------------
fake = FakeArr(arrs("/data/my-bin", ""))
messages = recycle_bin.configure(KEYS, opener=fake, sleep=lambda s: None)
check("custom: radarr untouched",
      [url for url, _ in puts(fake)], [f"{SONARR}/api/v3/config/mediamanagement/1"])
check("custom: named", any("/data/my-bin" in m and "left as is" in m
                           for m in messages), True)

# --- No key: reported, never called --------------------------------------
fake = FakeArr(arrs("", ""))
messages = recycle_bin.configure({"RADARR_API_KEY": "rk"}, opener=fake,
                                 sleep=lambda s: None)
check("no key: sonarr never called",
      [c for c in fake.calls if c[1].startswith(SONARR)], [])
check("no key: named", any("SONARR_API_KEY" in m for m in messages), True)

# --- Unreachable: raises after the deadline, named -----------------------
refused = urllib.error.URLError("connection refused")
fake = FakeArr(arrs("", "", {("GET", f"{RADARR}/api/v3/system/status"): refused}))
ticks = iter(range(0, 10_000, 100))
try:
    recycle_bin.configure(KEYS, opener=fake, clock=lambda: next(ticks),
                          sleep=lambda s: None)
    check("unreachable: raises", "no error", "RecycleBinError")
except recycle_bin.RecycleBinError as exc:
    check("unreachable: names the endpoint", "system/status" in str(exc), True)

# --- Refused: the app's validation message is kept -----------------------
rejection = urllib.error.HTTPError(
    f"{RADARR}/api/v3/config/mediamanagement/1", 400, "Bad Request", {},
    io.BytesIO(b'[{"errorMessage": "Path does not exist"}]'))
fake = FakeArr(arrs("", "", {("PUT", f"{RADARR}/api/v3/config/mediamanagement/1"):
                             rejection}))
try:
    recycle_bin.configure(KEYS, opener=fake, sleep=lambda s: None)
    check("refused: raises", "no error", "RecycleBinError")
except recycle_bin.RecycleBinError as exc:
    check("refused: validation message kept", "Path does not exist" in str(exc), True)

# --- .env reading ---------------------------------------------------------
env = "MEDIA_ROOT=/old\n# MEDIA_ROOT=/commented\nMEDIA_ROOT='/srv/media/'\n"
check("env: last assignment, quotes and trailing slash stripped",
      recycle_bin.media_root(env), "/srv/media")
check("env: absent variable", recycle_bin.env_value(env, "RADARR_API_KEY"), "")
for bad in ("", "MEDIA_ROOT=\n", "MEDIA_ROOT=media\n"):
    try:
        recycle_bin.media_root(bad)
        check(f"env {bad!r}: refused", "no error", "RecycleBinError")
    except recycle_bin.RecycleBinError:
        pass

# --- Directories: one per app, 0750, symlinks refused at both levels -----
with tempfile.TemporaryDirectory() as media:
    recycle_bin.ensure_dirs(media, os.getuid(), os.getgid())
    for app in recycle_bin.APPS:
        path = os.path.join(media, ".recycle-bin", app)
        check(f"dirs: {app} created", os.path.isdir(path), True)
        check(f"dirs: {app} mode", stat.S_IMODE(os.stat(path).st_mode), 0o750)
    recycle_bin.ensure_dirs(media, os.getuid(), os.getgid())  # idempotent

for planted in (".recycle-bin", ".recycle-bin/radarr"):
    with tempfile.TemporaryDirectory() as media, \
            tempfile.TemporaryDirectory() as victim:
        os.makedirs(os.path.join(media, ".recycle-bin"), exist_ok=True)
        link = os.path.join(media, planted)
        if os.path.isdir(link):
            os.rmdir(link)
        os.symlink(victim, link)
        os.chmod(victim, 0o700)
        try:
            recycle_bin.ensure_dirs(media, os.getuid(), os.getgid())
            check(f"symlink at {planted}: refused", "no error", "RuntimeError")
        except RuntimeError as exc:
            check(f"symlink at {planted}: named", planted in str(exc), True)
        check(f"symlink at {planted}: target untouched",
              (stat.S_IMODE(os.stat(victim).st_mode), os.listdir(victim)),
              (0o700, []))

# --- Through the activate hook -------------------------------------------
spec = importlib.util.spec_from_file_location(
    "activate_hook", REPO / "hooks" / "activate.py")
activate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activate)
run_phase_with = activate_fakes.phase_runner(activate, REPO)

DEPLOY_FILES = {
    ".env": "PUID=1000\nPGID=1000\nMEDIA_ROOT=/srv/media\n"
            "RADARR_API_KEY=\nSONARR_API_KEY=set-by-hand\nPROWLARR_API_KEY=\n",
    "radarr/config.xml": CONFIG_XML.format("radarr-key"),
    "sonarr/config.xml": CONFIG_XML.format("sonarr-key"),
    "prowlarr/config.xml": CONFIG_XML.format("prowlarr-key"),
}
ROUTES = {
    VERSION: completed(VERSION),
    CONFIG: completed(CONFIG, stdout="radarr\nsonarr\n"),
    PS_A: completed(PS_A, stdout="radarr\nsonarr\n"),
    **activate_fakes.timer_routes(),
}

seen = []
code, err = run_phase_with(ROUTES, deploy_files=DEPLOY_FILES,
                           recycle=seen.append)
check("hook: success", (code, err), (0, ""))
check("hook: run once with the effective .env",
      [("RADARR_API_KEY=radarr-key" in t, "SONARR_API_KEY=set-by-hand" in t)
       for t in seen], [(True, True)])

# The real step, its two side effects faked: dirs and keys as the .env says.
with mock.patch.object(activate.recycle_bin, "ensure_dirs") as dirs, \
        mock.patch.object(activate.recycle_bin, "configure",
                          return_value=["radarr: set"]) as conf, \
        mock.patch("sys.stdout", new=io.StringIO()):
    activate.configure_recycle_bin(DEPLOY_FILES[".env"].replace(
        "RADARR_API_KEY=", "RADARR_API_KEY=rk"))
check("step: dirs under MEDIA_ROOT, owned by PUID:PGID",
      dirs.call_args.args, ("/srv/media", 1000, 1000))
check("step: keys from the .env", conf.call_args.args[0],
      {"RADARR_API_KEY": "rk", "SONARR_API_KEY": "set-by-hand"})


def failing(env_text):
    raise recycle_bin.RecycleBinError("PUT http://127.0.0.1:7878/...: 400")


calls = []
code, err = run_phase_with(ROUTES, deploy_files=DEPLOY_FILES, calls=calls,
                           recycle=failing)
check("hook fails: exit status", code, 1)
check("hook fails: named", "7878" in err, True)
check("hook fails: timers not armed", [c for c in calls if c[0] == "systemctl"], [])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_recycle_bin: OK")
