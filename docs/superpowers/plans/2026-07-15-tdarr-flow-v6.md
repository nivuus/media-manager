# Flow Tdarr v6 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remplacer le flow Tdarr unique par `OLED4K_norm_v6` qui uniformise tous les fichiers (MKV/HEVC Main10), préserve les bonus (Dolby Vision via pipeline `dovi_tool`, HDR10/HLG, Atmos), normalise le son, encode en QSV avec fallback CPU→copy, réduit la taille en qualité-d'abord, sans jamais casser un fichier.

**Architecture :** Un flow v6 (nouvel `id` en base SQLite) assemblé par un script Python générateur. Une branche Dolby Vision est déportée dans un flow plugin Local custom (`dvReencodeQsv`) qui orchestre extract-rpu → QSV encode → inject-rpu → mkvmerge → validation, avec retombée automatique sur `copy` en cas d'anomalie. Les binaires `dovi_tool`/`mkvmerge` sont montés en volume read-only (image officielle intacte → Watchtower préservé).

**Tech Stack :** Tdarr flows (Node.js flow plugins, JS compilé), SQLite3 (WAL), Python 3 + `python-dotenv`, Docker Compose, FFmpeg 7 QSV (Intel `/dev/dri`), `dovi_tool`, `mkvtoolnix`/`mkvmerge`.

## Global Constraints

