# Maintenance scripts — the safeguards taken from Cleanuparr

On 2026-10-07 Cleanuparr, Decluttarr and Huntarr were evaluated as
replacements for `reset-error` and `update-wanted`. None of them can replace
`reset-error`:
- none of them has the storage guard or the queue-trust gate;
- Decluttarr deletes failed imports, with a blocklist;
- Cleanuparr force-imports only three English rejection messages and has no
  debrid support;
- Huntarr was abandoned after an unpatched API-key leak.

Five of Cleanuparr's ideas were ported instead. Every constant named below
lives in the module given.

## reset-error

**Quarantine, not deletion** (`quarantine.py`). The 24 h purge renames each
abandoned file to `DOWNLOADS_DIR/.quarantine/<UTC batch time>/<its path under
Downloads>`, and `expire()` deletes a batch once it is older than
`QUARANTINE_HOURS` (72). To restore a file, move it back to the same path
under Downloads.
- The rename stays inside the Downloads filesystem. A file whose rename would
  cross a mount point (EXDEV) is left where it is and the failure is recorded:
  it is never copied and never deleted.
- The quarantine is invisible to the purge, to the empty-directory sweep and to
  the files-gone listing (`downloads_purge.walk_downloads`).
- A directory in `.quarantine` whose name is not a batch time is left alone,
  with a warning.
- Disk cost: a purged file now occupies its space for 24 h + 72 h instead of
  24 h.

**Two observations, then an idle instance** (`import_guard.py`). The first read
of the queue marks the import-pending rows. A manual import then only happens
under both conditions below:
- a second read, `CONFIRM_DELAY_SECONDS` (120) later, still shows the row
  import-pending. Radarr/Sonarr import most downloads themselves a minute after
  they finish.
- the instance has no import-related command `queued`/`started` in `GET
  /api/v3/command`, polled for up to `IDLE_TIMEOUT_SECONDS` (300). Commands are
  matched on `name` (`ManualImport`, `ProcessMonitoredDownloads`,
  `RefreshMonitoredDownloads`, `Downloaded{Movies,Episodes}Scan`), never on
  message text.

A row that is not cleared is kept for the next run, never removed for that. An
unreadable command list or second read is a failure and blocks that instance's
imports.

**Attempts per download are not counted.** Neither app keeps a trace of a
ManualImport that imported nothing: there is no history event, and the command
list is short and in memory. Counting attempts would therefore need a state
file. The bound is the existing age rule: a row still import-pending 72 h after
it was added is removed with a blocklist, so a daily run tries it at most three
times.

**Replacement search** (`replacement_search.py`). After a removal with a
blocklist, Radarr/Sonarr already search again when `autoRedownloadFailed` is on
(their default, and the reference host's setting). The script reads that
setting once per instance and run, and searches the movie or the episodes of
the download itself only when the setting is off. It does so for at most
`REPLACEMENT_SEARCH_BUDGET` (10) downloads per run. If the setting cannot be
read, that is a failure and nothing is searched blind.

## update-wanted

- **Queue threshold.** An instance whose `GET /api/v3/queue/status`
  `totalCount` exceeds `MAX_ACTIVE_QUEUE` (20) is not searched this run (a
  warning). An unreadable queue size is a failure, and that instance is not
  searched.
- **Release grace.** Nothing is searched until `RELEASE_GRACE_HOURS` (6) after
  its release/air date: before that, the releases have not reached the
  indexers.

## Run time

`reset-error` can now wait up to 120 s + 300 s per instance with pending rows,
well inside the unit's `TimeoutStartSec=3600`.
