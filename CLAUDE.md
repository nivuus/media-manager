# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MediaManager is a Docker-based media automation platform for movies and TV shows. It consists of microservices orchestrated via Docker Compose, handling the complete workflow from user requests to media library delivery with AllDebrid downloads, Bazarr-sourced subtitles, and transcoding.

## Ce dépôt est un package Nivuus

Contrat `nivuus.dev/v1`, consommé par `~/Projects/Nivuus/packages/installer`.
Quatre conséquences qui priment sur tout le reste de ce fichier :

- **`stack/` EST le répertoire de déploiement**, à l'octet près. Ajouter un
  service au compose sans mettre son asset sous `stack/` le rend absent de la
  cible. Le hook `install` copie le sous-arbre entier, il n'a pas de liste.
- **Seuls les fichiers commités voyagent** : `build.sh` exporte par
  `git archive HEAD`. Un fichier laissé non commité n'existe pas pour l'ISO.
- **`tier: userspace`** : déclarer `kernel-cmdline`, `modules` ou
  `hugepages-mib` fait **refuser** le manifeste par le moteur, pas ignorer la
  clé.
- **Aucun `claims:`, délibérément.** Le package `console` réclame le GPU en
  exclusivité ; un claim identique ici rendrait les deux mutuellement
  exclusifs, alors que le node NVENC partage justement la carte avec la VM
  Windows via les hooks libvirt de `console`.

Les chemins ont changé : le compose, les trois scripts de maintenance et les
assets Tdarr sont sous `stack/`. Le déploiement est `/opt/nivuus/media-manager`
(et non plus `/opt/nivuus/MediaManager`).

Les trois lignes de crontab sont remplacées par
`media-manager-{reset-error,update-wanted,cleanup}.timer`, livrées par le
package. Seuls `reset-error` et `update-wanted` sont armés par le hook
`activate` : `cleanup` est livré mais volontairement pas armé — voir
« Maintenance Scripts » pour ses défauts connus. Tests : `make test`.

## Essential Commands

### Container Management
```bash
# Start all services
docker compose up -d

# Stop all services
docker compose down

# Restart specific service
docker compose restart <service_name>

# View logs for specific service
docker compose logs -f <service_name>

# Check service status
docker compose ps
```

**Before running any of these against the live deployment, read "Container
Updates" below** — an explicit `-f docker-compose.yml` silently drops hardware
transcoding, and a global `up -d`/`restart` can fight the Windows VM's libvirt
hooks.

### Maintenance Scripts

**Error Cleanup** (`stack/reset-error.py`, a thin wrapper — a hyphenated file
name cannot be imported — around `stack/maintenance/reset_error.py`, which
holds the actual logic and is what the tests import):
```bash
systemctl start media-manager-reset-error.service   # ou : python3 stack/reset-error.py
```
Fails closed: before touching anything, a storage guard
(`stack/maintenance/storage_guard.py`) checks that `DOWNLOADS_DIR` is a real
directory and that no Radarr/Sonarr root folder reports `accessible=false`;
if either trips, the run changes nothing at all and exits 1. The 24h
Downloads purge itself only runs when **every** instance's queue is trusted
(`stack/maintenance/queue_trust.py`): its download clients must pass `POST
/api/v3/downloadclient/testall` both before and after a forced
`RefreshMonitoredDownloads`, and `GET /api/v3/health` must report neither
`DownloadClientStatusCheck` nor `DownloadClientCheck`. A failure the API
marks as a warning would pass testall — but Radarr/Sonarr serialise every
testall failure as the base `ValidationFailure` type today, which drops that
marker, so in practice any testall warning currently counts as a failure and
skips the purge; the run records it, and `OnFailure=` can alert on it.
Radarr/Sonarr serve the queue from memory — empty
right after a restart, and missing every download of a client that is down
or backed off — so purging on an untrusted queue once deleted a completed,
unimported 36.8 GB download (audit C2). Queue rows that were actually read
are still processed whether or not their queue was trusted.