- Base SQLite : `tdarr/server/Tdarr/DB2/SQL/database.db`, table `flowsjsondb(id TEXT, timestamp INTEGER, json_data TEXT)`, table `librarysettingsjsondb(id, timestamp, json_data)`. **Toujours** `docker compose stop tdarr tdarr-node` avant édition DB (WAL actif), backup préalable de `database.db`.
- Flow v5 `OLED4K_norm_v5` : **ne jamais le modifier ni le supprimer** (rollback).
- ICQ QSV (variable de flow `quality`) : **SD 22 / 720p 21 / 1080p 20 / 4K 19**.
- Audio (branches d'encodage) : `-map 0:a:0 -map 0:v:0 -map 0:a -c:a copy -c:a:0 aac -ac:0 2 -b:a:0 256k -filter:a:0 loudnorm=I=-16:TP=-1.5:LRA=11 -map 0:s? -c:s copy -max_muxing_queue_size 9999 -vsync 0 -disposition:a:0 0 -disposition:a:1 default`.
- Encodeur : `hevc_qsv` (hardware), `-profile:v main10`. Fallback CPU : `libx265 -preset medium -profile:v main10`.
- Passthrough HDR obligatoire **uniquement** sur les branches HDR : `-color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc`. **Jamais** sur la branche SDR (sinon SDR mal taggé en HDR).
- Garde-fous taille : sortie `> 100 %` source → garder original ; `< 5 %` source → garder original.
- Invariant : **aucune branche ne laisse un fichier en erreur ou supprimé**. Pire cas = original conservé.
- Ne pas retirer les labels `com.centurylinklabs.watchtower.enable: true`. Ne pas modifier l'image officielle.
- Binaires montés : `./tdarr/bin:/app/bin:ro` sur le service **tdarr-node**.
- Plugin flow Local : `tdarr/server/Tdarr/Plugins/FlowPlugins/LocalFlowPlugins/video/dvReencodeQsv/1.0.0/index.js`, référencé dans le flow avec `sourceRepo: "Local"`, `pluginName: "dvReencodeQsv"`.
- Résolution native préservée (pas de downscale). SDR encodé en Main10.

---

## File Structure

- Create: `tdarr/bin/fetch-binaries.sh` — télécharge `dovi_tool` (statique) + `mkvmerge` portable dans `tdarr/bin/`.
- Create (artefacts, git-ignored): `tdarr/bin/dovi_tool`, `tdarr/bin/mkvmerge`.
- Modify: `docker-compose.yml` — volume `./tdarr/bin:/app/bin:ro` sur `tdarr-node`.
- Create: `tdarr/server/Tdarr/Plugins/FlowPlugins/LocalFlowPlugins/video/dvReencodeQsv/1.0.0/index.js` — plugin DV.
- Create: `scripts/tdarr/dv_reencode_qsv.logic.mjs` + `scripts/tdarr/dv_reencode_qsv.test.mjs` — logique de décision testée.
- Create: `scripts/tdarr/build_flow_v6.py` — assemble le JSON du flow v6 → `tdarr/flows/OLED4K_norm_v6.json`.
- Create (artefact): `tdarr/flows/OLED4K_norm_v6.json`.
- Create: `scripts/tdarr/deploy_flow_v6.py` — backup DB, insère v6, réassigne Movies/TV Shows, isole les samples, supprime `DELETED_JUNK`. `--dry-run` par défaut.
- Create: `scripts/tdarr/validate_output.py` — inspecte un fichier de sortie (ffprobe/dovi_tool).

---

## Task 1: Infra — binaires dovi_tool + mkvmerge montés dans tdarr-node

**Files:**
- Create: `tdarr/bin/fetch-binaries.sh`
- Modify: `docker-compose.yml` (service `tdarr-node`, section `volumes:`)
- Test: exécution en conteneur (`docker compose exec tdarr-node`)

**Interfaces:**
- Produces: binaires exécutables à `/app/bin/dovi_tool` et `/app/bin/mkvmerge` dans le conteneur `tdarr-node`. Les tâches suivantes (plugin, validation) s'appuient sur ces chemins absolus.

- [ ] **Step 1: Écrire le script de récupération des binaires**

Create `tdarr/bin/fetch-binaries.sh` :

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

DOVI_VER="2.1.3"
# dovi_tool : binaire statique musl x86_64 (releases GitHub officielles)
curl -fsSL -o dovi_tool.tar.gz \
  "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_VER}/dovi_tool-${DOVI_VER}-x86_64-unknown-linux-musl.tar.gz"
tar xzf dovi_tool.tar.gz dovi_tool
rm -f dovi_tool.tar.gz
chmod +x dovi_tool

# mkvmerge : extrait de l'AppImage mkvtoolnix + libs à côté du binaire
MKV_APPIMAGE="https://mkvtoolnix.download/appimage/MKVToolNix_GUI-92.0-x86_64.AppImage"
curl -fsSL -o mkvtoolnix.AppImage "${MKV_APPIMAGE}"
chmod +x mkvtoolnix.AppImage
./mkvtoolnix.AppImage --appimage-extract >/dev/null
cp squashfs-root/usr/bin/mkvmerge ./mkvmerge
mkdir -p lib
cp -a squashfs-root/usr/lib/. lib/ 2>/dev/null || true
rm -rf squashfs-root mkvtoolnix.AppImage
chmod +x mkvmerge

echo "OK: $(./dovi_tool --version 2>&1 | head -1)"
```

- [ ] **Step 2: Exécuter le script sur l'hôte et vérifier les binaires**

Run:
```bash
cd /opt/nivuus/MediaManager && bash tdarr/bin/fetch-binaries.sh && ls -la tdarr/bin/
```
Expected: présence de `dovi_tool` et `mkvmerge` exécutables ; ligne `OK: dovi_tool 2.1.3` (ou version proche).

- [ ] **Step 3: Ajouter le volume read-only dans docker-compose (service tdarr-node)**

Modify `docker-compose.yml`, dans le bloc `volumes:` du service `tdarr-node` (à côté de `- ./tdarr/configs:/app/configs`), ajouter :

```yaml
      - ./tdarr/bin:/app/bin:ro
```

- [ ] **Step 4: Ignorer les binaires dans git**

Append à `.gitignore` (créer le fichier s'il n'existe pas) :

```
tdarr/bin/dovi_tool
tdarr/bin/mkvmerge
tdarr/bin/lib/
```

- [ ] **Step 5: Recréer le conteneur node et vérifier l'exécution des binaires dedans**

Run:
```bash
cd /opt/nivuus/MediaManager && docker compose up -d tdarr-node
docker compose exec tdarr-node sh -lc 'LD_LIBRARY_PATH=/app/bin/lib /app/bin/dovi_tool --version && LD_LIBRARY_PATH=/app/bin/lib /app/bin/mkvmerge --version'
```
Expected: versions affichées sans erreur `not found`/`No such file`. Si `mkvmerge` échoue sur une lib, noter la lib manquante (Step 6).

- [ ] **Step 6: Si mkvmerge manque une lib système, la copier dans tdarr/bin/lib/**

Run:
```bash
cd /opt/nivuus/MediaManager
docker compose exec tdarr-node sh -lc 'LD_LIBRARY_PATH=/app/bin/lib ldd /app/bin/mkvmerge | grep "not found"' || true
```
Pour chaque lib « not found », la déposer dans `tdarr/bin/lib/` (depuis l'AppImage extrait ou l'hôte), puis re-run Step 5 jusqu'à succès.

- [ ] **Step 7: Commit**

```bash
git add tdarr/bin/fetch-binaries.sh docker-compose.yml .gitignore
git commit -m "feat(tdarr): monter dovi_tool + mkvmerge en volume ro dans tdarr-node"
```

---

## Task 2: Flow plugin Local `dvReencodeQsv` (pipeline DV + fallback copy)

**Files:**
- Create: `tdarr/server/Tdarr/Plugins/FlowPlugins/LocalFlowPlugins/video/dvReencodeQsv/1.0.0/index.js`
- Create: `scripts/tdarr/dv_reencode_qsv.logic.mjs`
- Test: `scripts/tdarr/dv_reencode_qsv.test.mjs`

**Interfaces:**
- Consumes: `args.inputFileObj.ffProbeData.streams`, `args.inputFileObj._id` (chemin source), `args.workDir` (cache Tdarr), `args.jobLog`, `args.variables.user.quality` (Task 3), `args.deps.fsextra`, helper `require('../../../../FlowHelpers/1.0.0/cliUtils').CLI`. Binaires `/app/bin/dovi_tool`, `/app/bin/mkvmerge` (Task 1).
- Produces: plugin flow exporté `details`/`plugin`. **Deux sorties** : `outputNumber: 1` (ré-encode DV réussi → `outputFileObj` pointe le MKV final) et `outputNumber: 2` (fallback → router vers la branche HDR du flow, `outputFileObj` = source inchangée). `pluginName: "dvReencodeQsv"`, `sourceRepo: "Local"`. Fonctions pures `detectDvProfile(ffProbeData) -> {profile, blCompat}` et `decideAction({profile}) -> 'reencode'|'convert-reencode'|'copy'`.

- [ ] **Step 1: Écrire le test de la logique de décision (profil → action)**

Create `scripts/tdarr/dv_reencode_qsv.test.mjs` :

```js
import assert from 'node:assert';
import test from 'node:test';
import { detectDvProfile, decideAction } from './dv_reencode_qsv.logic.mjs';

const withProfile = (dv_profile, extra = {}) => ({
  streams: [{ codec_type: 'video',
    side_data_list: [{ side_data_type: 'DOVI configuration record', dv_profile, ...extra }] }],
});

test('profil 8 -> reencode', () => {
  assert.equal(decideAction(detectDvProfile(withProfile(8, { dv_bl_signal_compatibility_id: 1 }))), 'reencode');
});
test('profil 7 -> convert-reencode', () => {
  assert.equal(decideAction(detectDvProfile(withProfile(7))), 'convert-reencode');
});
test('profil 5 -> copy', () => {
  assert.equal(decideAction(detectDvProfile(withProfile(5))), 'copy');
});
test('pas de DV -> copy', () => {
  assert.equal(decideAction(detectDvProfile({ streams: [{ codec_type: 'video' }] })), 'copy');
});
```

- [ ] **Step 2: Lancer le test, vérifier qu'il échoue**

Run: `cd /opt/nivuus/MediaManager && node --test scripts/tdarr/dv_reencode_qsv.test.mjs`
Expected: FAIL — `Cannot find module '.../dv_reencode_qsv.logic.mjs'`.

- [ ] **Step 3: Écrire la logique pure et relancer**

Create `scripts/tdarr/dv_reencode_qsv.logic.mjs` :

```js
export function detectDvProfile(ffProbeData) {
  const streams = (ffProbeData && ffProbeData.streams) || [];
  for (const s of streams) {
    if (s.codec_type !== 'video') continue;
    const rec = (s.side_data_list || []).find(
      (d) => d.side_data_type === 'DOVI configuration record');
    if (rec) return { profile: rec.dv_profile, blCompat: rec.dv_bl_signal_compatibility_id };
  }
  return { profile: null, blCompat: null };
}

export function decideAction(dv) {
  if (dv.profile === 8) return 'reencode';
  if (dv.profile === 7) return 'convert-reencode';
  return 'copy'; // profil 5/4, ou pas de DV -> copy sûr
}
```

Run: `node --test scripts/tdarr/dv_reencode_qsv.test.mjs`
Expected: PASS (4 tests).

- [ ] **Step 4: Écrire le plugin flow `index.js` (recopie à l'identique de la logique testée)**

Create `tdarr/server/Tdarr/Plugins/FlowPlugins/LocalFlowPlugins/video/dvReencodeQsv/1.0.0/index.js` :

```js
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
const path = require('path');
const { CLI } = require('../../../../FlowHelpers/1.0.0/cliUtils');

const DOVI = '/app/bin/dovi_tool';
const MKVMERGE = '/app/bin/mkvmerge';
const LIBENV = Object.assign({}, process.env, { LD_LIBRARY_PATH: '/app/bin/lib' });

// --- logique pure : COPIE À L'IDENTIQUE de scripts/tdarr/dv_reencode_qsv.logic.mjs (testée) ---
function detectDvProfile(ffProbeData) {
  const streams = (ffProbeData && ffProbeData.streams) || [];
  for (const s of streams) {
    if (s.codec_type !== 'video') continue;
    const rec = (s.side_data_list || []).find(
      (d) => d.side_data_type === 'DOVI configuration record');
    if (rec) return { profile: rec.dv_profile, blCompat: rec.dv_bl_signal_compatibility_id };
  }
  return { profile: null, blCompat: null };
}
function decideAction(dv) {
  if (dv.profile === 8) return 'reencode';
  if (dv.profile === 7) return 'convert-reencode';
  return 'copy';
}
// -----------------------------------------------------------------------------------------

const details = () => ({
  name: 'DV Re-encode QSV (RPU preserve)',
  description: 'Ré-encode Dolby Vision en QSV avec réinjection RPU. Fallback copy si échec.',
  style: { borderColor: 'purple' },
  tags: 'video',
  isStartPlugin: false,
  pType: '',
  requiresVersion: '2.11.01',
  sidebarPosition: -1,
  icon: 'faFilm',
  inputs: [],
  outputs: [
    { number: 1, tooltip: 'Ré-encode DV réussi (fichier remplacé)' },
    { number: 2, tooltip: 'Fallback -> router vers branche HDR (re-encode HDR10)' },
  ],
});
exports.details = details;

const plugin = async (args) => {
  const lib = require('../../../../../methods/lib')();
  args.inputs = lib.loadDefaultValues(args.inputs, details);
  const jobLog = args.jobLog || (() => {});
  const src = args.inputFileObj._id;
  const work = args.workDir;
  const quality = String((args.variables && args.variables.user && args.variables.user.quality) || 19);

  const fallback = (msg) => {
    jobLog(`DV fallback -> HDR re-encode: ${msg}`);
    return { outputFileObj: args.inputFileObj, outputNumber: 2, variables: args.variables };
  };

  const run = async (cliPath, spawnArgs, outFile) => {
    const cli = new CLI({
      cli: cliPath, spawnArgs, spawnOpts: { env: LIBENV },
      jobLog, outputFilePath: outFile, inputFileObj: args.inputFileObj,
      logFullCliOutput: true, updateWorker: args.updateWorker,
    });
    const res = await cli.runCli();
    if (res.cliExitCode !== 0) throw new Error(`${cliPath} exit ${res.cliExitCode}`);
    return res;
  };

  try {
    const dv = detectDvProfile(args.inputFileObj.ffProbeData);
    const action = decideAction(dv);
    jobLog(`DV profile=${dv.profile} action=${action}`);
    if (action === 'copy') return fallback(`profil ${dv.profile} non ré-encodable ici`);

    const inHevc = path.join(work, 'in.hevc');
    const rpu = path.join(work, 'RPU.bin');
    const baseHevc = path.join(work, 'base.hevc');
    const dvHevc = path.join(work, 'base_dv.hevc');
    const aacTmp = path.join(work, 'aac.mka');
    const outMkv = path.join(work, `${path.parse(src).name}.dv.mkv`);

    // 1. flux HEVC brut + extraction RPU (7 -> 8.1 via -m 2)
    await run('ffmpeg', ['-y', '-i', src, '-map', '0:v:0', '-c', 'copy',
      '-bsf:v', 'hevc_mp4toannexb', '-f', 'hevc', inHevc], inHevc);
    const mode = action === 'convert-reencode' ? ['-m', '2'] : [];
    await run(DOVI, [...mode, 'extract-rpu', inHevc, '-o', rpu], rpu);

    // 2. encode base QSV HEVC Main10, framerate passthrough
    await run('ffmpeg', ['-y', '-i', src, '-map', '0:v:0', '-an', '-sn',
      '-c:v', 'hevc_qsv', '-profile:v', 'main10', '-global_quality', quality,
      '-color_primaries', 'bt2020', '-color_trc', 'smpte2084', '-colorspace', 'bt2020nc',
      '-fps_mode', 'passthrough', '-f', 'hevc', baseHevc], baseHevc);

    // 3. réinjection RPU
    await run(DOVI, ['inject-rpu', '-i', baseHevc, '--rpu-in', rpu, '-o', dvHevc], dvHevc);

    // 4. piste AAC 2.0 normalisée + remux MKV (vidéo DV + toutes pistes originales + AAC)
    await run('ffmpeg', ['-y', '-i', src, '-map', '0:a:0', '-c:a', 'aac', '-ac', '2',
      '-b:a', '256k', '-filter:a', 'loudnorm=I=-16:TP=-1.5:LRA=11', aacTmp], aacTmp);
    await run(MKVMERGE, ['-o', outMkv, dvHevc,
      '--no-video', '--no-attachments', src, aacTmp], outMkv);

    // 5. validation : RPU présent + taille cohérente
    const info = await new CLI({
      cli: DOVI, spawnArgs: ['info', '-i', outMkv, '-f', '0'], spawnOpts: { env: LIBENV },
      jobLog, outputFilePath: outMkv, inputFileObj: args.inputFileObj, logFullCliOutput: true,
    }).runCli();
    if (info.cliExitCode !== 0) return fallback('dovi_tool info échec (RPU absent ?)');

    const fs = args.deps.fsextra;
    const outSize = (await fs.stat(outMkv)).size;
    const inSize = (await fs.stat(src)).size;
    if (outSize > inSize) return fallback(`sortie ${outSize} > source ${inSize}`);
    if (outSize < inSize * 0.05) return fallback('sortie < 5% source (tronquée ?)');

    args.inputFileObj._id = outMkv;
    args.inputFileObj.file = outMkv;
    return { outputFileObj: args.inputFileObj, outputNumber: 1, variables: args.variables };
  } catch (err) {
    return fallback(err && err.message ? err.message : String(err));
  }
};
exports.plugin = plugin;
```

- [ ] **Step 5: Vérifier la syntaxe du plugin**

Run: `cd /opt/nivuus/MediaManager && node --check tdarr/server/Tdarr/Plugins/FlowPlugins/LocalFlowPlugins/video/dvReencodeQsv/1.0.0/index.js && echo SYNTAX_OK`
Expected: `SYNTAX_OK`.

- [ ] **Step 6: Commit**

```bash
git add tdarr/server/Tdarr/Plugins/FlowPlugins/LocalFlowPlugins/video/dvReencodeQsv scripts/tdarr/dv_reencode_qsv.test.mjs scripts/tdarr/dv_reencode_qsv.logic.mjs
git commit -m "feat(tdarr): plugin flow dvReencodeQsv (DV QSV + inject RPU, fallback copy)"
```

---

## Task 3: Générateur du flow v6 (`build_flow_v6.py`)

**Files:**
- Create: `scripts/tdarr/build_flow_v6.py`
- Create (artefact): `tdarr/flows/OLED4K_norm_v6.json`

**Interfaces:**
- Consumes: le plugin `dvReencodeQsv` (Task 2, `sourceRepo="Local"`).
- Produces: `tdarr/flows/OLED4K_norm_v6.json` avec clés `_id`, `name`, `priority`, `flowPlugins` (`{name, sourceRepo, pluginName, version, id, position, fpEnabled, inputsDB}`), `flowEdges` (`{source, sourceHandle, target, id, type, animated}`). `_id = "OLED4K_norm_v6"`.

**Topologie :**
```
Input → HealthCheck →(2)→ Skip
      →(1)→ CheckMedium(video) →(2)→ Skip
           →(1)→ CheckHDR
                ├─(1 HDR)→ CheckDVcandidate(HEVC)
                │            ├─(1 HEVC)→ [plugin dvReencodeQsv] ─(1 OK)→ SizeGuard
                │            │                                   └─(2 fallback)→ chaîne HDR
                │            └─(2 non-HEVC)→ chaîne HDR
                └─(2 SDR)→ chaîne SDR
chaîne = Start→MKV→RemoveData→Resolution→[SD/720/1080/4K quality]→EncQSV→ArgsQSV→ExecQSV
         ExecQSV ─ok→ SizeGuard ; ─err→ EncCPU→ArgsCPU→ExecCPU ─ok→ SizeGuard ; ─err→ KeepOriginal
SizeGuard →(1 trop gros)→ KeepOriginal ; →(2/3 ok)→ Replace→ClearCache→Success
```
La chaîne **HDR** force `-color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc`.
La chaîne **SDR** n'ajoute **aucun** flag couleur (évite le mis-tag). Les deux encodent en Main10.

- [ ] **Step 1: Écrire le générateur de flow**

Create `scripts/tdarr/build_flow_v6.py` :

```python
#!/usr/bin/env python3
"""Génère tdarr/flows/OLED4K_norm_v6.json (flow HDR/DV hybride, chaînes HDR & SDR séparées)."""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "tdarr", "flows", "OLED4K_norm_v6.json")

