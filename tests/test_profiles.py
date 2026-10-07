#!/usr/bin/env python3
"""A profile the wizard's answers turn on reaches an EXISTING .env.

The install hook never overwrites an existing .env value (rule 1), and
COMPOSE_PROFILES always exists after the first install: recording
`subtitle_translation=true` on an installed host used to render every
SUBTITLE_* value and never start Lingarr. add_profiles() adds the missing
profiles and removes none — a profile enabled by hand (usenet) stays.

Run: python3 tests/test_profiles.py
"""
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "tests"))
# Keep the test run from leaving a __pycache__ in tests/.
sys.dont_write_bytecode = True

import install  # noqa: E402
from hook_fixtures import ANSWERS, context, fake_group_file  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


add = install.add_profiles
check("added to a list", add("A=1\nCOMPOSE_PROFILES=nvenc\nB=2\n", ["nvenc", "lingarr"]),
      "A=1\nCOMPOSE_PROFILES=nvenc,lingarr\nB=2\n")
check("added to an empty value", add("COMPOSE_PROFILES=\n", ["lingarr"]),
      "COMPOSE_PROFILES=lingarr\n")
check("hand-enabled profile kept", add("COMPOSE_PROFILES=usenet\n", ["nvenc"]),
      "COMPOSE_PROFILES=usenet,nvenc\n")
check("nothing wanted: untouched", add("COMPOSE_PROFILES=usenet\n", []),
      "COMPOSE_PROFILES=usenet\n")
check("already there: same text", add("COMPOSE_PROFILES=nvenc,lingarr", ["lingarr"]),
      "COMPOSE_PROFILES=nvenc,lingarr")
check("quotes kept", add('COMPOSE_PROFILES="nvenc"\n', ["lingarr"]),
      'COMPOSE_PROFILES="nvenc,lingarr"\n')
check("last assignment is the one rewritten",
      add("COMPOSE_PROFILES=a\n# COMPOSE_PROFILES=x\nCOMPOSE_PROFILES=b\n", ["c"]),
      "COMPOSE_PROFILES=a\n# COMPOSE_PROFILES=x\nCOMPOSE_PROFILES=b,c\n")
check("no assignment: untouched", add("A=1\n", ["c"]), "A=1\n")

# --- Through the install hook, on an installed host ----------------------
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    dest = pathlib.Path(root) / "opt/nivuus/media-manager"
    dest.mkdir(parents=True)
    env = dest / ".env"
    env.write_text("RADARR_API_KEY=keep-me\nCOMPOSE_PROFILES=usenet\n")
    answers = dict(ANSWERS, subtitle_translation=True,
                   subtitle_translation_endpoint="http://h/v1/chat/completions",
                   subtitle_translation_model="m")
    proc = subprocess.run(
        [sys.executable, str(REPO / "hooks" / "install.py"),
         "--phase", "install", "--root", root],
        input=context(answers), capture_output=True, text=True, cwd=str(REPO))
    check("install: exit status", proc.returncode, 0)
    text = env.read_text()
    check("install: profiles added after the hand-enabled one",
          [line for line in text.splitlines() if line.startswith("COMPOSE_PROFILES=")],
          ["COMPOSE_PROFILES=usenet,nvenc,lingarr"])
    check("install: other values untouched", "RADARR_API_KEY=keep-me" in text, True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_profiles: OK")
