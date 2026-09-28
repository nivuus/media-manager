# Audit remediation — maintenance scripts, package hooks, docs (2026-09-28)

**Source of requirements.** The production audit of 2026-09-28 (no separate
spec document). Its findings are quoted where a task depends on them.

**Goal.** Make the two maintenance scripts that still run daily safe when a
data source does not answer, bring the production-only fixes into the
repository, repair the package defects the audit found, and make the docs
describe what actually runs.

**Out of scope.** `stack/media_cleanup.py` is NOT modified: its timer is
disabled on the host and the package must stop arming it (Task 4); its
replacement (Maintainerr pilot) or rewrite is a later decision. The external
updater (`packages/mqtt`) and the console libvirt hooks are fixed in their
own repositories.

## Global Constraints

- Everything written in code is English: identifiers, comments, docstrings,
  log lines, user-facing strings, commit messages. Do not mass-translate
  lines you do not otherwise touch.
- No source file over 500 lines, new or modified. Split along real seams.
- `stack/` is deployed byte for byte to `/opt/nivuus/media-manager` by
  `hooks/install.py`. The systemd units run
  `python3 /opt/nivuus/media-manager/<script>.py`, so each script must keep
  working when run as `python3 <deploy_dir>/<script>.py` (Python puts the
  script's directory first on `sys.path`). Runtime dependencies are only the
  standard library, `requests` and `python-dotenv` (Debian packages
  `python3-requests`, `python3-dotenv`). No new third-party dependency.
- Tests are standalone Python scripts under `tests/`, run by `make test`
  (each new test file must be added to the Makefile loop). Follow the style
  of the existing tests. No pytest. Tests never touch the network, `/opt`,
  `/etc`, docker or systemd: mock `requests`, inject fakes.
- Never touch production: no writes to `/opt`, `/etc`, no docker, no
  systemctl, no calls to live services. Work only inside this worktree.
- Commits: conventional subjects (`fix(scripts): ...`, `refactor(scripts):
  ...`, `feat(package): ...`, `test: ...`, `docs: ...`), a body that says
  why, and the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  Commit with
  `git -c user.name="Maxime Allanic" -c user.email="maxime@allanic.me" commit`.
  Never push.
- Behaviour contract of `reset-error.py` (from CLAUDE.md, must hold after
  every task): a completed download that failed to import is imported
  (manual-import API), never deleted — except when its files are gone
  (remove the row, no blocklist) or when it has sat in the queue more than
  72 hours (remove + blocklist). The Downloads purge deletes only files
  older than 24 hours that no queue references, and only in a run where
  EVERY queue was read successfully.
- Exit status: a run that hit any API or I/O failure exits 1 after doing
  what it safely could; a run without failures exits 0. This is what makes
  systemd mark the unit failed so `OnFailure=` can alert.
- Durable logs: each script logs to stdout (the journal) and, when the
  environment variable `LOGS_DIRECTORY` is set (systemd sets it from
  `LogsDirectory=media-manager`), to a rotating file in that directory:
  `reset-error.log` and `update-wanted.log`, using
  `logging.handlers.RotatingFileHandler` with `maxBytes=5 * 1024 * 1024`
  and `backupCount=5`.

## Task 1: Import the production versions of the two scripts verbatim

Done by the controller before this plan was written: commit `de51009`
copies `/opt/nivuus/media-manager/reset-error.py` and `update_wanted.py`
byte for byte into `stack/`. Nothing to implement.

## Task 2: Split reset-error.py into modules and make it fail closed

Start from the imported version (commit `de51009`). It is 565 lines and
mixes configuration, HTTP, pure policy, queue actions, the Downloads purge
and the dead-metadata cleanup.

Create the package `stack/maintenance/` (with `__init__.py`) holding one
responsibility per module, each under 500 lines:

- `maintenance/arr_api.py` — environment loading (python-dotenv, same `.env`
  lookup as today), the Radarr/Sonarr instance lists built from the same
  environment variables as today, the API-key check, a small HTTP helper
  that always passes an explicit timeout (`timeout=(10, 60)`), and a
  `Failures` recorder (e.g. `record(context, error)`, `count`) that every
  step uses to report a failure instead of swallowing it. `update_wanted.py`
  reuses this module in Task 3, so keep its interface script-agnostic.
- `maintenance/run_log.py` — logging setup as described in Global
  Constraints (stdout + rotating file when `LOGS_DIRECTORY` is set).
- `maintenance/queue_policy.py` — pure classification: keyword tables, age
  computation, `classify_item(item, now)` returning the action. No I/O.
- `maintenance/queue_actions.py` — manual import and queue-row removal.
- `maintenance/downloads_purge.py` — the 24-hour purge of the Downloads
  directory, protected names, recreation of the category subdirectories.
- `maintenance/dead_metadata.py` — removal of file-less entries whose
  TMDb/TVDB metadata is gone.
- `stack/reset-error.py` stays the entry point the systemd unit runs: set up
  logging, run the steps, `sys.exit(1)` if any failure was recorded, else 0.

Behaviour changes (each one covered by a test):

1. **Fail closed.** If the queue of any instance cannot be read — network
   error, non-2xx status, JSON that does not parse, `records` not a list —
   the Downloads purge is skipped for the whole run and the failure is
   recorded. Queues that were read are still processed. (Audit C2: with
   Radarr unreachable for 7 days, the purge ran with an empty protection
   set and deleted a completed, unimported 36.8 GB download.)
2. **Every failure counts.** Queue fetch, manual import, queue-row removal
   and dead-metadata calls all record failures; the run exits 1 if any.
3. **Import-pending rows first.** Rows whose `trackedDownloadState` is
   `importPending` or `importBlocked` are classified before the generic
   keyword rules, so a keyword (e.g. `'sample'`, which also matches the
   legitimate rejection "Unable to determine if file is a sample") can no
   longer pre-empt the manual-import attempt.
4. **Files gone.** An import-pending row whose status messages say the files
   are not there ("No files found are eligible for import", "path does not
   exist") is removed WITHOUT blocklisting: DELETE the queue row with
   `removeFromClient=true`, `blocklist=false`, `skipRedownload=true`. The
   release was fine; its files vanished.
5. **Real failure states.** The rule that today tests `error` /
   `importFailed` (values the Radarr/Sonarr APIs never return) must test
   the real ones: `status == 'failed'` or `trackedDownloadState == 'failed'`
   → remove + blocklist. Upstream enums, camelCase in the API — status:
   unknown, queued, paused, downloading, completed, failed, warning, delay,
   downloadClientUnavailable, fallback; trackedDownloadState: downloading,
   importBlocked, importPending, importing, imported, failedPending, failed,
   ignored.
6. **Client unavailable is not a bad release.** The 72-hour rule never
   removes a row whose `status` is `downloadClientUnavailable`.
7. **Naive dates.** An `added` value without a timezone is treated as UTC
   instead of raising `TypeError`.
8. Keep the production rule for client-error statuses (`warning`/`failed`
   with an `errorMessage`, 6-hour grace) and every other existing rule
   unchanged.
9. Log lines in English.

Systemd: add `LogsDirectory=media-manager` to
`systemd/media-manager-reset-error.service`, and assert it in
`tests/test_maintenance_units.py`.

Tests (new files, added to the Makefile): `tests/test_queue_policy.py`
(classification table covering every rule above, including the unchanged
ones) and `tests/test_reset_error_run.py` (a failing queue fetch → the purge
function is never called and the exit status is 1; all queues readable and
no failure → exit 0; a failing queue-row DELETE → exit 1). Use TDD: write
each test, watch it fail, then implement.

## Task 3: update_wanted.py on the shared modules, deterministic backlog

Start from the imported version (commit `de51009`) and the modules of
Task 2.

- Use `maintenance/arr_api.py` (instances, API-key check, HTTP with
  timeouts, `Failures`) and `maintenance/run_log.py`
  (`update-wanted.log`).
- Selection per instance, capped at `MAX_SEARCH_PER_INSTANCE = 100`:
  first the items released/aired within `RECENT_WINDOW_DAYS = 30`, newest
  first; then the backlog ordered by `lastSearchTime` ascending, items with
  no `lastSearchTime` first. This replaces `random.sample`: stateless and
  deterministic, and every missing item gets its turn (audit: random draws
  needed a median of 96 days to cover 1226 episodes once, against 14 for an
  ordered walk). Both Radarr movies and Sonarr episodes returned by the
  wanted/missing endpoints carry `lastSearchTime`.
- Keep: the unreleased/unaired skip, `release_date` parsing,
  `DELAY_BETWEEN_INSTANCES = 120`, the instance order, pagination.
- Exit 1 if any instance failed (fetch or search), after trying all of them.
- Log lines in English. Fix the docstring that claims "newest first" for
  the fetched list: Radarr returns it sorted by title; the ordering must come
  from the selection code, not from the API.
- Systemd: `LogsDirectory=media-manager` in
  `systemd/media-manager-update-wanted.service`, asserted in
  `tests/test_maintenance_units.py`.
- Tests: `tests/test_update_wanted.py` (recent first and newest first;
  backlog by `lastSearchTime` with missing values first; cap of 100 across
  both; exit 1 when a fetch fails; no network). TDD.

## Task 4: Package hooks — failure alerts, timers, errors, safe copies

Files: `hooks/install.py`, `hooks/activate.py`, their tests, the units.

1. **Failure alerts without a hard dependency.** The deployed units on the
   reference host carry `OnFailure=systemd-failure-notify@%n.service`, added
   by hand; a reinstall drops it. `systemd-failure-notify@.service` belongs
   to the host, not to this package. The install hook writes, for each of
   the three `media-manager-*.service` units, the drop-in
   `<root>/etc/systemd/system/<unit>.d/10-on-failure.conf` containing
   `[Unit]` / `OnFailure=systemd-failure-notify@%n.service` — only when
   `<root>/etc/systemd/system/systemd-failure-notify@.service` exists. When
   it does not exist, it removes that exact drop-in file if a previous
   install wrote it, and nothing else. Tests for both cases.
2. **Stop arming the cleanup timer.** `media_cleanup.py` over-deletes and
   ranks re-requested titles first (audit H2); its timer is disabled on the
   host pending a replacement. activate must no longer enable
   `media-manager-cleanup.timer`. Keep shipping its unit files so an
   operator can still run it by hand or opt in deliberately. The two other
   timers keep today's behaviour.
3. **Compose errors are errors.** `compose_services()` in activate returns
   `[]` on any non-zero exit, so a failing `docker compose config` reads as
   "nothing to create" (exit 0, activation stamped) and a failing
   `docker compose ps` reads as "no container exists" (global `up -d`,
   which starts the Tdarr nodes the VM hooks stopped). It must raise; the
   phase then exits non-zero so the engine retries it. A failing `up` must
   also make the phase exit non-zero. Tests with a fake compose runner for
   each path.
4. **Docker is a prerequisite, checked.** Before any compose call, activate
   checks `docker compose version`; on failure it exits non-zero with an
   English message saying Docker Engine with the compose v2 plugin is
   required (e.g. enable the installer's docker feature). The apt list is
   fixed in Task 5.
5. **Copy only what git tracks.** When the package directory is a git
   checkout (it contains `.git`), install copies only the files
   `git ls-files -z stack/` lists; otherwise (a `git archive` export) it
   copies the tree as today. It never copies a `.env`. Test: a checkout
   with an untracked `stack/.env` and `stack/radarr/config.xml` deploys
   neither.
6. **Atomic `.env`.** Every `.env` write (install and activate) goes to a
   temporary file created with mode 0600 in the same directory, is fsynced,
   then `os.replace`d; when the new content equals the current content,
   nothing is written. Tests: mode 0600, unchanged content leaves the file
   untouched (same inode and mtime).
7. Strings you add or change are English.

## Task 5: Manifest apt list, drop the Whisper service, CI runs the tests

1. `nivuus-package.yaml`: the apt list becomes `python3-requests` and
   `python3-dotenv` only. `docker-compose-v2` does not exist on Debian, and
   `docker.io` conflicts with docker-ce (installing it would remove Docker CE
   on the reference host). Rewrite the comment above the list accordingly
   (Docker is a prerequisite checked by activate, Task 4). Update
   `tests/test_manifest_contract.py`.
2. Remove the `openai-whisper-asr-webservice` service (image
   `mccloud/subgen`) from `stack/docker-compose.yml`, its entry from
   `stack/docker-compose.qsv.yml`, and from bazarr's `depends_on`. Bazarr's
   Whisper provider could never produce a useful subtitle for this setup
   (it only translates to English, and its only possible output with the
   French profile is a full French transcription filed as forced) and was
   disabled on the host on 2026-09-28. Update `tests/test_compose_portable.py`
   (the overlay now covers tdarr, tdarr-node and plex) and any template or
   test that references the service.
3. `.github/workflows/ci.yml`: add a job that runs `make test` on
   `ubuntu-latest` with Python 3 and the modules the tests import (check
   them: at least PyYAML, requests, python-dotenv). Keep the three shared
   jobs unchanged.

## Task 6: Documentation matches what runs

`CLAUDE.md` and `README.md`. Keep the language each section is written in.

- The container updater is `mqtt-system-agent` (repository
  `packages/mqtt`, `src/features/updates/`), not Watchtower; the
  `com.centurylinklabs.watchtower.enable` label is how it selects
  containers. It replays each container's compose files, recreates one
  service at a time with `--no-deps`, never starts a stopped container.
- Compose is run from `/opt/nivuus/media-manager` without `-f`: the `.env`
  sets `COMPOSE_FILE` to add `docker-compose.qsv.yml` (Intel QSV,
  `/dev/dri`). An explicit `-f docker-compose.yml` silently drops it — that
  is how Plex lost hardware transcoding from 2026-09-04 to 2026-09-28.
  Never a global `docker compose up -d` or `restart` while the Windows VM
  runs (it starts the Tdarr nodes the libvirt hooks stopped): target
  services with `--no-deps`, and recreate `tdarr-node` during a game with
  `docker compose up --no-start --no-deps tdarr-node`. How to verify:
  `docker inspect -f '{{json .HostConfig.Devices}}' <container>`.
- Hardware acceleration: Plex, Tdarr and Tdarr-node use QSV; the Whisper
  service is gone (Task 5) and so is its line in the data flow; Bazarr has
  one instance; subtitles come from Bazarr's providers.
- `reset-error.py`: fail-closed purge, exit status and `OnFailure=` drop-in,
  logs in `/var/log/media-manager/reset-error.log`, files-gone rows removed
  without blocklist.
- `update_wanted.py`: recent first, then backlog by `lastSearchTime`; no
  state file (remove every `.update_wanted_state` mention).
- `media_cleanup.py`: the package no longer arms its timer; state its known
  defects (asynchronous deletion makes it overshoot, re-requested titles
  ranked first, watch history matched on localized titles, carries on when
  Tautulli or an *arr is down) and that a Maintainerr pilot is the next step;
  how to run it with `--dry-run`.
- `.env.example` → `stack/env.template`.
- README: the "no manual step" claim, the activate row, "eight questions"
  (there are six), the broken optional-services table, the troubleshooting
  advice (check devices with `docker inspect`, not `grep COMPOSE_FILE`), the
  Watchtower section, and the license (PolyForm Noncommercial 1.0.0, see
  `LICENSE.md`).
