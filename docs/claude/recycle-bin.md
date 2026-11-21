# Recycle bin — Radarr and Sonarr

Both apps delete a replaced or removed file for good unless a recycle bin is
set. On 2026-11-21 an interactive search grabbed a 2160p English/Italian
"DUAL" release of a film already held as a French 1080p remux; Radarr
imported it as an upgrade (quality rank beats custom-format score) and the
French file was gone.

## What the package sets

The activate hook (`hooks/recycle_bin.py`, called from `hooks/activate.py`):

- creates `${MEDIA_ROOT}/.recycle-bin/radarr` and `.../sonarr`, 0750, owned
  by PUID:PGID, through `data_dirs.ensure_data_dirs` (no symlink followed at
  any level: the media root is world-writable on the reference host);
- sets `recycleBin` to `/data/.recycle-bin/<app>` and
  `recycleBinCleanupDays` to 7, through `PUT /api/v3/config/mediamanagement/1`,
  **only while `recycleBin` is empty**. A path chosen in the UI is left alone
  and the hook says so.

Recyclarr cannot do this: its `media_management` section only knows
`propers_and_repacks`.

Why there: the bin must exist before it is set (the apps validate that the
path exists, is writable, and is neither a root folder nor an ancestor of
one), and it must sit inside the `${MEDIA_ROOT}:/data` bind mount so that
sending a 50 GB file to it is a rename, not a copy.

## Restoring a file

Move it back from `${MEDIA_ROOT}/.recycle-bin/<app>/<folder>/` into the
library folder, then refresh the title in the app (or Manual Import).

## Keep the cleanup below 14 days

Maintainerr deletes through Radarr/Sonarr, so its deletions land in the bin
too, and the space comes back only when the bin is purged. Its collections
wait 14 days before deleting (CLAUDE.md, "Disk Cleanup"); the purge must
happen before the next batch is due, or the free-space rules would keep
matching titles they no longer need to. `CLEANUP_DAYS` is 7; a test holds it
below 14.