ENC_AUDIO = ("-map 0:a:0 -map 0:v:0 -map 0:a -c:a copy -c:a:0 aac -ac:0 2 -b:a:0 256k "
             "-filter:a:0 loudnorm=I=-16:TP=-1.5:LRA=11 -map 0:s? -c:s copy "
             "-max_muxing_queue_size 9999 -vsync 0 -disposition:a:0 0 -disposition:a:1 default")

def node(nid, name, plugin, repo="Community", version="1.0.0", inputs=None, x=0, y=0):
    return {"name": name, "sourceRepo": repo, "pluginName": plugin, "version": version,
            "id": nid, "position": {"x": x, "y": y}, "fpEnabled": True, "inputsDB": inputs or {}}

def edge(src, handle, tgt, eid):
    return {"source": src, "sourceHandle": str(handle), "target": tgt, "id": eid,
            "type": "smoothstep", "animated": True}

nodes, edges = [], []
def add(n): nodes.append(n)
def link(s, h, t, i): edges.append(edge(s, h, t, i))

QSV_ENC = {"outputCodec": "hevc", "ffmpegPresetEnabled": False, "ffmpegQualityEnabled": True,
           "ffmpegQuality": "{{args.variables.user.quality}}", "hardwareEncoding": True,
           "hardwareType": "qsv", "hardwareDecoding": False, "forceEncoding": False}
