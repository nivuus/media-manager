#!/usr/bin/env python3
"""The activate hook starts the services that read API keys from their
environment (Recyclarr, Lingarr) only AFTER the harvest has written the keys
into the .env, and configures Lingarr only when its profile declares it.

Started in the same `up` as everything else, on a fresh install, they would
read the .env's empty keys once, at container creation, and keep them until
someone recreated them by hand: armed, running, and silently keyless.

Run: python3 tests/test_activate_waves.py
"""
import importlib.util
import pathlib
import sys
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "tests"))

import activate_fakes  # noqa: E402
from activate_fakes import CONFIG, CONFIG_XML, PS_A, VERSION, completed  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "activate_hook", REPO / "hooks" / "activate.py")
activate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activate)
run_phase_with = activate_fakes.phase_runner(activate, REPO)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


KEYED_DEPLOY = {
    ".env": "RADARR_API_KEY=\nSONARR_API_KEY=\nPROWLARR_API_KEY=\n",
    "radarr/config.xml": CONFIG_XML.format("radarr-key"),
    "sonarr/config.xml": CONFIG_XML.format("sonarr-key"),
    "prowlarr/config.xml": CONFIG_XML.format("prowlarr-key"),
}
UP_FIRST = ("docker", "compose", "up", "-d", "radarr", "sonarr")
UP_LATER = ("docker", "compose", "up", "-d", "recyclarr", "lingarr")


def routes(services, existing="", extra=None):
    base = {
        VERSION: completed(VERSION),
        CONFIG: completed(CONFIG, stdout="\n".join(services) + "\n"),
        PS_A: completed(PS_A, stdout=existing),
        **activate_fakes.timer_routes(),
    }
    base.update(extra or {})
    return base


# --- Fresh install: key consumers come up after the .env holds the keys --
env_when_later_started = {}
calls = []
real_compose_up = activate.compose_up


def spy_compose_up(services):
    # The .env as it is at the moment each wave is started.
    env_when_later_started[tuple(services)] = pathlib.Path(
        activate.DEPLOY, ".env").read_text()
    return real_compose_up(services)


with mock.patch.object(activate, "compose_up", new=spy_compose_up), \
     mock.patch.object(activate.lingarr_setup, "configure",
                       return_value=["Lingarr: configured"]) as configure:
    code, err = run_phase_with(
        routes(["radarr", "recyclarr", "sonarr", "lingarr"],
               extra={UP_FIRST: completed(UP_FIRST), UP_LATER: completed(UP_LATER)}),
        deploy_files=KEYED_DEPLOY, calls=calls)

check("fresh install: exit status", code, 0)
check("fresh install: no stderr", err, "")
ups = [call for call in calls if call[:4] == ("docker", "compose", "up", "-d")]
check("fresh install: two waves, consumers last", ups, [UP_FIRST, UP_LATER])
check("fresh install: first wave started before the harvest",
      env_when_later_started.get(("radarr", "sonarr")),
      KEYED_DEPLOY[".env"])
check("fresh install: consumers started with the keys in the .env",
      "RADARR_API_KEY=radarr-key" in env_when_later_started.get(
          ("recyclarr", "lingarr"), ""), True)
check("fresh install: Lingarr configured once", configure.call_count, 1)

# --- Lingarr not declared (profile off): never configured ---------------
calls = []
with mock.patch.object(activate.lingarr_setup, "configure") as configure:
    code, err = run_phase_with(
        routes(["radarr", "recyclarr"], existing="radarr\n",
               extra={("docker", "compose", "up", "-d", "recyclarr"):
                      completed(("docker", "compose", "up", "-d", "recyclarr"))}),
        deploy_files=KEYED_DEPLOY, calls=calls)
check("profile off: exit status", code, 0)
check("profile off: Lingarr API never called", configure.call_count, 0)

# --- Everything exists already: no `up` at all, Lingarr still configured -
calls = []
with mock.patch.object(activate.lingarr_setup, "configure", return_value=[]) as configure:
    code, err = run_phase_with(
        routes(["radarr", "lingarr"], existing="radarr\nlingarr\n"),
        deploy_files=KEYED_DEPLOY, calls=calls)
check("all exist: exit status", code, 0)
check("all exist: no up", [c for c in calls if c[:3] == ("docker", "compose", "up")], [])
check("all exist: Lingarr configuration re-checked", configure.call_count, 1)

# --- The second wave failing fails the phase, before the timers ---------
calls = []
code, err = run_phase_with(
    routes(["radarr", "recyclarr"], existing="radarr\n",
           extra={("docker", "compose", "up", "-d", "recyclarr"):
                  completed(("docker", "compose", "up", "-d", "recyclarr"),
                            returncode=1, stderr="pull access denied\n")}),
    deploy_files=KEYED_DEPLOY, calls=calls)
check("second wave fails: exit status", code, 1)
check("second wave fails: stderr kept", "pull access denied" in err, True)
check("second wave fails: timers not armed",
      [c for c in calls if c[0] == "systemctl"], [])

# --- Lingarr unreachable fails the phase, named --------------------------
with mock.patch.object(activate.lingarr_setup, "configure",
                       side_effect=activate.lingarr_setup.LingarrSetupError(
                           "GET /api/version: connection refused")):
    code, err = run_phase_with(
        routes(["lingarr"], existing="lingarr\n"), deploy_files=KEYED_DEPLOY)
check("Lingarr unreachable: exit status", code, 1)
check("Lingarr unreachable: named", "/api/version" in err, True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_activate_waves: OK")
