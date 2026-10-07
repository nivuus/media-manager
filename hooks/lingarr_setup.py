#!/usr/bin/env python3
"""The part of Lingarr's configuration its environment variables cannot set.

Lingarr reads most settings from its environment (docker-compose.lingarr.yml)
but two only through its API:

1. ONBOARDING. Until it is completed, every settings call answers 403. It is
   completed with authentication off, which is what AUTH_ENABLED=false
   re-applies at every start anyway (see the compose file for why a login
   would protect nothing in Lingarr 1.3.0).

2. THE CHAT REQUEST TEMPLATE. Lingarr's default body leaves a reasoning
   model free to think before every line. Measured on qwen3:14b through
   Ollama's OpenAI API: 572 completion tokens for one subtitle line, 13 with
   `"reasoning_effort": "none"` — same translation, forty times the GPU time
   across a whole film. The template gets that one field added, and only
   while it is still Lingarr's default: a template someone edited in the UI
   is theirs, and is left alone with a message saying so.
"""
import json
import time
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:9876"
TEMPLATE_KEY = "local_ai_chat_request_template"

# What Lingarr 1.3.0 serialises as its default (LocalAiChatTemplate).
DEFAULT_TEMPLATE = {
    "model": "{model}",
    "messages": [
        {"role": "system", "content": "{systemPrompt}"},
        {"role": "user", "content": "{userMessage}"},
    ],
}
TEMPLATE = {**DEFAULT_TEMPLATE, "reasoning_effort": "none"}

READY_TIMEOUT = 300
READY_INTERVAL = 5


class LingarrSetupError(RuntimeError):
    """Lingarr did not come up, or refused a configuration call."""


def _call(method, path, body=None, opener=urllib.request.urlopen):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with opener(request, timeout=30) as response:
            text = response.read().decode()
    except (urllib.error.URLError, OSError) as exc:
        raise LingarrSetupError(f"{method} {path}: {exc}") from exc
    return json.loads(text) if text else None


def wait_ready(opener=urllib.request.urlopen, clock=time.monotonic, sleep=time.sleep):
    """Poll the anonymous version endpoint until Lingarr answers."""
    deadline = clock() + READY_TIMEOUT
    while True:
        try:
            return _call("GET", "/api/version", opener=opener)
        except LingarrSetupError:
            if clock() >= deadline:
                raise
            sleep(READY_INTERVAL)


def template_action(current):
    """'set', 'keep' (already ours) or 'custom' for a stored template value."""
    if not current:
        return "set"
    try:
        stored = json.loads(current)
    except ValueError:
        return "custom"
    if stored == TEMPLATE:
        return "keep"
    return "set" if stored == DEFAULT_TEMPLATE else "custom"


def configure(opener=urllib.request.urlopen, clock=time.monotonic, sleep=time.sleep):
    """Complete the onboarding and set the template. Returns progress messages."""
    wait_ready(opener, clock, sleep)
    messages = []
    status = _call("GET", "/api/auth/authenticated", opener=opener)
    if status.get("requiresOnboarding"):
        _call("POST", "/api/auth/onboarding", {"enableUserAuth": "false"}, opener=opener)
        messages.append("Lingarr: onboarding completed, authentication off (loopback only)")

    stored = _call("POST", "/api/setting/multiple/get", [TEMPLATE_KEY], opener=opener)
    action = template_action((stored or {}).get(TEMPLATE_KEY))
    if action == "set":
        _call("POST", "/api/setting/multiple/set",
              {TEMPLATE_KEY: json.dumps(TEMPLATE, separators=(",", ":"))}, opener=opener)
        messages.append("Lingarr: chat template set with reasoning_effort=none")
    elif action == "custom":
        messages.append("Lingarr: chat template customised in the UI, left as is")
    return messages