CPU_ENC = {"outputCodec": "hevc", "ffmpegPresetEnabled": True, "ffmpegPreset": "medium",
           "ffmpegQualityEnabled": True, "ffmpegQuality": "{{args.variables.user.quality}}",
           "hardwareEncoding": False, "hardwareType": "auto", "hardwareDecoding": False,
           "forceEncoding": True}
RES_MAP = [(1,"SD"),(2,"SD"),(3,"720"),(4,"1080"),(5,"1080"),(6,"4K"),(7,"4K"),(8,"4K"),(9,"1080")]
QUAL = {"SD":"22","720":"21","1080":"20","4K":"19"}

def encode_chain(p, color, x0):
    """Construit une chaîne QSV + fallback CPU. Retourne l'id du nœud d'entrée."""
    add(node(f"{p}_START", f"Begin QSV {p}", "ffmpegCommandStart", x=x0, y=500))
    add(node(f"{p}_MKV", f"Set MKV {p}", "ffmpegCommandSetContainer",
             inputs={"container": "mkv", "forceConform": True}, x=x0, y=560))
    add(node(f"{p}_RM", f"Remove Data {p}", "ffmpegCommandRemoveDataStreams", x=x0, y=620))
    add(node(f"{p}_RES", f"Check Resolution {p}", "checkVideoResolution", x=x0, y=680))
    for key in ("SD", "720", "1080", "4K"):
        add(node(f"{p}_{key}", f"{key} Q", "setFlowVariable",
                 inputs={"variable": "quality", "value": QUAL[key]}, x=x0, y=740))
    add(node(f"{p}_ENC", f"Enc QSV HEVC 10bit {p}", "ffmpegCommandSetVideoEncoder", inputs=QSV_ENC, x=x0, y=800))
    add(node(f"{p}_ARGS", f"Args QSV {p}", "ffmpegCommandCustomArguments",
             inputs={"inputArguments": "", "outputArguments": color + ENC_AUDIO}, x=x0, y=860))
    add(node(f"{p}_EXEC", f"Exec QSV {p}", "ffmpegCommandExecute", x=x0, y=920))
    add(node(f"{p}_CSTART", f"Begin CPU {p}", "ffmpegCommandStart", x=x0+300, y=920))
    add(node(f"{p}_CMKV", f"Set MKV CPU {p}", "ffmpegCommandSetContainer",
             inputs={"container": "mkv", "forceConform": True}, x=x0+300, y=980))
    add(node(f"{p}_CRM", f"Remove Data CPU {p}", "ffmpegCommandRemoveDataStreams", x=x0+300, y=1040))
    add(node(f"{p}_CENC", f"Enc CPU libx265 {p}", "ffmpegCommandSetVideoEncoder", inputs=CPU_ENC, x=x0+300, y=1100))
    add(node(f"{p}_CARGS", f"Args CPU {p}", "ffmpegCommandCustomArguments",
             inputs={"inputArguments": "", "outputArguments": color + "-profile:v main10 " + ENC_AUDIO}, x=x0+300, y=1160))
    add(node(f"{p}_CEXEC", f"Exec CPU {p}", "ffmpegCommandExecute", x=x0+300, y=1220))
    # câblage QSV
    link(f"{p}_START", 1, f"{p}_MKV", f"{p}_e1"); link(f"{p}_MKV", 1, f"{p}_RM", f"{p}_e2")
    link(f"{p}_RM", 1, f"{p}_RES", f"{p}_e3")
    for h, key in RES_MAP:
        link(f"{p}_RES", h, f"{p}_{key}", f"{p}_er{h}")
    for key in ("SD", "720", "1080", "4K"):
        link(f"{p}_{key}", 1, f"{p}_ENC", f"{p}_eq{key}")
    link(f"{p}_ENC", 1, f"{p}_ARGS", f"{p}_e4"); link(f"{p}_ARGS", 1, f"{p}_EXEC", f"{p}_e5")
    link(f"{p}_EXEC", 1, "V6_SIZE", f"{p}_e6"); link(f"{p}_EXEC", "err1", f"{p}_CSTART", f"{p}_eerr")
    # câblage CPU
    link(f"{p}_CSTART", 1, f"{p}_CMKV", f"{p}_c1"); link(f"{p}_CMKV", 1, f"{p}_CRM", f"{p}_c2")
    link(f"{p}_CRM", 1, f"{p}_CENC", f"{p}_c3"); link(f"{p}_CENC", 1, f"{p}_CARGS", f"{p}_c4")
    link(f"{p}_CARGS", 1, f"{p}_CEXEC", f"{p}_c5")
    link(f"{p}_CEXEC", 1, "V6_SIZE", f"{p}_c6"); link(f"{p}_CEXEC", "err1", "V6_KEEP", f"{p}_cerr")
    return f"{p}_START"

