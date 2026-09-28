#!/usr/bin/env python3
"""The maintenance scripts' logging: the journal always, a rotating file under systemd.

stdout is what the journal keeps. When the unit declares LogsDirectory=,
systemd creates the directory and passes it as LOGS_DIRECTORY; the scripts
then also write a file there that outlives the journal's retention, rotated
so that it cannot fill the disk. A crash lands there too, traceback
included: reset-error sets the log up before anything else can fail.

Run: python3 tests/test_run_log.py
"""
import io
import logging
import os
import pathlib
import sys
import tempfile
from unittest import mock

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stack"))

from maintenance import reset_error, run_log  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def set_up(environ):
    """run_log.setup() with stdout captured; returns what reached stdout."""
    stdout = io.StringIO()
    with mock.patch("sys.stdout", new=stdout):
        run_log.setup("reset-error.log", environ)
    return stdout


def tear_down():
    """Detach and close what setup() attached, so the next case starts clean."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


log = logging.getLogger("maintenance.reset_error")

# --- Without LOGS_DIRECTORY: the journal only ------------------------------
# Run from an empty directory: a file written to the current directory by
# default would show up there.
with tempfile.TemporaryDirectory() as tmp:
    previous = os.getcwd()
    os.chdir(tmp)
    try:
        stdout = set_up({})
        log.info("queue read")
        tear_down()
    finally:
        os.chdir(previous)
    check("no directory: line on stdout", "queue read" in stdout.getvalue(), True)
    check("no directory: no file written", list(pathlib.Path(tmp).iterdir()), [])

# --- With LOGS_DIRECTORY: the journal and the file -------------------------
with tempfile.TemporaryDirectory() as tmp:
    stdout = set_up({"LOGS_DIRECTORY": tmp})
    log.error("queue unreadable")
    tear_down()
    log_file = pathlib.Path(tmp) / "reset-error.log"
    check("directory: line on stdout", "queue unreadable" in stdout.getvalue(), True)
    text = log_file.read_text() if log_file.is_file() else ""
    check("directory: line in the file", "ERROR queue unreadable" in text, True)
    # The journal dates every line itself; the file has to.
    check("directory: file lines are dated", text[:2] == "20", True)

# systemd joins several LogsDirectory= entries with ':'; the first one is used.
with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
    set_up({"LOGS_DIRECTORY": f"{first}:{second}"})
    log.info("queue read")
    tear_down()
    check("several directories: file in the first",
          (pathlib.Path(first) / "reset-error.log").is_file(), True)

# --- Rotation: the file cannot grow without bound --------------------------
MIB = 1024 * 1024
with tempfile.TemporaryDirectory() as tmp:
    set_up({"LOGS_DIRECTORY": tmp})
    chunk = "x" * (256 * 1024)
    for _ in range(4 * 40):  # 40 MiB: eight times the size of one file
        log.info(chunk)
    tear_down()
    directory = pathlib.Path(tmp)
    check("rotation: current file stays under 5 MiB",
          (directory / "reset-error.log").stat().st_size <= 5 * MIB, True)
    check("rotation: five backups kept",
          sorted(p.name for p in directory.iterdir()),
          ["reset-error.log"] + [f"reset-error.log.{n}" for n in range(1, 6)])

# --- A crash reaches the file, traceback included -------------------------------
def crash(target, run):
    """reset_error.main() with `target` raising; returns (exit status, file text).

    `run` stands in for reset_error.run unless `target` is run itself.
    """
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.dict(os.environ, {"LOGS_DIRECTORY": tmp}), \
                mock.patch("sys.stdout", new=io.StringIO()), \
                mock.patch("maintenance.reset_error.load_environment"), \
                mock.patch("maintenance.reset_error.run", new=run), \
                mock.patch(target, side_effect=RuntimeError("queue exploded")):
            try:
                code = reset_error.main()
            except Exception as error:  # a crash fails this case, not the whole file
                code = f"raised {error!r}"
        tear_down()
        log_file = pathlib.Path(tmp) / "reset-error.log"
        return code, log_file.read_text() if log_file.is_file() else ""


for label, target in [("run", "maintenance.reset_error.run"),
                      ("environment", "maintenance.reset_error.load_environment")]:
    run = mock.Mock()
    code, text = crash(target, run)
    check(f"crash in {label}: exit status", code, 1)
    check(f"crash in {label}: traceback in the file",
          "Traceback (most recent call last)" in text, True)
    check(f"crash in {label}: error in the file",
          "RuntimeError: queue exploded" in text, True)
    if label == "environment":
        # Nothing goes on after an unexpected exception.
        check("crash in environment: no run after it", run.called, False)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_run_log: OK")
