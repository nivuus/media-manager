# media-manager

A Docker-based media automation platform for movies and TV shows — from a user
request to a file in the Plex library, with AllDebrid downloads, AI-generated
subtitles and hardware transcoding.

It is packaged as a **Nivuus package** (`nivuus.dev/v1`): the
[installer](https://github.com/nivuus/installer) embeds it in its ISO and
deploys it in two phases, with no manual step.

## Data flow

```
User request (Seerr :5055)
    ↓
Search (Prowlarr :9696 → indexers via FlareSolverr)
    ↓
Download (RDTClient :6500 → AllDebrid)
    ↓
Organize (Radarr :7878 · Sonarr :8989)
    ↓
Transcode (Tdarr :8265-8266, optional)
    ↓
Subtitles (Bazarr :6767 → providers + Whisper ASR :9000)
    ↓
Library (Plex, monitored by Tautulli :8181)
```

## Installation

```bash
cd ~/Projects/Nivuus/packages/installer/installer
PACKAGE_REPOS="$HOME/Projects/Nivuus/packages/media-manager" sudo -E make build-iso
```

The export uses `git archive HEAD`: **only committed files travel**. A file
left uncommitted does not exist as far as the installer is concerned.

| Phase | When | What it does |
|---|---|---|
| `install` | On the target filesystem | Copies `stack/` to `/opt/nivuus/media-manager`, renders `.env`, places the six maintenance units |
| `activate` | After the reboot, network up | `docker compose up -d`, harvests the Radarr/Sonarr/Prowlarr API keys, arms the three timers |

There is no `resolve` phase: the package is `tier: userspace` — it declares no
kernel parameter, no module and no hugepage, so there is nothing to resolve.
A machine without `/dev/dri` is not refused either; Plex and Tdarr fall back to
software transcoding.

On a machine that is already installed, run the `install` phase directly:

```bash
echo '{"package":{},"hw":{},"answers":{"media_root":"/media/data","nvenc_node":false}}' \
  | sudo python3 hooks/install.py --phase install --root /
```

An existing `.env` is **never overwritten** — missing variables are appended,
existing values and comments are left alone.

## Repository layout

| Path | What it is |
|---|---|
| `nivuus-package.yaml` | The manifest. `tier: userspace`, no `claims`, no `requires` |
| `wizard.yaml` | The eight questions the portal asks |
| `hooks/` | `install.py` and `activate.py` — stdlib only, they run on a minimal Debian |
| `stack/` | **The deployment directory, byte for byte.** Compose files, the three maintenance scripts, the Tdarr assets |
| `systemd/` | The three service/timer pairs that replace the crontab |
| `tests/` | Five standalone suites, run by `make test` |

`stack/` being the deployment directory verbatim is what makes the `install`
hook a plain recursive copy: there is no file list to keep in sync, so there is
no file anyone can forget to add alongside a new service.

## Configuration

`.env` is rendered by the `install` hook from the wizard answers and lives at
`/opt/nivuus/media-manager/.env`, mode 0600.

| Variable | Source | Example |
|---|---|---|
| `MEDIA_ROOT` | wizard | `/media/data` |
| `DOWNLOADS_DIR` · `MOVIES_DIR` · `TV_DIR` | **derived** from `MEDIA_ROOT` | `/media/data/Downloads` |
| `TRANSCODE_DIR` | wizard | `/media/backup/.transcode` |
| `COMPOSE_FILE` | `/dev/dri` present or not | `docker-compose.yml:docker-compose.qsv.yml` |
| `COMPOSE_PROFILES` | wizard (`nvenc_node`) | `nvenc` |
| `VIDEO_GID` · `RENDER_GID` | target's `/etc/group` | `44` · `105` |
| `RADARR_API_KEY` · `SONARR_API_KEY` · `PROWLARR_API_KEY` | harvested by `activate` | — |
| `BAZARR_API_KEY` · `TAUTULLI_API_KEY` · `OVERSEERR_API_KEY` | **by hand**, Settings > General | — |

The three library directories are derived rather than asked: they are three
subdirectories of one mount point, and separating them is what silently
disabled hardlinks once — a 4.8 GB import went from seconds to 13 minutes, with
the space used twice until the 24h purge.

## Maintenance

Three systemd timers, shipped by the package and armed by the `activate` hook:

| Unit | Time | What it does |
|---|---|---|
| `media-manager-reset-error.timer` | 06:00 | Clears failed downloads, imports the completed-but-unimported ones |
| `media-manager-update-wanted.timer` | 07:00 | Searches a bounded, rotating slice of the missing backlog |
| `media-manager-cleanup.timer` | 08:00 | Frees disk space, least-watched first, using Tautulli data |

`media-manager-cleanup.timer` is armed **only once `TAUTULLI_API_KEY` is set**:
`media_cleanup.py` exits 1 without it, so arming it on a fresh install would
produce a failed unit every day at 08:00. That key is not harvestable — Tautulli
does not write it to a `config.xml`. Fill it in `.env`, then re-run the
`activate` hook (or link the timer by hand).

```bash
systemctl list-timers 'media-manager-*'
systemctl start media-manager-reset-error.service   # run one now
journalctl -u media-manager-reset-error.service -n 50
```

The scripts also run by hand from the deployment directory:

```bash
cd /opt/nivuus/media-manager
python3 media_cleanup.py --status        # disk status
python3 media_cleanup.py --dry-run       # simulate
python3 media_cleanup.py --threshold 15  # custom threshold (15% free)
```

## Services

| Service | Port(s) | Description |
|---|---|---|
| **Seerr** | 5055 | Request portal (localhost only) |
| **Prowlarr** | 9696 | Indexer manager |
| **FlareSolverr** | — | Cloudflare bypass for indexers |
| **RDTClient** | 6500 | AllDebrid download client |
| **SABnzbd** | 8080 | Usenet download client |
| **Radarr** | 7878 | Movie management |
| **Sonarr** | 8989 | TV show management |
| **Bazarr** | 6767 | Subtitle automation |
| **Tdarr** | 8265-8266 | Transcoding server |
| **Tdarr-Node** | — | QSV transcoding worker |
| **Tdarr-Node-NVENC** | — | NVIDIA worker, profile `nvenc` |
| **Plex** | 32400 | Media server (host network) |
| **Tautulli** | 8181 | Plex monitoring and statistics |
| **Whisper ASR** | 9000 | AI subtitle generation |

### One instance per media type

One Radarr, one Sonarr, one Bazarr. The stack ran each of them as a
standard/4K pair until 2026-08-18, because Seerr routes a 4K request to a
*server* flagged `is4k` — there is no per-request quality profile. The pair was
dropped: both members shared a root folder and kept importing each other's
files, and only 18 of 856 movie folders ever held two versions. The 4K files
already in the library are kept — they sit outside the quality profile, so
Radarr and Sonarr leave them alone.

### No GPU claim, deliberately

The manifest declares no `claims:`. The `console` package claims the GPU
exclusively; an identical claim here would make the two mutually exclusive in
the engine's conflict check — while the NVENC Tdarr node is precisely designed
to *share* the card with the Windows VM, through `console`'s own libvirt hooks.

### Storage layout

```
$MEDIA_ROOT/
├── Downloads/      # temporary, shared by every download client
├── Movies/         # Radarr library
└── TV Shows/       # Sonarr library
```

Radarr and Sonarr mount `${MEDIA_ROOT}:/data` as a **single** bind mount, not
one per directory. Hardlinks only work inside one mount point — see the
`CLAUDE.md` note before changing it.

## Tests

```bash
make test
make test NIVUUS_INSTALLER_DIR=$HOME/Projects/Nivuus/packages/installer
```

The second form validates the manifest and the wizard with the engine's own
parser rather than the local re-check. That is the authoritative verification;
the local one exists so the repository stays testable on its own.

## Troubleshooting

```bash
cd /opt/nivuus/media-manager
docker compose ps
docker compose logs -f radarr
docker compose config              # validate the merged compose files
```

- **Indexers not syncing** — check Prowlarr's connection to FlareSolverr.
- **Downloads stuck on "waiting for download links"** — RDTClient's slots are
  saturated by hung downloads; `docker compose restart rdtclient`.
- **Transcoding errors** — check the node memory limits before the flow; see
  `CLAUDE.md`, "Tdarr resource limits".
- **No hardware transcoding** — `grep COMPOSE_FILE .env`: if the QSV overlay is
  missing, `/dev/dri` was absent when the `install` hook ran.

## Auto-updates

Every service carries a [Watchtower](https://containrrr.dev/watchtower/) label
for automatic image updates.

## License

MIT
