#!/usr/bin/env python3
"""The install hook retires what an earlier release installed, verified by its artifacts.

A root is laid out the way the previous release left the reference host —
cleanup units, their OnFailure drop-in, the timer's .wants link and the
deployed media_cleanup.py — then the real install hook runs over it. Only
the shapes this package wrote may go; an operator's mask, sibling drop-in
or symlink must survive.

Run: python3 tests/test_retired.py
"""
import pathlib
import subprocess
import sys
import tempfile

# Keep the test run from leaving a __pycache__ in tests/.
sys.dont_write_bytecode = True

from hook_fixtures import ANSWERS, context, fake_group_file  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "install.py"
DEST_REL = "opt/nivuus/media-manager"
UNIT_ABS = "/etc/systemd/system"
DROPIN_CONTENT = "[Unit]\nOnFailure=systemd-failure-notify@%n.service\n"
SERVICE = "media-manager-cleanup.service"
TIMER = "media-manager-cleanup.timer"

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def run(root):
    return subprocess.run(
        [sys.executable, str(HOOK), "--phase", "install", "--root", root],
        input=context(ANSWERS), capture_output=True, text=True, cwd=str(REPO))


def previous_release(root):
    """Lay out what the release that still shipped media_cleanup left behind."""
    units = pathlib.Path(root) / UNIT_ABS.lstrip("/")
    units.mkdir(parents=True)
    (units / SERVICE).write_text("[Service]\nExecStart=/usr/bin/true\n")
    (units / TIMER).write_text("[Timer]\nOnCalendar=*-*-* 08:00:00\n")
    wants = units / "timers.target.wants"
    wants.mkdir()
    (wants / TIMER).symlink_to(f"{UNIT_ABS}/{TIMER}")
    dropin_dir = units / f"{SERVICE}.d"
    dropin_dir.mkdir()
    (dropin_dir / "10-on-failure.conf").write_text(DROPIN_CONTENT)
    dest = pathlib.Path(root) / DEST_REL
    dest.mkdir(parents=True)
    (dest / "media_cleanup.py").write_text("# previous release\n")
    (dest / "cleanup.log").write_text("evidence\n")
    return units, dest


# --- Everything the previous release wrote is removed ---------------------
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    units, dest = previous_release(root)
    proc = run(root)
    check("retire: exit status", proc.returncode, 0)
    check("retire: service unit gone", (units / SERVICE).exists(), False)
    check("retire: timer unit gone", (units / TIMER).exists(), False)
    check("retire: .wants link gone",
          (units / "timers.target.wants" / TIMER).is_symlink(), False)
    check("retire: empty drop-in dir gone", (units / f"{SERVICE}.d").exists(), False)
    check("retire: script gone", (dest / "media_cleanup.py").exists(), False)
    # The log is the operator's evidence of what the script deleted, not a
    # file this package shipped.
    check("retire: cleanup.log kept", (dest / "cleanup.log").read_text(), "evidence\n")
    check("retire: current units still placed",
          (units / "media-manager-reset-error.timer").is_file(), True)
    check("retire: each removal announced",
          proc.stdout.count("Retired: "), 5)

    # Idempotent: a second run over the cleaned root removes nothing.
    again = run(root)
    check("retire again: exit status", again.returncode, 0)
    check("retire again: nothing announced", again.stdout.count("Retired: "), 0)

# --- An operator's shapes survive -----------------------------------------
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    units = pathlib.Path(root) / UNIT_ABS.lstrip("/")
    units.mkdir(parents=True)
    # A mask is a symlink to /dev/null: the operator's decision, not our file.
    (units / TIMER).symlink_to("/dev/null")
    dropin_dir = units / f"{SERVICE}.d"
    dropin_dir.mkdir()
    (dropin_dir / "10-on-failure.conf").write_text(DROPIN_CONTENT)
    (dropin_dir / "20-operator.conf").write_text("# kept\n")
    wants = units / "timers.target.wants"
    wants.mkdir()
    (wants / TIMER).symlink_to("/somewhere/else.timer")
    dest = pathlib.Path(root) / DEST_REL
    dest.mkdir(parents=True)
    outside = pathlib.Path(root) / "outside.py"
    outside.write_text("not ours\n")
    (dest / "media_cleanup.py").symlink_to(outside)

    proc = run(root)
    check("operator: exit status", proc.returncode, 0)
    check("operator: mask kept", (units / TIMER).is_symlink(), True)
    check("operator: our drop-in removed",
          (dropin_dir / "10-on-failure.conf").exists(), False)
    check("operator: sibling drop-in kept",
          (dropin_dir / "20-operator.conf").read_text(), "# kept\n")
    check("operator: foreign .wants link kept", (wants / TIMER).is_symlink(), True)
    check("operator: symlinked script kept",
          (dest / "media_cleanup.py").is_symlink(), True)
    check("operator: symlink target untouched", outside.read_text(), "not ours\n")

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_retired: OK")