# --- entrée + aiguillage ---
add(node("V6_IN", "Input File", "inputFile", x=500, y=0))
add(node("V6_HC", "Run Health Check", "runHealthCheck", x=500, y=100))
add(node("V6_MED", "Check File Medium", "checkFileMedium", inputs={"medium": "video"}, x=500, y=200))
add(node("V6_SKIP", "Skip", "comment", inputs={"comment": "Fichier laissé tel quel"}, x=1200, y=200))
add(node("V6_HDR", "Check HDR Video", "checkHdr", x=500, y=300))
add(node("V6_DVCHK", "Check DV candidate (HEVC)", "checkVideoCodec", inputs={"codec": "hevc"}, x=250, y=400))
add(node("V6_DV", "DV Re-encode QSV", "dvReencodeQsv", repo="Local", x=100, y=500))
# --- post-traitement commun ---
add(node("V6_SIZE", "Check File Size Ratio", "compareFileSizeRatio",
         inputs={"greaterThan": 100, "lessThan": 5}, x=1000, y=1320))
add(node("V6_KEEP", "Keep Original", "comment",
         inputs={"comment": "Ré-encode non bénéfique/suspect -> original conservé"}, x=1300, y=1320))
add(node("V6_REPL", "Replace Original File", "replaceOriginalFile", x=1000, y=1380))
add(node("V6_CLR", "Clear Cache", "clearCache", x=1000, y=1440))
add(node("V6_OK", "Success", "comment",
         inputs={"comment": "HEVC Main10 + Atmos copy + AAC norm. HDR/DV préservé."}, x=1000, y=1500))
