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


# --- Turned on later, from a .env the hook itself rendered ----------------
# The case above starts from a .env that predates the SUBTITLE_* keys. The
# real starting state is a .env this hook rendered with translation off:
# since 1.4.0 the keys are always there, empty. The endpoint and the model
# must then be filled from the answers, or Lingarr starts and fails every job.
def run_install(root, answers):
    return subprocess.run(
        [sys.executable, str(REPO / "hooks" / "install.py"),
         "--phase", "install", "--root", root],
        input=context(answers), capture_output=True, text=True, cwd=str(REPO))


def env_values(text):
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


ON = dict(ANSWERS, subtitle_translation=True,
          subtitle_translation_endpoint="http://h/v1/chat/completions",
          subtitle_translation_model="m", subtitle_translation_api_key="k")
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    env = pathlib.Path(root) / "opt/nivuus/media-manager/.env"
    first = run_install(root, ANSWERS)
    check("off then on: first install", (first.returncode, first.stderr), (0, ""))
    before = env_values(env.read_text())
    check("off then on: keys rendered empty",
          [before.get(k) for k in ("SUBTITLE_TRANSLATION_ENDPOINT", "SUBTITLE_TRANSLATION_MODEL")],
          ["", ""])
    second = run_install(root, ON)
    check("off then on: second install", (second.returncode, second.stderr), (0, ""))
    after = env_values(env.read_text())
    check("off then on: endpoint, model and key filled",
          [after.get(k) for k in ("SUBTITLE_TRANSLATION_ENDPOINT", "SUBTITLE_TRANSLATION_MODEL",
                                  "SUBTITLE_TRANSLATION_API_KEY")],
          ["http://h/v1/chat/completions", "m", "k"])
    check("off then on: profile added", "lingarr" in after.get("COMPOSE_PROFILES", ""), True)

    # A value already set is the operator's: other answers never replace it.
    third = run_install(root, dict(ON, subtitle_translation_endpoint="http://other/v1",
                                   subtitle_translation_model="other"))
    check("set values: third install", (third.returncode, third.stderr), (0, ""))
    kept = env_values(env.read_text())
    check("set values: never replaced",
          [kept.get(k) for k in ("SUBTITLE_TRANSLATION_ENDPOINT", "SUBTITLE_TRANSLATION_MODEL")],
          ["http://h/v1/chat/completions", "m"])

check("fill: last assignment, quotes and other lines kept",
      install.fill_empty_values('A=1\nE=x\nE=""\nM=\n', {"E": "e", "M": "m", "N": "n"}),
      'A=1\nE=x\nE="e"\nM=m\n')
check("fill: a non-empty value is kept",
      install.fill_empty_values("E=set\n", {"E": "e"}), "E=set\n")
check("fill: an empty answer fills nothing",
      install.fill_empty_values("E=\n", {"E": ""}), "E=\n")

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_profiles: OK")
