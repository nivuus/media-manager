#!/usr/bin/env python3
"""reset-error's run, checked from its entry point.

Run: python3 tests/test_reset_error_run.py
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
STACK = REPO / "stack"

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# --- The entry point runs from its deployment directory -------------------
# systemd runs `python3 /opt/nivuus/media-manager/reset-error.py`: Python puts
# that directory first on sys.path, and nothing else. A copy of the deployed
# tree with an empty .env (python-dotenv's upward search stops there) and an
# environment without API keys exercises the imports, the logging and the key
# check without a single network call.
with tempfile.TemporaryDirectory() as tmp:
    deploy = pathlib.Path(tmp) / "media-manager"
    shutil.copytree(STACK, deploy,
                    ignore=shutil.ignore_patterns("__pycache__", ".env"))
    (deploy / ".env").write_text("")
    proc = subprocess.run(
        [sys.executable, str(deploy / "reset-error.py")],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=str(deploy), capture_output=True, text=True, timeout=60)
    output = proc.stdout + proc.stderr
    check("missing keys: exit status", proc.returncode, 1)
    check("missing keys: RADARR_API_KEY named", "RADARR_API_KEY" in output, True)
    check("missing keys: SONARR_API_KEY named", "SONARR_API_KEY" in output, True)
    check("missing keys: no traceback", "Traceback" in output, False)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_reset_error_run: OK")
