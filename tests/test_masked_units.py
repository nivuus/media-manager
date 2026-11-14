#!/usr/bin/env python3
"""A unit the operator masked stays masked: install and activate leave it alone.

A mask in /etc/systemd/system is a symlink (to /dev/null, in practice).
Writing a unit by path follows it: the unit's content would land in the
link's target and its mode would become 0644. Here the link points at a
scratch file of mode 0600, never at the real /dev/null, and both hooks run
against a temporary --root only: install as a subprocess, activate through
run_phase() with a fake subprocess.run (tests/activate_fakes.py), so no
docker or systemctl command is ever executed.

Run: python3 tests/test_masked_units.py
"""
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

# Keep the test run from leaving a __pycache__ in tests/.
sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "tests"))

import activate_fakes  # noqa: E402
from hook_fixtures import ANSWERS, context, fake_group_file  # noqa: E402

HOOK = REPO / "hooks" / "install.py"
UNIT_REL = "etc/systemd/system"
MASKED_TIMER = "media-manager-reset-error.timer"
OTHER_TIMER = "media-manager-update-wanted.timer"
SERVICE = "media-manager-reset-error.service"
DROPIN_REL = f"{SERVICE}.d/10-on-failure.conf"
SENTINEL = b"operator's file, not a unit\n"

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def sentinel(path):
    """A scratch file of mode 0600 standing in for the mask's target."""
    path.write_bytes(SENTINEL)
    os.chmod(path, 0o600)
    return path


def state(path):
    st = path.stat()
    return path.read_bytes(), stat.S_IMODE(st.st_mode)


def messages(stdout):
    return [json.loads(line).get("msg", "") for line in stdout.splitlines() if line.strip()]


# --- install: a masked unit and a symlinked drop-in are left alone ---------
with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
    fake_group_file(root)
    units = pathlib.Path(root) / UNIT_REL
    units.mkdir(parents=True)
    # The host has the notifier, so the drop-ins are written.
    (units / "systemd-failure-notify@.service").write_text("[Service]\n")
    unit_target = sentinel(pathlib.Path(outside) / "unit-target")
    (units / MASKED_TIMER).symlink_to(unit_target)
    dropin_target = sentinel(pathlib.Path(outside) / "dropin-target")
    (units / DROPIN_REL).parent.mkdir()
    (units / DROPIN_REL).symlink_to(dropin_target)

    proc = subprocess.run(
        [sys.executable, str(HOOK), "--phase", "install", "--root", root],
        input=context(ANSWERS), capture_output=True, text=True, cwd=str(REPO))
    check(f"install: exit status (stderr: {proc.stderr.strip()!r})", proc.returncode, 0)
    check("install: masked unit target unchanged", state(unit_target), (SENTINEL, 0o600))
    check("install: mask still a symlink", (units / MASKED_TIMER).is_symlink(), True)
    check("install: symlinked drop-in target unchanged",
          state(dropin_target), (SENTINEL, 0o600))
    check("install: symlinked drop-in still a symlink", (units / DROPIN_REL).is_symlink(), True)
    msgs = messages(proc.stdout) if proc.returncode == 0 else []
    check("install: the masked unit is reported",
          any(MASKED_TIMER in m and "masked" in m for m in msgs), True)
    check("install: the symlinked drop-in is reported",
          any(DROPIN_REL in m and "symlink" in m for m in msgs), True)

    # The units that are not masked are still placed, 0644, as regular files.
    other = units / OTHER_TIMER
    check("install: other unit placed",
          other.is_file() and not other.is_symlink()
          and other.read_bytes() == (REPO / "systemd" / OTHER_TIMER).read_bytes(), True)
    check("install: other unit mode", stat.S_IMODE(other.stat().st_mode), 0o644)
    other_dropin = units / "media-manager-update-wanted.service.d/10-on-failure.conf"
    check("install: other drop-in written", other_dropin.read_text(),
          "[Unit]\nOnFailure=systemd-failure-notify@%n.service\n")
    check("install: other drop-in mode", stat.S_IMODE(other_dropin.stat().st_mode), 0o644)
    check("install: no temporary file left",
          sorted(p.name for p in units.iterdir() if ".tmp-" in p.name), [])

# --- activate: a masked timer is neither armed nor started -----------------
spec = importlib.util.spec_from_file_location("activate_hook", REPO / "hooks" / "activate.py")
activate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activate)

with tempfile.TemporaryDirectory() as outside:
    unit_target = sentinel(pathlib.Path(outside) / "unit-target")
    routes = {
        activate_fakes.VERSION: activate_fakes.completed(activate_fakes.VERSION),
        activate_fakes.CONFIG: activate_fakes.completed(activate_fakes.CONFIG, stdout="radarr\n"),
        activate_fakes.PS_A: activate_fakes.completed(activate_fakes.PS_A, stdout="radarr\n"),
        **activate_fakes.timer_routes(),
    }
    calls = []
    seen = {}

    def mask_then_inspect(deploy, root):
        units = root / UNIT_REL
        seen["wants"] = sorted(p.name for p in (units / "timers.target.wants").iterdir())
        seen["target"] = state(unit_target)

    run_phase = activate_fakes.phase_runner(activate, REPO)
    # phase_runner lays the timers as regular files; mask one before the phase.
    real_run_phase = activate.run_phase

    def masked_run_phase(root):
        masked = pathlib.Path(root) / UNIT_REL / MASKED_TIMER
        masked.unlink()
        masked.symlink_to(unit_target)
        return real_run_phase(root)

    emitted = []
    real_emit = activate.emit
    activate.run_phase = masked_run_phase
    activate.emit = emitted.append
    try:
        keys = {rel: activate_fakes.CONFIG_XML.format("k" * 32) for rel in activate.HARVEST.values()}
        code, stderr = run_phase(routes, deploy_files={**keys, ".env": "RADARR_API_KEY=\n"},
                                 inspect=mask_then_inspect, calls=calls)
    finally:
        activate.run_phase = real_run_phase
        activate.emit = real_emit
    check("activate: exit status", (code, stderr), (0, ""))
    check("activate: only the unmasked timer armed", seen.get("wants"), [OTHER_TIMER])
    check("activate: masked timer never started",
          ("systemctl", "start", MASKED_TIMER) in calls, False)
    check("activate: unmasked timer started", ("systemctl", "start", OTHER_TIMER) in calls, True)
    check("activate: mask target unchanged", seen.get("target"), (SENTINEL, 0o600))
    check("activate: the masked timer is reported",
          any(MASKED_TIMER in e.get("msg", "") and "masked" in e.get("msg", "")
              for e in emitted), True)

# A unit that is missing altogether is still an error, but a named one.
real_run_phase = activate.run_phase


def missing_run_phase(root):
    (pathlib.Path(root) / UNIT_REL / MASKED_TIMER).unlink()
    return real_run_phase(root)


activate.run_phase = missing_run_phase
try:
    keys = {rel: activate_fakes.CONFIG_XML.format("k" * 32) for rel in activate.HARVEST.values()}
    code, stderr = activate_fakes.phase_runner(activate, REPO)(
        routes, deploy_files={**keys, ".env": "RADARR_API_KEY=\n"})
finally:
    activate.run_phase = real_run_phase
check("activate, missing unit: exit status and message",
      (code, "unit to arm is missing" in stderr), (1, True))

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_masked_units: OK")