A finished download that failed to import (`importPending`/`importBlocked`)
is **imported, never deleted**: the script calls the manual-import API for it
(120s read timeout — Radarr/Sonarr ffprobe every file in the download).
Deleting those instead re-opened the exact same grab on every cycle —
measured at 64 re-grabs of a single season pack, which is what exhausted the
indexer API quotas. A row whose files vanished from `DOWNLOADS_DIR` is
removed **without** blocklisting (the release itself was fine); one stuck
more than 72h, or one Radarr/Sonarr flags as a bad release, is removed
**with** blocklisting so the next search picks a different one. Files-gone is
only concluded while `MOVIES_DIR` or `TV_DIR` holds something
(`storage_guard.library_doubt`): with the media disk missing at boot, Docker
recreates every bind source as an empty directory, so the root folders report
accessible and the storage guard passes; an empty library then withholds
files-gone for the run, with a warning, not a failure — a fresh install only
waits for its first import. Exit status is 1 on any API/I/O failure — the
install hook wires that to `OnFailure=systemd-failure-notify@%n.service`
through a drop-in, when the host provides that unit. One such alert is
expected after a reboot that missed 06:00: the timer is `Persistent=true`
and the unit is only ordered `After=docker.service`, so the catch-up run at
boot can reach Radarr/Sonarr before they listen; the storage guard records
"rootfolder endpoint unreadable", nothing is changed, and the run exits 1.
There is deliberately no readiness wait: it would hide a real outage for as
long as it waited. Logs go to stdout (the journal) and to
`/var/log/media-manager/reset-error.log`, rotated at 5 x 5 MiB. Lancé
quotidiennement à 06:00 par `media-manager-reset-error.timer`, livré par le
package.

**Missing Content Search** (`stack/update_wanted.py`, same wrapper pattern,
logic in `stack/maintenance/update_wanted.py`):
```bash
systemctl start media-manager-update-wanted.service # ou : python3 stack/update_wanted.py
```
Searches for missing episodes/movies across both instances (Radarr + Sonarr)
and triggers automatic downloads. Each run searches up to
`MAX_SEARCH_PER_INSTANCE` (100) items per instance: everything
released/aired within `RECENT_WINDOW_DAYS` (30 days) first, newest first,
then the rest of the budget from the backlog ordered by `lastSearchTime`
ascending, never-searched first — deterministic and stateless, there is no
state file to keep in sync (do not reintroduce `.update_wanted_state`).
Instances are spaced by `DELAY_BETWEEN_INSTANCES` (120s). Unreleased/unaired
items are skipped. Exit status is 1 if any instance failed. The same boot
catch-up applies after a reboot that missed 07:00: an instance not listening
yet records "cannot fetch the missing list", is not searched, and the run
exits 1 — one expected `OnFailure=` alert, and no readiness wait, for the
same reason. Searching everything at once made the indexers answer 429 and
Prowlarr disabled them for hours. Logs go to stdout and to
`/var/log/media-manager/update-wanted.log`, same rotation as above.

**Disk Cleanup** (`stack/media_cleanup.py`): frees space by deleting the
least-recently-watched movies/series once free space drops under a
threshold, using Tautulli's watch history. The package still ships
`media-manager-cleanup.timer`, but `activate` deliberately does not arm it —
disabled on the reference host since 2026-09-28, pending a Maintainerr pilot
to replace it. Known defects: it can overshoot the threshold (its post-delete
check re-reads real disk usage right away, before Radarr/Sonarr's own file
deletion is necessarily reflected on disk); a re-requested title is ranked
first for deletion again (Tautulli watch data is keyed by title/year and
outlives the deletion, so a freshly re-downloaded title inherits its old
watch date and looks stale immediately); the watch-history match is done on
the raw title text, so a Plex library using localised titles never lines up
with Radarr/Sonarr's own title; and it carries on with partial or empty data
when Tautulli or an `*arr` instance is unreachable, instead of stopping. Run
it by hand only, and always start with `--dry-run`:
```bash
python3 stack/media_cleanup.py --dry-run
```

## Architecture

### Single Instance Per Media Type
One **Radarr** (7878), one **Sonarr** (8989), one **Bazarr** (6767). The stack
ran each of them as a standard/4K pair until 2026-08-18; do not recreate the
pair without reading why it went away.

The pair existed because 4K is a *server*-level flag, not a per-request one:
Seerr routes a request with `findIndex(r => r.is4k && r.isDefault)`
(`dist/entity/MediaRequest.js`), and Radarr/Sonarr track one title with one
quality profile and one file. Two coexisting versions therefore need two
instances — no frontend setting changes that.

What killed it here: both members shared a root folder, so each kept importing
the other's files (11 of radarr-4k's 60 files were 1080p, 46 of radarr's were
2160p), while only 18 of 856 movie folders ever held two versions. The pair
cost two containers and four Prowlarr app syncs to deliver a "search with a 4K
profile", not a second copy.