# --- chaînes d'encodage ---
hdr = encode_chain("H", "-color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc ", 700)
sdr = encode_chain("S", "", 1600)
# --- routage ---
link("V6_IN", 1, "V6_HC", "e_in")
link("V6_HC", 1, "V6_MED", "e_hc1"); link("V6_HC", 2, "V6_SKIP", "e_hc2")
link("V6_MED", 1, "V6_HDR", "e_med1"); link("V6_MED", 2, "V6_SKIP", "e_med2")
link("V6_HDR", 1, "V6_DVCHK", "e_hdr1"); link("V6_HDR", 2, sdr, "e_hdr2")
link("V6_DVCHK", 1, "V6_DV", "e_dv1"); link("V6_DVCHK", 2, hdr, "e_dv2")
link("V6_DV", 1, "V6_SIZE", "e_dvok"); link("V6_DV", 2, hdr, "e_dvfb")
link("V6_SIZE", 1, "V6_KEEP", "e_s1"); link("V6_SIZE", 2, "V6_REPL", "e_s2"); link("V6_SIZE", 3, "V6_REPL", "e_s3")
link("V6_REPL", 1, "V6_CLR", "e_r1"); link("V6_REPL", 2, "V6_KEEP", "e_r2")
link("V6_CLR", 1, "V6_OK", "e_cl1")

flow = {"_id": "OLED4K_norm_v6", "name": "OLED4K norm v6 - HDR/DV hybride QSV [CANONICAL]",
        "priority": 1, "flowPlugins": nodes, "flowEdges": edges}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(flow, f, indent=1)
print(f"OK {len(nodes)} nœuds, {len(edges)} arêtes -> {OUT}")
```

- [ ] **Step 2: Générer le flow et valider le JSON**

Run:
```bash
cd /opt/nivuus/MediaManager && python3 scripts/tdarr/build_flow_v6.py && python3 -c "import json;d=json.load(open('tdarr/flows/OLED4K_norm_v6.json'));print('nodes',len(d['flowPlugins']),'edges',len(d['flowEdges']),'id',d['_id'])"
```
Expected: `OK ... nœuds ...` puis `nodes 40+ edges 45+ id OLED4K_norm_v6`.

- [ ] **Step 3: Vérifier l'intégrité du graphe (pas d'arête orpheline, ids uniques)**

Run:
```bash
cd /opt/nivuus/MediaManager && python3 -c "
import json
d=json.load(open('tdarr/flows/OLED4K_norm_v6.json'))
ids=[n['id'] for n in d['flowPlugins']]
assert len(ids)==len(set(ids)), 'ids dupliqués'
S=set(ids)
bad=[e for e in d['flowEdges'] if e['source'] not in S or e['target'] not in S]
assert not bad, bad
eids=[e['id'] for e in d['flowEdges']]
assert len(eids)==len(set(eids)), 'edge ids dupliqués'
print('graphe OK :', len(ids), 'nœuds,', len(eids), 'arêtes, tous référencés')
"
```
Expected: `graphe OK : ...` (aucune assertion levée).

- [ ] **Step 4: Commit**

```bash
git add scripts/tdarr/build_flow_v6.py tdarr/flows/OLED4K_norm_v6.json
git commit -m "feat(tdarr): générateur flow v6 (chaînes HDR & SDR séparées, DV plugin, fallback)"
```

---

## Task 4: Script de déploiement (`deploy_flow_v6.py`)

**Files:**
- Create: `scripts/tdarr/deploy_flow_v6.py`

**Interfaces:**
- Consumes: `tdarr/flows/OLED4K_norm_v6.json` (Task 3), base SQLite (Global Constraints).
- Produces: flow v6 dans `flowsjsondb` ; `Movies`/`TV Shows` → `flowId=OLED4K_norm_v6` ; sample-libraries `processLibrary=false` + repointées ; `2Op_kn3UU` supprimé. `--dry-run` par défaut, `--apply` pour écrire.

- [ ] **Step 1: Écrire le script de déploiement**

Create `scripts/tdarr/deploy_flow_v6.py` :

```python
#!/usr/bin/env python3
"""Déploie le flow v6 en base Tdarr. Usage: deploy_flow_v6.py [--apply]  (dry-run par défaut)."""
import json, os, sqlite3, shutil, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "tdarr/server/Tdarr/DB2/SQL/database.db")
FLOW = os.path.join(ROOT, "tdarr/flows/OLED4K_norm_v6.json")
TEST_DIR = "/media/_tdarr_test"
DRY = "--apply" not in sys.argv

