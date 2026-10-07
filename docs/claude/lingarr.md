# Lingarr — subtitle translation with a local LLM

Service `lingarr` (`stack/docker-compose.lingarr.yml`, included by
`docker-compose.yml`), optional: profile `lingarr`, turned on by the wizard
answer `subtitle_translation`. UI and API on `127.0.0.1:9876` only.

## How it is wired

- Translation goes through any OpenAI-compatible chat completions endpoint
  (`SERVICE_TYPE=localai`). On the reference host it is the Ollama proxy:
  `http://host.docker.internal:11435/v1/chat/completions`, model `qwen3:14b`,
  with the proxy's Bearer key.
- Every value Lingarr can read from its environment comes from the `.env`
  (`SUBTITLE_TRANSLATION_*`, `SUBTITLE_*_LANGUAGES`). Lingarr re-applies them
  at each start, so they win over the UI.
- Languages are BCP-47 codes from the wizard, comma-separated, rendered as the
  JSON list Lingarr expects (`hooks/subtitles.py`). Only the shape of a code is
  checked, never a list: Lingarr turns a code into the language name its prompt
  uses with .NET's `CultureInfo`, so any language the model writes works.
- Two settings exist only in its API, so the activate hook sets them
  (`hooks/lingarr_setup.py`): the onboarding (completed with authentication
  off), and `reasoning_effort: "none"` in the chat template. Without it,
  qwen3:14b thinks before each line: 572 completion tokens instead of 13 for
  the same translation. A template edited in the UI is left alone.
- Its media mounts use the same inner paths as Radarr/Sonarr (`/data/Movies`,
  `/data/TV Shows`), so no path mapping is needed. Downloads are not mounted.

## Security

Lingarr 1.3.0's `POST /api/auth/onboarding` is anonymous and can be called
at any time to turn authentication off. A login would therefore protect
nothing. Lingarr is published on the loopback only, like Seerr and
Maintainerr. Never publish it on another interface.

## Prerequisites outside this package

- **The endpoint must be reachable from the compose network.** On the
  reference host, firewalld's `docker` zone only lets the bridges reach listed
  services on the host. Port 11435 has to be allowed there for Lingarr to reach
  the Ollama proxy, or every translation fails with a connection error. This
  belongs to whoever runs the endpoint, not to this package.
- **The GPU is shared with the Windows VM.** The console package's libvirt hooks
  stop Ollama while the VM runs. A translation started then fails and is
  retried by Lingarr (`max_retries`).

## Turning it on for an existing install

The install hook never overwrites a key that already exists in the `.env`
(rule 1 in `hooks/install.py`), and `COMPOSE_PROFILES` already exists. Recording
`subtitle_translation=true` with `nivuus answers` therefore adds the
`SUBTITLE_*` values but **not** the profile. Add `lingarr` to `COMPOSE_PROFILES`
by hand, as for `usenet`, then run the activate phase.

Automatic translation (Lingarr's "automation") stays **off** after install.
Check the quality of one translation from the UI before turning it on.
