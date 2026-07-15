# Design — Flow Tdarr `OLED4K_norm_v6` (uniformisation vidéo + bonus préservés)

Date : 2026-07-15
Statut : validé (design), en attente relecture spec

## 1. Objectif

Refondre le flow Tdarr unique appliqué à toutes les bibliothèques pour :

1. **Uniformiser** les fichiers vidéo (conteneur MKV, codec HEVC Main10, dispositions de pistes propres).
2. **Préserver les bonus** : HDR (Dolby Vision, HDR10/HDR10+, HLG) et audio immersif (Dolby Atmos / TrueHD / DTS-X / DTS-HD).
3. **Normaliser le son** (piste de compatibilité aux normes streaming EBU R128).
4. **Maximiser l'encodage hardware** (Intel QSV), CPU réservé au fallback.
5. **Réduire la taille** en **maximisant la qualité** perçue pour du streaming.
6. **Garantir que tout fichier passe sans erreur et ressort uniformisé** — le pire cas est toujours « original conservé », jamais un fichier cassé ou bloqué.

## 2. État actuel (point de départ)

- Un flow unique `OLED4K_norm_v5` (« OLED4K norm v5 - HEVC Main10 + AudioDual + HDR [CANONICAL] »), 32 plugins, assigné aux **5 libraries** : `Movies`, `TV Shows`, et 3 sample-libraries de test (`Megalopolis`, `Spider-Man DV`, `Interstellar HDR10`, dont une nommée « DO NOT PROCESS IN PROD »).
- Stockage : SQLite `tdarr/server/Tdarr/DB2/SQL/database.db`, table `flowsjsondb` (colonnes `id`, `timestamp`, `json_data`), table `librarysettingsjsondb`.
- Comportement v5 : si déjà HEVC → `c:v copy` (préserve DV/HDR mais **ne réduit jamais** la vidéo) ; sinon → ré-encode QSV. Audio : copie de toutes les pistes + AAC 2.0 normalisée. Garde-fou taille (>200 % → skip). Fallback QSV → CPU libx265.
- Résidu à nettoyer : flow `2Op_kn3UU` = `DELETED_JUNK` (66 octets).

## 3. Décisions de conception (arbitrages validés)

| Sujet | Décision |
|---|---|
| Fichiers HEVC + bonus | **Hybride intelligent** : DV traité à part, HDR10/SDR HEVC ré-encodés QSV, non-HEVC ré-encodés. Skip si le ré-encode grossit le fichier. |
| Dolby Vision | **Pipeline `dovi_tool` (HW + réinjection RPU)** avec **fallback copy obligatoire** en cas d'échec. |
| Audio | **Garder l'Atmos** (copie bit-perfect de toutes les pistes originales) **+ AAC 2.0 normalisée** ajoutée. |
| Normalisation | `loudnorm=I=-16:TP=-1.5:LRA=11` sur la piste AAC de compatibilité uniquement (l'Atmos ne survit qu'à la copie). |
| Cible qualité/taille | **« Qualité d'abord »** — QSV ICQ : **4K 19 / 1080p 20 / 720p 21 / SD 22**. |
| Résolution | **Pas de downscale** — résolution native préservée (4K reste 4K). |
| Encodage HW | **QSV** partout ; CPU libx265 uniquement en fallback. |
| Déploiement | **Nouveau flow `OLED4K_norm_v6`**, réassigner Movies + TV Shows, garder v5 en rollback. |
| Binaires DV | **Volume monté read-only** (`./tdarr/bin/dovi_tool`, `./tdarr/bin/mkvmerge`) — image officielle intacte, Watchtower préservé. |
| Sample-libraries | **Repointées vers un dossier de test isolé**, `processLibrary=false`. |

## 4. Architecture du flow v6

### 4.1 Aiguillage d'entrée

```
Input File
  → Run Health Check ─(échec)──▶ garde original (log), fin
  → Check medium = video ─(non)─▶ garde original, fin
  → Détection HDR/DV (ffprobe side_data + color params)
      ├─ Dolby Vision  → branche DV (§4.2)
      ├─ HDR10 / HDR10+→ branche HDR10 (§4.3)
      ├─ HLG           → branche HLG (§4.3, transfer=arib-std-b67)
      └─ SDR           → branche SDR (§4.3, Main10 anti-banding)
```

### 4.2 Branche Dolby Vision — plugin custom isolé

Plugin Local unique et autonome : `Tdarr/Plugins/Local/dvReencodeQSV.js`.
Responsabilité unique : ré-encoder la vidéo DV en QSV tout en réinjectant le RPU, ou échouer proprement vers `copy`.

Pipeline interne :

```
1. ffprobe → profil DV (DOVI configuration record)
2. Extraction RPU selon profil :
     • 8.1 → dovi_tool extract-rpu
     • 7   → dovi_tool -m 2 (convert dual-layer → 8.1) puis extract-rpu
     • 5   → PAS de ré-encode → fallback copy (base non HDR10)
3. QSV encode base layer HEVC Main10 (ICQ cible §3), -fps_mode passthrough
4. dovi_tool inject-rpu → réinjecte le RPU dans le flux encodé
5. mkvmerge → remux (vidéo DV + audio §4.4 + subs), tag DoVi
6. Validation : nb frames source == encodées == nb RPU
                + dovi_tool info confirme RPU présent + output lisible
```

**Filet de sécurité (non négociable) :** l'ensemble du plugin est en `try/catch`. Toute défaillance (extraction, encode, injection, validation frame-count, remux) **ou une sortie ≥ 100 % de la source** → route vers la **branche `copy`** (DV intact, MKV + audio normalisé, pas de réduction vidéo). Profils 5 et 7-non-convertibles → copy direct.