def main():
    with open(FLOW) as f:
        flow = json.load(f)
    print("== DRY-RUN (ajouter --apply pour écrire) ==" if DRY else "== APPLY ==")
    if not DRY:
        bak = f"{DB}.bak-{int(time.time())}"
        shutil.copy2(DB, bak); print(f"backup -> {bak}")

    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    cur = con.cursor()
    ts = int(time.time() * 1000)

    print(f"flow upsert: {flow['_id']}")
    if not DRY:
        cur.execute("INSERT INTO flowsjsondb(id,timestamp,json_data) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET timestamp=excluded.timestamp, json_data=excluded.json_data",
                    (flow["_id"], ts, json.dumps(flow)))

    for row in cur.execute("SELECT id,json_data FROM librarysettingsjsondb").fetchall():
        data = json.loads(row["json_data"]); name = data.get("name", ""); changed = False
        if name in ("Movies", "TV Shows"):
            data["flowId"] = "OLED4K_norm_v6"; changed = True
            print(f"  {name}: flowId -> OLED4K_norm_v6")
        elif name.startswith("SAMPLE"):
            data["processLibrary"] = False; data["folder"] = TEST_DIR; changed = True
            print(f"  {name}: processLibrary=false, folder -> {TEST_DIR}")
        if changed and not DRY:
            cur.execute("UPDATE librarysettingsjsondb SET json_data=?,timestamp=? WHERE id=?",
                        (json.dumps(data), ts, row["id"]))

    if cur.execute("SELECT id FROM flowsjsondb WHERE id='2Op_kn3UU'").fetchone():
        print("  suppression flow résidu 2Op_kn3UU (DELETED_JUNK)")
        if not DRY:
            cur.execute("DELETE FROM flowsjsondb WHERE id='2Op_kn3UU'")

    if not DRY:
        con.commit(); print("commit DB OK")
    con.close()

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Dry-run (Tdarr encore up, lecture seule)**

Run: `cd /opt/nivuus/MediaManager && python3 scripts/tdarr/deploy_flow_v6.py`
Expected: liste des changements prévus (flow upsert, Movies/TV Shows → v6, samples isolés, suppression DELETED_JUNK), **sans** écriture ni backup.

- [ ] **Step 3: Arrêter Tdarr, appliquer, redémarrer**

Run:
```bash
cd /opt/nivuus/MediaManager
docker compose stop tdarr tdarr-node
python3 scripts/tdarr/deploy_flow_v6.py --apply
docker compose up -d
```
Expected: `backup -> ...`, `commit DB OK`, conteneurs remontés.

- [ ] **Step 4: Vérifier en base l'état post-déploiement**

Run:
```bash
cd /opt/nivuus/MediaManager/tdarr/server/Tdarr/DB2/SQL
sqlite3 database.db "SELECT json_extract(json_data,'\$.name'), json_extract(json_data,'\$.flowId'), json_extract(json_data,'\$.processLibrary') FROM librarysettingsjsondb;"
sqlite3 database.db "SELECT id FROM flowsjsondb WHERE id IN ('OLED4K_norm_v6','OLED4K_norm_v5','2Op_kn3UU');"
```
Expected: Movies/TV Shows → `OLED4K_norm_v6` ; samples `processLibrary=0` ; `OLED4K_norm_v6` et `OLED4K_norm_v5` présents, `2Op_kn3UU` absent.

- [ ] **Step 5: Commit**

```bash
cd /opt/nivuus/MediaManager
git add scripts/tdarr/deploy_flow_v6.py
git commit -m "feat(tdarr): script de déploiement flow v6 (backup+upsert+réassignation)"
```

---

## Task 5: Validation sur fichiers de test (go/no-go prod)

**Files:**
- Create: `scripts/tdarr/validate_output.py`

**Interfaces:**
- Consumes: binaires (Task 1), flow v6 déployé (Task 4), un fichier par type dans le dossier de test isolé (`${MEDIA_ROOT}/_tdarr_test`).
- Produces: rapport pass/fail par fichier + décision go/no-go.

- [ ] **Step 1: Écrire le validateur de sortie**

Create `scripts/tdarr/validate_output.py` :

