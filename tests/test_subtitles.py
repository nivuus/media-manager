#!/usr/bin/env python3
"""Wizard answers of the subtitle translation -> .env values
(hooks/subtitles.py), and their path through the install hook.

Languages are any BCP-47 code: the defaults are French and English, never
the only languages accepted. A malformed code and a translation turned on
without an endpoint are refused before anything is laid down.

Run: python3 tests/test_subtitles.py
"""
import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "tests"))

# Keep the test run from leaving a __pycache__ in tests/.
sys.dont_write_bytecode = True

import subtitles  # noqa: E402
from hook_fixtures import ANSWERS, context, fake_group_file  # noqa: E402

DEST_REL = "opt/nivuus/media-manager"

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def decoded(value):
    check("value is single-quoted", (value[0], value[-1]), ("'", "'"))
    return json.loads(value[1:-1])


# --- Languages: any tag, in order, without duplicates ---------------------
check("one language", decoded(subtitles.language_list("fr", "k")),
      [{"name": "fr", "code": "fr"}])
check("languages of other scripts and regions",
      [lang["code"] for lang in decoded(
          subtitles.language_list("ja, ar ,zh-Hant,pt-BR,fil", "k"))],
      ["ja", "ar", "zh-Hant", "pt-BR", "fil"])
check("duplicates dropped, order kept",
      [lang["code"] for lang in decoded(subtitles.language_list("en,fr,en", "k"))],
      ["en", "fr"])
check("minified JSON, as Lingarr reads it",
      subtitles.language_list("en", "k"), '\'[{"name":"en","code":"en"}]\'')

for bad in ("", " , ", "french", "f", "fr_FR", "fr;rm -rf", "日本語"):
    try:
        subtitles.language_list(bad, "subtitle_target_languages")
        check(f"refused: {bad!r}", "accepted", "refused")
    except ValueError as exc:
        check(f"refusal names the answer: {bad!r}",
              "subtitle_target_languages" in str(exc), True)

# --- On without an endpoint or a model: refused --------------------------
for endpoint, model in (("", "m"), ("http://x/v1/chat/completions", ""), ("", "")):
    try:
        subtitles.translation_env(True, endpoint, model, "", "en", "fr")
        check(f"on without endpoint/model {endpoint!r} {model!r}", "accepted", "refused")
    except ValueError:
        pass

# Off: rendered all the same, so turning it on later is one profile edit.
env = subtitles.translation_env(False, "", "", "", "en", "fr")
check("off: endpoint rendered empty", env["SUBTITLE_TRANSLATION_ENDPOINT"], "")
check("off: languages still rendered",
      decoded(env["SUBTITLE_TARGET_LANGUAGES"]), [{"name": "fr", "code": "fr"}])

env = subtitles.translation_env(True, "http://h/v1/chat/completions", "qwen3:14b",
                                "secret", "en", "fr,de")
check("on: every value rendered", sorted(env), sorted([
    "SUBTITLE_TRANSLATION_ENDPOINT", "SUBTITLE_TRANSLATION_MODEL",
    "SUBTITLE_TRANSLATION_API_KEY", "SUBTITLE_SOURCE_LANGUAGES",
    "SUBTITLE_TARGET_LANGUAGES"]))
check("on: model kept as given", env["SUBTITLE_TRANSLATION_MODEL"], "qwen3:14b")

# --- Through the install hook --------------------------------------------
def install(answers):
    """(exit code, stderr, .env values) of a real install.py run."""
    with tempfile.TemporaryDirectory() as root:
        fake_group_file(root)
        proc = subprocess.run(
            [sys.executable, str(REPO / "hooks" / "install.py"),
             "--phase", "install", "--root", root],
            input=context(answers), capture_output=True, text=True, cwd=str(REPO))
        env = pathlib.Path(root) / DEST_REL / ".env"
        values = {}
        if env.exists():
            for line in env.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip()
        return proc.returncode, proc.stderr, values


ON = dict(ANSWERS, subtitle_translation=True,
          subtitle_translation_endpoint="http://host.docker.internal:11435/v1/chat/completions",
          subtitle_translation_model="qwen3:14b",
          subtitle_translation_api_key="k-123",
          subtitle_target_languages="fr, es")
code, err, values = install(ON)
# Exit status only: run unprivileged (the CI), install warns on stderr that
# it cannot chown the data directories, which is not this test's concern.
check("install on: exit status", code, 0)
check("install on: lingarr profile added", values.get("COMPOSE_PROFILES"), "nvenc,lingarr")
check("install on: endpoint", values.get("SUBTITLE_TRANSLATION_ENDPOINT"),
      ON["subtitle_translation_endpoint"])
check("install on: target languages",
      [lang["code"] for lang in decoded(values.get("SUBTITLE_TARGET_LANGUAGES", "''"))],
      ["fr", "es"])
check("install on: source languages default to en",
      decoded(values.get("SUBTITLE_SOURCE_LANGUAGES", "''")), [{"name": "en", "code": "en"}])

code, err, values = install(ANSWERS)
check("install without the answers: exit status", code, 0)
check("install without the answers: no lingarr profile",
      values.get("COMPOSE_PROFILES"), "nvenc")

code, err, values = install(dict(ON, subtitle_translation_model=""))
check("install on without a model: refused", code, 1)
check("install on without a model: named", "subtitle_translation_model" in err, True)
check("install on without a model: nothing laid down", values, {})

code, err, values = install(dict(ON, subtitle_translation="true"))
check("install with a string boolean: refused", code, 1)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_subtitles: OK")
