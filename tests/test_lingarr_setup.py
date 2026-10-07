#!/usr/bin/env python3
"""Lingarr's API-only configuration (hooks/lingarr_setup.py), against a fake
HTTP opener: onboarding only when Lingarr asks for it, the chat template set
only while it is still Lingarr's default, and an unreachable Lingarr reported
by name after the readiness deadline instead of hanging or passing.

Run: python3 tests/test_lingarr_setup.py
"""
import io
import json
import pathlib
import sys
import urllib.error

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))

import lingarr_setup  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


class FakeLingarr:
    """Answers (method, path) from a table and records every request body."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, request, timeout=None):
        path = request.full_url.removeprefix(lingarr_setup.BASE_URL)
        key = (request.get_method(), path)
        body = json.loads(request.data) if request.data else None
        self.calls.append((key, body))
        if key not in self.answers:
            raise AssertionError(f"unexpected Lingarr call: {key}")
        answer = self.answers[key]
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode() if answer is not None else b"")


DEFAULT = json.dumps(lingarr_setup.DEFAULT_TEMPLATE, separators=(",", ":"))
OURS = json.dumps(lingarr_setup.TEMPLATE, separators=(",", ":"))
SET = ("POST", "/api/setting/multiple/set")


def lingarr(onboarding, template):
    return FakeLingarr({
        ("GET", "/api/version"): {"currentVersion": "1.3.0"},
        ("GET", "/api/auth/authenticated"): {"requiresOnboarding": onboarding},
        ("POST", "/api/auth/onboarding"): None,
        ("POST", "/api/setting/multiple/get"):
            {lingarr_setup.TEMPLATE_KEY: template},
        SET: None,
    })


# --- What each stored template value leads to ----------------------------
check("empty template is set", lingarr_setup.template_action(""), "set")
check("missing template is set", lingarr_setup.template_action(None), "set")
check("Lingarr's default is set", lingarr_setup.template_action(DEFAULT), "set")
check("ours is kept", lingarr_setup.template_action(OURS), "keep")
check("an edited one is the operator's",
      lingarr_setup.template_action(json.dumps(
          {**lingarr_setup.DEFAULT_TEMPLATE, "temperature": 0.2})), "custom")
check("unparsable is the operator's", lingarr_setup.template_action("{oops"), "custom")
check("the template only adds reasoning_effort to the default",
      {k: v for k, v in lingarr_setup.TEMPLATE.items() if k != "reasoning_effort"},
      lingarr_setup.DEFAULT_TEMPLATE)

# --- Fresh Lingarr: onboarding, then the template -------------------------
fake = lingarr(onboarding=True, template=DEFAULT)
messages = lingarr_setup.configure(opener=fake, sleep=lambda s: None)
check("fresh: onboarding with auth off",
      [body for (key, body) in fake.calls if key == ("POST", "/api/auth/onboarding")],
      [{"enableUserAuth": "false"}])
check("fresh: template written",
      [body for (key, body) in fake.calls if key == SET],
      [{lingarr_setup.TEMPLATE_KEY: OURS}])
check("fresh: two messages", len(messages), 2)

# --- Already configured: no write at all ----------------------------------
fake = lingarr(onboarding=False, template=OURS)
messages = lingarr_setup.configure(opener=fake, sleep=lambda s: None)
check("configured: no onboarding, no write",
      [key for (key, _) in fake.calls if key[0] == "POST" and key != (
          "POST", "/api/setting/multiple/get")], [])
check("configured: silent", messages, [])

# --- Edited in the UI: left alone, and said ------------------------------
fake = lingarr(onboarding=False, template='{"model":"{model}"}')
messages = lingarr_setup.configure(opener=fake, sleep=lambda s: None)
check("custom: no write", [key for (key, _) in fake.calls if key == SET], [])
check("custom: said", any("customised" in m for m in messages), True)

# --- Never answers: fails by name once the deadline has passed ------------
now = [0.0]
fake = FakeLingarr({("GET", "/api/version"): urllib.error.URLError("refused")})
try:
    lingarr_setup.configure(opener=fake, clock=lambda: now[0],
                            sleep=lambda s: now.__setitem__(0, now[0] + s))
    check("unreachable: raises", "returned", "raised")
except lingarr_setup.LingarrSetupError as exc:
    check("unreachable: names the endpoint", "/api/version" in str(exc), True)
check("unreachable: polled until the deadline, not once",
      len(fake.calls), lingarr_setup.READY_TIMEOUT // lingarr_setup.READY_INTERVAL + 1)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_lingarr_setup: OK")