The 2160p files already in the library are **kept**: their quality sits outside
the 1080p profile, and Radarr does not list them as cutoff-unmet, so nothing
tries to replace them. Verify before touching a profile:
`curl -H "X-Api-Key: $RADARR_API_KEY" localhost:7878/api/v3/wanted/cutoff`.
Widening the profile to allow 2160p would make every new grab prefer 4K —
quality rank beats custom-format score in release selection.

### 4K requests without a second instance
Seerr declares **two servers per type pointing at the same instance**: `Radarr`
(profile `1080p`) and `Radarr 4K` (profile `4K`) both on `radarr:7878`, same for
Sonarr on `8989`. That is what makes `movie4kEnabled` / `series4kEnabled` true
and brings the 4K button back, without a second container.

Tested, so don't re-derive it: the Radarr/Sonarr **scanners de-duplicate servers
by hostname/port/baseUrl** (`uniqWith`, `lib/scanners/radarr/index.js:43`), so
the 4K entry is never scanned and no status4k is falsified — measured at 0
change over 1353 movies. 4K availability comes from the **Plex** scanner
instead, which reads the real `videoResolution`.

What this setup does NOT do, by design (`api/servarr/radarr.js:36`,
`if (movie.hasFile) → skipping add and returning success`): a 4K request on a
title that is already in the library is silently marked as satisfied. **First
request wins** — a title requested in 1080p by anyone else can no longer be had
in 4K, since one Radarr record carries one quality profile and one file.

Approval routing needs no rule, it falls out of the permission bits:
auto-approving a 4K request requires `AUTO_APPROVE_4K` (never `AUTO_APPROVE`) or
`MANAGE_REQUESTS` — `entity/MediaRequest.js:306`. Users hold `REQUEST_4K`
without `AUTO_APPROVE_4K`, so their 1080p flows through and their 4K waits for
approval; the admin bit short-circuits everything (`lib/permissions.js:66`).
**Do not strip `REQUEST_4K` from the users** — that is what lets them ask for 4K
at all.

**Both quality profiles carry the language policy, not just the 1080p one.** The
`4K` profiles (Radarr id 11, Sonarr id 9) shipped with every custom format at
score 0 and `language: Original`, so a 4K request had no French preference and
took whatever release came first — 7 of the 28 movies on that profile landed in
English-only (the 4K Bond batch of 2026-08-21). Aligned on 2026-08-24 with the
1080p profile: French/`MULTI-FRENCH (titre)` +100, English −20, BR-DISK −10000,
`minFormatScore -1000` (English fallback kept when no French 4K release exists),
`cutoffFormatScore 3` (an English file stays below the cutoff, so it remains
upgradable), `language: Any` (`Original` rejected French-only releases outright).
Sonarr's quality profiles have no `language` field — that one is Radarr-only.

A title already imported in English is **not** re-searched on its own: Radarr
only reconsiders it when RSS sync happens to surface a better release. There are
379 movies under the cutoff; searching them all is what makes the indexers answer
429. Re-search a bounded slice by movie id via `POST /api/v3/command`
`{"name": "MoviesSearch", "movieIds": [...]}`.

Seerr's API cannot be driven over plain HTTP: `network.csrfProtection` is on and
the `_csrf` cookie is `secure`, so every POST/PUT/DELETE returns 403 even with a
valid `X-Api-Key` + `X-API-User`. Change the config by stopping the container and
editing `overseerr/settings.json` (rewritten on boot); job schedules live there
too (`jobs.radarr-scan.schedule`).

### Download Client
- **RDTClient** (port 6500): debrid download client. Despite the name, the
  configured provider is **AllDebrid** (`Provider:Provider = 1` in the `Settings`
  table), not Real-Debrid
- Downloads are handled by the debrid servers (no local P2P)
- Files are fetched via HTTPS from AllDebrid
- RDTClient only unrestricts a torrent's links **when it starts downloading it**,
  and runs at most `General:DownloadLimit` downloads at once (8). Downloads that
  hang — started, file complete on disk, `DownloadFinished` still NULL — keep
  their slot forever, and every other torrent then sits on "waiting for download
  links" even though the debrid side is ready. Diagnose in `rdtclient.db`:
  `SELECT FileName, DownloadStarted, DownloadFinished FROM Downloads WHERE Completed IS NULL`,
  and compare the `.download` file size against `Torrents.RdSize`. Unblock with
  `docker compose restart rdtclient`.
- Transient `ProviderUpdater` errors (AllDebrid "database error", 10s HTTP
  timeout) are normal and self-healing — the loop retries on the next pass. They
  are not the cause of stalled queues.

