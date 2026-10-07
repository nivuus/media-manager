# Recyclarr — the language policy as code

Service `recyclarr` (`stack/docker-compose.recyclarr.yml`, included by
`docker-compose.yml`), image `ghcr.io/recyclarr/recyclarr:8`, configuration in
`stack/recyclarr/`. It syncs once a day (`CRON_SCHEDULE=@daily`).

## What it manages, and what it does not

`recyclarr.yml` reproduces the policy described in CLAUDE.md ("Both quality
profiles carry the language policy"), score for score, on both profiles of
both instances. Before it was installed (2026-10-07), a preview sync against
the reference host reported **no change** for formats and profiles, and the
first real sync adopted the nine existing formats without modifying them.

- **Managed:** the custom formats (`stack/recyclarr/custom-formats/<app>/*.json`,
  declared as local resource providers in `settings.yml`), their scores in the
  `1080p` and `4K` profiles, `min_format_score`, `upgrade.until_score`.
  `reset_unmatched_scores` is on: a format scored by hand in the UI and absent
  from `recyclarr.yml` goes back to 0 at the next sync.
- **Not managed:** each profile's qualities and cutoff quality, quality
  definitions (sizes), media naming. Recyclarr leaves alone what the YAML does
  not mention.

To change the policy, edit `recyclarr.yml` (or a JSON file) in this repository.
An edit made in the Radarr/Sonarr UI is reverted the next day. Adding TRaSH
guide formats changes what gets grabbed: make that change on purpose, in its
own commit, after a `--preview`.

## Commands

From `/opt/nivuus/media-manager`, never with an explicit `-f`:

```bash
docker compose exec recyclarr recyclarr sync --preview   # what would change
docker compose exec recyclarr recyclarr sync             # apply now
```

## Things that bit once

- `./recyclarr` is **both** the tracked configuration (laid by the install
  copy, root-owned 0644) **and** Recyclarr's state (`state/`: trash_id →
  service id mappings, written by the service). The directory itself therefore
  belongs to PUID:PGID (`hooks/data_dirs.py`). Mounting it read-only fails at
  start with `Access to the path '/config/state' is denied`.
- The tracked files used to deploy as 0640 (root's umask 027 copied from the
  checkout), unreadable by the service: `safe_copy` now writes git's modes
  (0644/0755) whatever the umask.
- The image is pinned to the major (`:8`): a Recyclarr major changes the
  config schema (v8 renamed `quality_profiles` under `custom_formats` to
  `assign_scores_to` and removed `replace_existing_custom_formats`). Read the
  upgrade guide before moving to `:9`.
- It reads `RADARR_API_KEY`/`SONARR_API_KEY` from its environment at creation.
  The activate hook therefore creates it only after the key harvest (see
  `KEY_CONSUMERS` in `hooks/activate.py`).
