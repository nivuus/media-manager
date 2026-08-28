# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MediaManager is a Docker-based media automation platform for movies and TV shows. It consists of microservices orchestrated via Docker Compose, handling the complete workflow from user requests to media library delivery with AllDebrid downloads, AI-powered subtitles, and transcoding.

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

### Maintenance Scripts

**Error Cleanup** (`reset-error.py`):
```bash
python3 reset-error.py
```
Removes failed downloads from Radarr (port 7878) and Sonarr (port 8989), plus any queue item stuck >72h (removed + blocklisted). Also cleans files older than 24h in the Downloads directory, recreates the 2 category subdirs (radarr, tv-sonarr), and removes entries whose metadata was deleted from TMDb/TheTVDB (file-less only). Runs daily at 06:00 via root crontab.

A finished download that failed to import (`importPending`/`importBlocked`) is **imported, never deleted**: the script calls the manual-import API for it. Deleting those instead re-opened the exact same grab on every cycle — measured at 64 re-grabs of a single season pack, which is what exhausted the indexer API quotas. Deletion only happens after 72h, and always with blocklisting so the next search picks a different release.

**Missing Content Search** (`update_wanted.py`):
```bash
python3 update_wanted.py
```
Searches for missing episodes/movies across both instances (Radarr + Sonarr) and triggers automatic downloads. Each run searches a bounded slice (`MAX_SEARCH_PER_INSTANCE`, newest first) and rotates through the backlog across days via `.update_wanted_state`; instances are spaced by `DELAY_BETWEEN_INSTANCES`. Unreleased/unaired items are skipped. Searching everything at once made the indexers answer 429 and Prowlarr disabled them for hours.

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
Subtitles (Bazarr :6767 → Providers + Whisper AI :9000)
    ↓
Library (Plex via Tautulli :8181 monitoring)
```

## Key Technical Details

### API Keys
All API keys are configured via environment variables in `.env` — see `.env.example` for the full list.

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
4. **Whisper ASR** → Bazarr (AI subtitle generation fallback)
5. **Tdarr** → Tdarr-Node (distributed transcoding)

### API Communication
All services expose REST APIs. Inter-service communication uses Docker DNS (e.g., `http://prowlarr:9696`, `http://radarr:7878`).

## Modification Guidelines

### When Editing `docker-compose.yml`:
- Preserve `depends_on` chains (dependencies are critical)
- Never remove `labels.com.centurylinklabs.watchtower.enable: true` (auto-updates)
- All PUID/PGID and paths are parameterized via `.env`
- Never split `${MEDIA_ROOT}:/data` back into per-directory mounts on
  Radarr/Sonarr — it silently disables hardlinks (see Critical Path Mappings)

### When Modifying Python Scripts:
- All scripts read configuration from environment variables (via `python-dotenv`)
- `reset-error.py`: Radarr/Sonarr use REST API v3, cleanup deletes files >24h, purges queue items stuck >72h and dead TMDb/TVDB entries. Never delete a completed-but-unimported download: manual-import it (see above)
- `update_wanted.py`: Uses URLs from environment variables; searches a bounded, rotating slice rather than the whole missing list
- `media_cleanup.py`: Disk cleanup with Tautulli watch data correlation
- Instances are declared as lists (`RADARR_INSTANCES`, `instances`) even though
  there is now one of each: adding an instance back stays a one-line change

### When Troubleshooting:
1. Verify Prowlarr indexer sync status
2. Check RDTClient connection to AllDebrid, and that no download is holding a slot indefinitely
3. Run `reset-error.py` to clear stuck downloads
4. Check logs: `docker compose logs -f <service>`

## Hardware Acceleration

Services using Intel QSV (`/dev/dri` device):
- **Tdarr + Tdarr-Node**: FFmpeg 7 transcoding
- **Whisper ASR**: AI subtitle generation (faster_whisper engine)

Both require host GPU passthrough to work.

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
