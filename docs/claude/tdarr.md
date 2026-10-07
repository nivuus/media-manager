# Tdarr: the flow, its plugins, deploying and re-evaluating

One flow, `OLED4K_norm_v6`, runs both production libraries. It is generated,
never edited in the UI: `stack/scripts/tdarr/build_flow_v6.py` writes
`stack/tdarr/flows/OLED4K_norm_v6.json`, and the build is reproducible (a
rebuild of an unchanged script leaves no diff).

## What the flow does

Compatibility first, size second:

- **Video** — kept when HEVC or H264 and under the bitrate guard (overall
  bitrate, audio included: 1080p > 20 Mbps, 1440p/4K > 35 Mbps re-encode).
  Anything else is re-encoded to HEVC: NVENC on `NvencNode`, QSV on
  `MyInternalNode` (`checkNodeHardwareEncoder` picks per node), libx265 if the
  GPU encode fails. HDR10 mastering metadata survives both encoders
  (verified with ffprobe on output files). Dolby Vision is re-encoded as
  HDR10: the RPU is lost.
- **Audio** — every audio language gets one loudness-normalised AAC stereo
  track at 48 kHz, titled `AAC 2.0 (normalized)`, and exactly one audio track
  is flagged default (the stereo track of the primary language — the language
  of the first default track). Decided by the Local plugin
  `ensureStereoPerLanguage`, which groups tracks by their language tag
  whatever it is; commentary and audio description are recognised by
  disposition flags, never by title.
- **Kept file** — on the compatibility path the file goes through
  `Begin Command → ensureStereoPerLanguage → Execute`: a copy of every stream
  plus the audio fix, or nothing at all when the file is already right
  (`Already Compatible`). On the encode path the audio fix rides in the same
  ffmpeg pass as the video.
- **Secondary video** — `keepPrimaryVideo` drops every video stream but the
  first (cover images excepted) before encoding. A Dolby Vision profile 7
  enhancement layer, or an alternate 1080p version bundled with a 4K one,
  used to be encoded into a useless second video track.
- **Failures are failures** — a CPU fallback that fails too, or an output
  below 5 % of the original, ends the job in `transcodeError` (no `err1` edge,
  `failFlow`). Nothing routes a failure to "keep the original" as a success.

Plugin logic is tested outside Tdarr: `node --test tests/tdarr_plugins.test.cjs`
(part of `make test`) drives the real `plugin()` entry points.

## Why the audio was rebuilt everywhere (2026-10-07)

The previous plugin, `addNormalizedStereo`, wrote its track from `0:a:0`
only, whatever its language (3,731 multilingual files had a stereo track in
one language), flagged it default without clearing the existing default
(4,762 files with two), added a second one on the encode path when one was
already there, and — loudnorm resampling to 192 kHz with nothing bringing it
back — wrote every track as AAC at **96 kHz** (4,935 of 4,935). Its failures
were swallowed: ffmpeg failing returned the input unchanged, as a success.
Every one of those tracks is non-compliant for `ensureStereoPerLanguage`,
which drops and rebuilds it; the legacy French title is how it recognises
them.

## Deploying a flow or plugin change

```bash
python3 stack/scripts/tdarr/build_flow_v6.py         # after editing it
python3 stack/scripts/tdarr/deploy_tdarr.py          # dry run
python3 stack/scripts/tdarr/deploy_tdarr.py --apply
python3 stack/scripts/tdarr/requeue_outdated.py --limit 20 --apply   # canary
python3 stack/scripts/tdarr/requeue_outdated.py --apply              # the rest
```

`deploy_tdarr.py` talks to the server API (`/api/v2/cruddb`,
`/api/v2/alter-worker-limit`), never to its SQLite file: the server keeps
flows, libraries and nodes in memory, and a direct write is ignored or
overwritten. It upserts the flow, points the production libraries at it,
removes `SAMPLE*` test libraries and every flow no library uses, and sets
`NODE_LIMITS`. Tdarr's own daily backups are under
`tdarr/server/Tdarr/Backups/`.

**A flow change re-evaluates nothing by itself.** Tdarr keeps each file's
verdict; 102 files above the bitrate guard and 10 AV1 files stayed untouched
for weeks because they were processed before the guard existed. Always run
`requeue_outdated.py` after deploying: it requeues the production libraries,
files due for re-encoding first, and a file already as required only costs a
health check.

Local plugins reach the nodes through `nodePlugins.Zip`, which the server
rebuilds hourly. If its date stops moving, look for `Error zipping plugins
folder` in the server log: the server (PUID) must be able to read the whole
`Plugins` tree. Directories laid by the old `copytree`-based install were
root-owned 0750 and blocked it from 2026-08-28 to 2026-10-07; `safe_copy.py`
lays new ones 0755 but, by design, never re-stamps an existing directory.

## Worker limits

`NvencNode` runs 2 GPU transcode workers, matching its `cpus: 2`; it ran 5,
mostly full-file remuxes that only added an audio track. `MyInternalNode`
follows its hourly schedule (scheduler on). Limits live in the server
database and survive restarts; change them through `deploy_tdarr.py`
(`NODE_LIMITS`) so the repository stays the reference.