```python
#!/usr/bin/env python3
"""Vérifie un fichier transcodé. Usage: validate_output.py <src> <out>"""
import json, subprocess, sys

def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                          "-show_format", "-show_streams", path], capture_output=True, text=True)
    return json.loads(out.stdout)

def main(src, out):
    ps, po = ffprobe(src), ffprobe(out)
    def vid(p): return next(s for s in p["streams"] if s["codec_type"] == "video")
    def audios(p): return [s for s in p["streams"] if s["codec_type"] == "audio"]
    vo, vs = vid(po), vid(ps)
    ok = True
    if vo["codec_name"] != "hevc":
        ok = False; print("FAIL codec != hevc")
    if vs.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        if vo.get("color_transfer") != vs.get("color_transfer"):
            ok = False; print(f"FAIL HDR transfer perdu ({vo.get('color_transfer')})")
    if len(audios(po)) < len(audios(ps)) + 1:
        print("WARN moins de pistes audio qu'attendu (source + AAC)")
    if not any(a["codec_name"] == "aac" for a in audios(po)):
        ok = False; print("FAIL piste AAC normalisée absente")
    ss, so = int(ps["format"]["size"]), int(po["format"]["size"])
    print(f"taille: {ss/1e9:.2f}G -> {so/1e9:.2f}G ({100*so/ss:.0f}%)")
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

- [ ] **Step 2: Préparer les fichiers de test (1 par type) dans le dossier isolé**

Copier dans `${MEDIA_ROOT}/_tdarr_test/` : 1 DV profil 8.1, 1 DV profil 7, 1 HDR10, 1 SDR non-HEVC. Identifier leur type :
```bash
for f in "${MEDIA_ROOT}"/_tdarr_test/*; do
  echo "== $f"
  ffprobe -v quiet -show_streams "$f" | grep -Ei 'color_transfer|dv_profile|DOVI' || echo "SDR/aucun"
done
```
Expected: chaque type identifiable (DOVI record + dv_profile pour DV, smpte2084 pour HDR10).

- [ ] **Step 3: Créer une library de test Tdarr sur le dossier isolé et lancer le flow v6**

Dans l'UI Tdarr (`http://<host>:8265`) : créer une library pointant `${MEDIA_ROOT}/_tdarr_test`, lui assigner le flow `OLED4K_norm_v6`, lancer un scan. Observer les jobs.
Expected: chaque fichier traité ; pour les DV, log du plugin `DV profile=... action=...` ; aucun job en échec bloquant. **Itérer le plugin (Task 2) si nécessaire** jusqu'à ce que la sortie DV soit correcte, en gardant l'invariant fallback→copy.

- [ ] **Step 4: Valider chaque sortie**

Run (pour chaque paire source/sortie) :
```bash
cd /opt/nivuus/MediaManager
python3 scripts/tdarr/validate_output.py "<source>" "<sortie>"
# pour les DV réussis, confirmer le RPU :
LD_LIBRARY_PATH=tdarr/bin/lib tdarr/bin/dovi_tool info -i "<sortie_DV>" -f 0 | grep -i "profile"
```
Expected: `RESULT: PASS` pour chaque fichier ; pour DV réussi, `dovi_tool info` montre le profil 8.1 ; pour DV en fallback, la sortie est un ré-encode HDR10 (transfer smpte2084 conservé) lisible.

- [ ] **Step 5: Décision go/no-go + commit du validateur**

Si tous les types passent → prod déjà active (Movies/TV Shows sur v6). Sinon, **rollback** :
```bash
cd /opt/nivuus/MediaManager
docker compose stop tdarr tdarr-node
sqlite3 tdarr/server/Tdarr/DB2/SQL/database.db "UPDATE librarysettingsjsondb SET json_data=json_set(json_data,'\$.flowId','OLED4K_norm_v5') WHERE json_extract(json_data,'\$.name') IN ('Movies','TV Shows');"
docker compose up -d
```
Puis commit du validateur :
```bash
git add scripts/tdarr/validate_output.py
git commit -m "test(tdarr): validateur de sortie (HDR/DV/audio/taille) + validation samples"
```

---

## Notes de risque (à garder en tête pendant l'exécution)

- **HLG hors périmètre du routage actuel** : le plugin Community `checkHdr` ne détecte comme HDR que `color_transfer=smpte2084` (+ tags DV). Le **HLG** (`arib-std-b67`) partirait donc dans la chaîne **SDR** et perdrait son tag. Contenu HLG rare dans une bibliothèque films/séries. Suivi éventuel : insérer un nœud de détection HLG dédié routant vers une 3ᵉ chaîne `encode_chain("L", "-color_primaries bt2020 -color_trc arib-std-b67 -colorspace bt2020nc ", x)`. À décider après la validation Task 5.
- **Plugin DV, API fichier de travail Tdarr** : `CLI`/`workDir`/`outputFileObj` peuvent varier selon la version Tdarr installée. Task 5 Step 3 est le banc d'essai réel ; itérer jusqu'à sortie correcte en conservant l'invariant fallback→copy.
- **mkvmerge portable** : si l'AppImage ne donne pas un `mkvmerge` exploitable, remplacer le remux (Step 4 du plugin) par un remux `ffmpeg` (`-c copy` du flux DV annexb vers MKV) — moins fiable pour le tag DoVi Matroska, re-valider avec `dovi_tool info`.
- **checkVideoResolution handles** : le mapping sorties 1–9 → résolution est repris de v5 ; re-vérifier dans l'UI Tdarr qu'il correspond à la version installée.
- **Profils DV 5/7** : le profil 5 part toujours en fallback (base non HDR10) ; le profil 7 est converti en 8.1 (`-m 2`) — si la conversion échoue, fallback HDR10. C'est conforme à l'invariant « sans erreur ».
```