### 4.3 Branches HDR10 / HLG / SDR — QSV HEVC Main10

Toutes passent par le même squelette d'encode (variable `quality` = ICQ pilotée par la résolution) :

```
Begin FFmpeg QSV
  → Set Container MKV (forceConform)
  → Remove Data Streams
  → Set Encoder QSV HEVC Main10 (hardware=qsv, ICQ = {{quality}})
  → Custom Args (passthrough HDR + audio §4.4)
  → Execute QSV ──échec──▶ Fallback CPU libx265 (§4.5)
```

Passthrough couleur obligatoire (sinon HDR perdu) :

- **HDR10 / HDR10+** : `-color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc` + injection `master-display` (mastering display) + `max-cll` (MaxCLL/MaxFALL) extraits de la source. HDR10+ dynamique → non préservé par QSV → **fallback HDR10 base** (métadonnées statiques conservées).
- **HLG** : idem bt2020 mais `-color_trc arib-std-b67`.
- **SDR** : encodé en **Main10** (10-bit) pour éviter le banding sur OLED ; pas de métadonnée HDR.

Réglage ICQ par résolution (variable `quality`) :

| Résolution | ICQ |
|---|---|
| SD | 22 |
| 720p | 21 |
| 1080p | 20 |
| 4K (2160p) | 19 |

### 4.4 Audio (commun à toutes les branches)

```
-map 0:v:0                                   # vidéo (copy DV, ou QSV/CPU réencodée)
-map 0:a  -c:a copy                          # TOUTES les pistes originales bit-perfect
                                             #   → Atmos / TrueHD / DTS-X / DTS-HD intacts
-c:a:0 aac -ac:0 2 -b:a:0 256k               # piste AAC 2.0 de compatibilité (depuis a:0)
-filter:a:0 loudnorm=I=-16:TP=-1.5:LRA=11    # normalisée EBU R128 streaming
-map 0:s? -c:s copy                          # sous-titres copiés
-disposition:a:0 default                     # AAC normalisée = default
-disposition (pistes originales) : non-default
-max_muxing_queue_size 9999
```

### 4.5 Robustesse — « tout passe sans erreur & uniformisé »

Chaîne de repli à chaque encode :

```
Encode QSV ──échec──▶ CPU libx265 (même ICQ cible, -profile:v main10, preset medium)
                          └──échec──▶ COPY original (jamais de fichier cassé)
```

Garde-fous taille :

- Sortie **> 100 %** de la source (ré-encode contre-productif, source déjà optimale) → garder l'original.
- Sortie **< 5 %** de la source (encode probablement tronqué) → garder l'original.

Invariant : **aucune branche ne laisse un fichier en erreur ni ne le supprime**. Le pire cas est toujours « original conservé (au minimum remuxé/uniformisé) ».

## 5. Infrastructure

### 5.1 Binaires DV (volume monté, image intacte)

- `./tdarr/bin/dovi_tool` — binaire Rust **statique** (téléchargé depuis les releases officielles).
- `./tdarr/bin/mkvmerge` — build portable de mkvtoolnix (remux + tag DoVi Matroska).
- Montés **read-only** dans le service **tdarr-node** (c'est lui qui encode) :
  ```yaml
  volumes:
    - ./tdarr/bin:/app/bin:ro
  ```
- L'image officielle `ghcr.io/haveagitgat/tdarr` n'est pas modifiée ⇒ **Watchtower continue les auto-updates**.

### 5.2 Plugin

- `./tdarr/server/Tdarr/Plugins/Local/dvReencodeQSV.js` (voir §4.2).

## 6. Déploiement

```
1. Backup : copie de database.db avant toute modif.
2. docker compose stop tdarr tdarr-node   (WAL SQLite → édition DB sûre)
3. Insérer le flow OLED4K_norm_v6 dans flowsjsondb.
4. Réassigner librarysettingsjsondb : Movies → v6, TV Shows → v6.
5. Repointer les 3 sample-libraries vers un dossier de test isolé, processLibrary=false.
6. Supprimer le résidu DELETED_JUNK (2Op_kn3UU).
7. Déposer ./tdarr/bin/{dovi_tool,mkvmerge} + volume :ro dans docker-compose (tdarr-node).
8. Déposer le plugin dvReencodeQSV.js.
9. docker compose up -d
```

Rollback : réassigner `Movies` + `TV Shows` sur `OLED4K_norm_v5` (conservé intact).

## 7. Validation post-déploiement

Tester d'abord sur **un fichier de chaque type** via une library de test isolée, avant de lâcher le flow sur toute la bibliothèque :

| Type test | Vérifications |
|---|---|
| DV profil 8.1 | `dovi_tool info` → RPU présent ; taille réduite ; Atmos intact ; AAC norm présente |
| DV profil 7 | conversion 8.1 OK **ou** fallback copy propre ; lecture OK |
| HDR10 | `ffprobe` → bt2020/smpte2084 + master-display présents ; taille réduite |
| SDR non-HEVC | sortie HEVC Main10 ; taille réduite ; audio conservé + normalisé |
| Cas d'échec forcé | fichier corrompu → original conservé, aucune erreur bloquante |

## 8. Hors périmètre (YAGNI)

- Downscale 4K→1080p (résolution native préservée).
- Transcodage/compression des pistes audio lossless (Atmos préservé par copie).
- Préservation du HDR10+ dynamique et du DV profil 5 (fallback HDR10 / copy respectivement).
- Refonte des autres flows présents en base (seuls v5 conservé + v6 créé).