### Data Flow
```
User Request (Seerr :5055)
    ↓
Search (Prowlarr :9696 → Indexers via FlareSolverr)
    ↓
Download (RDTClient :6500 → AllDebrid)
    ↓
Process (Move to Movies or TV Shows directory)
    ↓
Transcode (Tdarr :8265-8266, optional optimization)
    ↓
Subtitles (Bazarr :6767 → Providers)
    ↓
Library (Plex via Tautulli :8181 monitoring)
```

## Key Technical Details

### API Keys
All API keys are configured via environment variables in `.env`, rendered by
the `install` hook from `stack/env.template` — see that file for the full
list.

### Database Technology
All services use **SQLite3** with WAL journaling:
- `radarr.db`, `sonarr.db` - Media metadata and quality profiles
- `prowlarr.db` - Indexer configurations
- `bazarr.db` - Subtitle providers and language profiles
- `rdtclient.db` - debrid downloads and their queue state (ASP.NET Core Identity)
- `tautulli.db` - Plex viewing statistics

### Critical Path Mappings
Configured via `.env` variables (`MEDIA_ROOT`, `DOWNLOADS_DIR`, `MOVIES_DIR`, `TV_DIR`):
```
$MEDIA_ROOT/
├── Downloads/      # Temporary (shared by all download clients)
├── Movies/         # Final library (Radarr)
└── TV Shows/       # Final library (Sonarr)
```

Radarr and Sonarr mount **`${MEDIA_ROOT}:/data` as a single
bind mount**, not one per directory. Hardlinks only work inside one mount point:
with `Downloads` and `Movies` bound separately, `link()` fails with `EXDEV` even
though both sit on the same filesystem, and Radarr/Sonarr silently fall back to a
full copy — 13 min for a 4.8 GB import, with the space used twice until the 24h
purge. Keep the single mount; the inner paths are unchanged, so nothing else
needs reconfiguring. Verify with:
`docker compose exec radarr sh -c 'ln /data/Downloads/x /data/Movies/x'`

## Service Dependencies

### Hard Dependencies (must start in order)
1. **Prowlarr** → Radarr/Sonarr instances (indexer sync)
2. **RDTClient + FlareSolverr** → Prowlarr (download client + Cloudflare bypass)
3. **Radarr/Sonarr** → Bazarr instances (subtitle automation)
4. **Tdarr** → Tdarr-Node (distributed transcoding)

### API Communication
All services expose REST APIs. Inter-service communication uses Docker DNS (e.g., `http://prowlarr:9696`, `http://radarr:7878`).

## Container Updates

Every service carries a `com.centurylinklabs.watchtower.enable: true` label
in `docker-compose.yml`, but the updater reading it is **not** Watchtower: it
is `mqtt-system-agent` (repository `packages/mqtt`, `src/features/updates/`),
which reuses that label name to pick which containers to update. It replays
each container's own compose files and recreates one service at a time with
`--no-deps`; it never starts a container that is currently stopped.

**Compose must be run from `/opt/nivuus/media-manager` without an explicit
`-f`.** The deployed `.env` sets `COMPOSE_FILE` to merge in
`docker-compose.qsv.yml` (Intel QSV, `/dev/dri`) whenever the host has
hardware transcoding. Passing `-f docker-compose.yml` explicitly bypasses
`COMPOSE_FILE` and silently drops that overlay — that is how Plex lost
hardware transcoding from 2026-09-04 to 2026-09-28. Verify a container
actually has the device, rather than trusting the `.env`:
`docker inspect -f '{{json .HostConfig.Devices}}' <container>`.

**Never run a global `docker compose up -d` or `restart` while the Windows VM
(package `console`) is running.** Its libvirt hooks deliberately stop
`tdarr-node` and `tdarr-node-nvenc` for the duration of the game — the CPU
node to give its cores back to the VM, the NVENC node because a container
holding the GPU blocks the vfio bind — and a blanket `up`/`restart` starts
them right back up in the middle of it, with nothing to signal it happened.
Target one service at a time with `--no-deps` instead, e.g. to recreate
`tdarr-node` mid-game:
```bash
docker compose up --no-start --no-deps tdarr-node
```
The console package's own libvirt hooks stop `tdarr-node-nvenc` and
`tdarr-node` when the VM starts and `start` (never `up`) them again when it
stops.

