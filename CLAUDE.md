# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MediaManager is a Docker-based media automation platform for movies and TV shows. It consists of microservices orchestrated via Docker Compose, handling the complete workflow from user requests to media library delivery with Real-Debrid downloads, AI-powered subtitles, and transcoding.

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
Removes failed downloads from Radarr (ports 7878, 7879) and Sonarr (ports 8989, 8990). Also cleans files older than 24h in the Downloads directory.

**Missing Content Search** (`update_wanted.py`):
```bash
python3 update_wanted.py
```
Searches for missing episodes/movies across all 4 instances (2 Radarr + 2 Sonarr) and triggers automatic downloads.

## Architecture

### Dual-Instance Pattern
Critical services run in **pairs** for standard and 4K quality:
- **Radarr**: ports 7878 (standard), 7879 (4K)
- **Sonarr**: ports 8989 (standard), 8990 (4K)
- **Bazarr**: ports 6767 (standard), 6768 (4K) — subtitles for both

### Download Client
- **RDTClient** (port 6500): Real-Debrid download client
- Downloads are handled by Real-Debrid servers (no local P2P)
- Files are fetched via HTTPS from Real-Debrid

### Data Flow
```
User Request (Overseerr :5055)
    ↓
Search (Prowlarr :9696 → Indexers via FlareSolverr)
    ↓
Download (RDTClient :6500 → Real-Debrid)
    ↓
Process (Move to Movies or TV Shows directory)
    ↓
Transcode (Tdarr :8265-8266, optional optimization)
    ↓
Subtitles (Bazarr :6767/6768 → Providers + Whisper AI :9000)
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
- `rdtclient.db` - Real-Debrid downloads (ASP.NET Core Identity)
- `tautulli.db` - Plex viewing statistics

### Critical Path Mappings
Configured via `.env` variables (`MEDIA_ROOT`, `DOWNLOADS_DIR`, `MOVIES_DIR`, `TV_DIR`):
```
$MEDIA_ROOT/
├── Downloads/      # Temporary (shared by all download clients)
├── Movies/         # Final library (Radarr + Radarr-4K)
└── TV Shows/       # Final library (Sonarr + Sonarr-4K)
```

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

### When Modifying Python Scripts:
- All scripts read configuration from environment variables (via `python-dotenv`)
- `reset-error.py`: Radarr/Sonarr use REST API v3, cleanup deletes files >24h
- `update_wanted.py`: Uses URLs from environment variables
- `media_cleanup.py`: Disk cleanup with Tautulli watch data correlation
- All scripts handle **4 instances total** (2 movies + 2 TV)

### When Troubleshooting:
1. Verify Prowlarr indexer sync status
2. Check RDTClient connection to Real-Debrid
3. Run `reset-error.py` to clear stuck downloads
4. Check logs: `docker compose logs -f <service>`

## Hardware Acceleration

Services using Intel QSV (`/dev/dri` device):
- **Tdarr + Tdarr-Node**: FFmpeg 7 transcoding
- **Whisper ASR**: AI subtitle generation (faster_whisper engine)

Both require host GPU passthrough to work.

## Security Notes

- Overseerr is **localhost-only** (`127.0.0.1:5055`)
- Downloads via Real-Debrid (no local P2P exposure)
- User quotas in Overseerr: 10 movies/TV per 7 days
- API keys are configured via `.env` (not tracked in git)