These guarantees hold since the 2026-09-28 fixes in `mqtt-system-agent` and in
the `console` package's libvirt hooks. An older version of either can still
recreate a container with an explicit `-f` or a plain `up` instead of
`--no-deps`/`start`, which drops the QSV overlay and can start the Tdarr
nodes mid-game — check `docker inspect` after an update to confirm which
version actually ran.

## Modification Guidelines

### When Editing `docker-compose.yml`:
- Preserve `depends_on` chains (dependencies are critical)
- Never remove the `com.centurylinklabs.watchtower.enable: true` labels:
  despite the name, `mqtt-system-agent` uses them to select which containers
  to auto-update (see "Container Updates")
- All PUID/PGID and paths are parameterized via `.env`
- Never split `${MEDIA_ROOT}:/data` back into per-directory mounts on
  Radarr/Sonarr — it silently disables hardlinks (see Critical Path Mappings)

### When Modifying Python Scripts:
- All scripts read configuration from environment variables (via `python-dotenv`)
- `stack/maintenance/reset_error.py` (run through the `stack/reset-error.py`
  wrapper): fails closed on a storage/queue-trust problem before touching
  anything; never deletes a completed-but-unimported download, it
  manual-imports it instead (see above)
- `stack/maintenance/update_wanted.py` (run through the `stack/update_wanted.py`
  wrapper): deterministic and stateless — do not bring back a state file;
  searches a bounded slice per run rather than the whole missing list
- `stack/media_cleanup.py`: disk cleanup with Tautulli watch data correlation;
  known defects, not armed by `activate` (see "Maintenance Scripts")
- Radarr/Sonarr instances are still declared as lists —
  `radarr_instances()`/`sonarr_instances()` in `stack/maintenance/arr_api.py`,
  `RADARR_INSTANCES`/`SONARR_INSTANCES` in `media_cleanup.py` — even though
  there is one of each: adding an instance back stays a one-line change,
  every caller already loops

### When Troubleshooting:
1. Verify Prowlarr indexer sync status
2. Check RDTClient connection to AllDebrid, and that no download is holding a slot indefinitely
3. Run `systemctl start media-manager-reset-error.service` to clear stuck downloads
4. Check logs: `docker compose logs -f <service>`

## Hardware Acceleration

Services using Intel QSV (`/dev/dri` device), merged in by
`docker-compose.qsv.yml` only when the install hook finds it on the host:
- **Tdarr + Tdarr-Node**: FFmpeg 7 transcoding
- **Plex**: hardware-accelerated transcoding

All three require host GPU passthrough to work; see "Container Updates" for
why an explicit `-f docker-compose.yml` silently drops this overlay.

### Tdarr resource limits
Two failure modes produced `transcodeError` verdicts until 2026-08-24, both
resource limits rather than flow bugs — check these before editing the flow:

- **Nodes OOM on 4K sources.** `mem_limit` was 4 Go while a 4K remux encode sits
  at ~3,3 Go anon-rss on its own; the cgroup OOM killer took 4 `tdarr-ffmpeg`
  processes (`memory.events` → `oom_kill`, and the kernel journal names the
  cgroup). ffmpeg dying on a signal logs `CLI tdarr-ffmpeg exited with code:
  null` — an exit code of *null*, not a number, is the signature. Now 10 Go.
- **Server too small to answer its own nodes.** With 2 CPU / 2 Go, the server
  took up to 106 s to answer `POST /api/v2/update-node-relay` (node timeout:
  30 s), deregistering the nodes 172 times in 24 h. A worker that loses the
  server between two plugins fails with `Failed to load flow plugin handler` /
  `Cannot read properties of undefined (reading 'flowPluginStates')` **after its
  ffmpeg returned 0** — a successful encode thrown away. Now 4 CPU / 4 Go.

The nodes' `cpus: 2` is a separate, deliberate cap (platform freezes, see the
comment in `docker-compose.yml`) — raising memory does not touch it.

Job logs live in `tdarr/server/Tdarr/DB2/JobReports/<footprintId>/`; map a file
to its footprintId through `POST /api/v2/client/status-tables` with
`opts.table = "table3"` (the error table). Re-queue with
`POST /api/v2/bulk-update-files` `{"data": {"fileIds": [...], "updatedObj":
{"TranscodeDecisionMaker": "Queued"}}}`.

## Security Notes

- Seerr is **localhost-only** (`127.0.0.1:5055`)
- Downloads via AllDebrid (no local P2P exposure)
- User quotas in Seerr: 10 movies/TV per 7 days
- API keys are configured via `.env` (not tracked in git)
