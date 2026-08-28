# MediaManager en package Nivuus — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sortir la source de MediaManager de `/opt/nivuus/MediaManager` vers un package Nivuus autonome `~/Projects/Nivuus/packages/media-manager`, installable par l'installer sous le contrat `nivuus.dev/v1`, puis basculer la production dessus.

**Architecture:** Le dépôt devient un package : un `nivuus-package.yaml` à la racine, un sous-arbre `stack/` qui **est** le répertoire de déploiement à l'octet près, et deux hooks (`install`, `activate`) qui le déposent puis le démarrent. Trois lignes de crontab deviennent trois timers systemd livrés par le package. La production est renommée en place (`rename()` sur le même système de fichiers, 34 Go, instantané) puis reprise en main par le hook `install`.

**Tech Stack:** Python 3 (stdlib uniquement dans les hooks — ils tournent sur une cible minimale), PyYAML pour les tests, Docker Compose v2, systemd, git.

**Spec:** `docs/superpowers/specs/2026-08-28-package-nivuus-media-manager-design.md`

## Global Constraints

- **`apiVersion: nivuus.dev/v1`**, `name: media-manager`, `version: 1.0.0`, `tier: userspace`.
- **`tier: userspace` interdit** `kernel-cmdline`, `modules` et `hugepages-mib` : le parseur du manifeste **refuse** le fichier, il ne l'ignore pas.
- **Seuls les fichiers suivis par git voyagent** : `installer/iso-build/build.sh` exporte le package par `git archive HEAD`. Un fichier non commité n'existe pas pour l'installer.
- **Les hooks n'importent rien hors stdlib** : ils doivent tourner sur une Debian minimale qui n'a jamais vu ce moteur. Contexte JSON sur **stdin**, jsonl sur **stdout** : `{"event":"progress","pct":N,"msg":"…"}`, `{"event":"done"}`. Sortie non nulle = échec de l'installation.
- **Répertoire de déploiement : `/opt/nivuus/media-manager`** (minuscules).
- **Aucun `claims:`, aucune `requires.capabilities`, aucune `requires.features`** — les dépendances sont déclarées en `apt:`.
- **Style de test du dépôt** : scripts Python autonomes (pas pytest), une liste `failures`, sortie non nulle si elle n'est pas vide, lancés par `make test`. C'est le style de `console/tests/`.
- **Jamais `systemctl enable`** pour armer une unité : symlink dans `<cible>.target.wants/`. `systemctl` échoue silencieusement en environnement contraint ; un symlink existe ou lève.
- **Le `.env` n'est jamais écrasé** : il porte des identifiants et, en production, des clés API réelles. `install` fusionne (ajoute les clés absentes), `activate` ne remplit que les valeurs vides.

---

### Task 1: Sécuriser le travail en cours et cloner le dépôt

L'arbre de travail porte ~1200 lignes non commitées sur 9 fichiers et 20 commits non poussés. Rien ne doit être perdu, et quatre répertoires de données de conteneurs (`sabnzbd/`, `overseerr-backup/`, `qbittorrent-vpn/`, `tdarr/configs-nvenc/`) apparaissent comme non suivis alors qu'ils ne doivent **jamais** être commités.

**Files:**
- Modify: `/opt/nivuus/MediaManager/.gitignore`
- Create: `/home/mallanic/Projects/Nivuus/packages/media-manager/` (clone)

**Interfaces:**
- Produces: le dépôt `packages/media-manager` sur la branche `tdarr-flow-v6`, historique complet, `origin` = `git@github.com:nivuus/media-manager.git`, propriété `mallanic:mallanic`.

- [ ] **Step 1: Ignorer les quatre répertoires de données non suivis**

Dans `/opt/nivuus/MediaManager/.gitignore`, sous le bloc `# Service data directories (generated at first run)`, ajouter :

```
sabnzbd/
qbittorrent-vpn/
overseerr-backup/
tdarr/configs-nvenc/
aria2-config/
```

- [ ] **Step 2: Vérifier qu'il ne reste aucun fichier non suivi indésirable**

```bash
cd /opt/nivuus/MediaManager && git status --porcelain | grep '^??'
```

Attendu : aucune sortie. Si un chemin apparaît, l'ajouter à `.gitignore` s'il s'agit de données de conteneur, sinon décider explicitement.

- [ ] **Step 3: Committer le travail en cours en trois lots cohérents**

```bash
cd /opt/nivuus/MediaManager
git add reset-error.py update_wanted.py media_cleanup.py .env.example
git commit -m "feat(scripts): durcissement des scripts de maintenance

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"

git add scripts/tdarr/build_flow_v6.py tdarr/flows/OLED4K_norm_v6.json
git commit -m "feat(tdarr): mise a jour du flow v6

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"

git add CLAUDE.md README.md .gitignore
git commit -m "docs: mise a jour de la documentation projet

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Vérifier que l'arbre est propre**

```bash
cd /opt/nivuus/MediaManager && git status --porcelain
```

Attendu : aucune sortie.

- [ ] **Step 5: Cloner vers l'emplacement du package**

```bash
git clone /opt/nivuus/MediaManager /home/mallanic/Projects/Nivuus/packages/media-manager
cd /home/mallanic/Projects/Nivuus/packages/media-manager
git branch main origin/main
git remote set-url origin git@github.com:nivuus/media-manager.git
chown -R mallanic:mallanic /home/mallanic/Projects/Nivuus/packages/media-manager
```

`git branch main origin/main` est nécessaire : un clone ne crée en local que la branche courante (`tdarr-flow-v6`), `main` n'existerait que comme référence de suivi vers l'ancien chemin, qui va disparaître.

- [ ] **Step 6: Vérifier que l'historique et les deux branches ont suivi**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
git log --oneline | wc -l          # attendu: >= 23
git branch                          # attendu: main et tdarr-flow-v6
git log --oneline main..tdarr-flow-v6 | wc -l   # attendu: 23
git remote -v                       # attendu: nivuus/media-manager
ls                                  # attendu: les fichiers source, aucun repertoire de donnees
```

- [ ] **Step 7: Commit**

Rien à committer ici — le clone est le livrable. Vérifier une dernière fois que `/opt/nivuus/MediaManager` tourne toujours :

```bash
docker compose -f /opt/nivuus/MediaManager/docker-compose.yml ps --format '{{.Name}}\t{{.Status}}' | wc -l   # attendu: 12
```

---

### Task 2: Restructurer le dépôt autour de `stack/`

Tout ce qui doit atterrir sur la cible passe sous `stack/`, dont la racine devient la racine du déploiement. Le hook `install` se réduit alors à une copie récursive : il n'y a pas de liste de fichiers à tenir à jour, donc pas de fichier qu'on oublie d'ajouter en même temps qu'un service.

**Files:**
- Move: `docker-compose.yml`, `reset-error.py`, `update_wanted.py`, `media_cleanup.py`, `scripts/`, `tdarr/` → `stack/`
- Move: `.env.example` → `stack/env.template`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `stack/docker-compose.yml`, `stack/env.template`, `stack/reset-error.py`, `stack/update_wanted.py`, `stack/media_cleanup.py`, `stack/scripts/tdarr/`, `stack/tdarr/{bin,flows,server}/`.

- [ ] **Step 1: Déplacer les fichiers avec `git mv`**

`git mv` préserve l'historique par fichier — un `rm` + `add` le romprait.

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
mkdir stack
git mv docker-compose.yml reset-error.py update_wanted.py media_cleanup.py scripts tdarr stack/
git mv .env.example stack/env.template
```

- [ ] **Step 2: Réécrire `.gitignore`**

Le dépôt n'est plus un répertoire de déploiement : les règles qui ignoraient les données de conteneurs n'ont plus d'objet et masqueraient des fichiers du package. Remplacer tout le contenu par :

```
# Environnement rendu a l'installation (contient des identifiants)
stack/.env

# Cache Python
__pycache__/
*.pyc

# Journaux
*.log

# Fichiers OS
.DS_Store
Thumbs.db
```

- [ ] **Step 3: Vérifier que les 16 fichiers source ont suivi**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
git status --porcelain          # uniquement des renommages (R) + .gitignore modifie
ls stack/                        # docker-compose.yml env.template media_cleanup.py reset-error.py scripts tdarr update_wanted.py
ls stack/tdarr/server/Tdarr/Plugins/FlowPlugins/LocalFlowPlugins/*/*/  # les 2 plugins locaux
```

- [ ] **Step 4: Vérifier que le compose reste analysable depuis son nouvel emplacement**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager/stack
python3 -c "import yaml,sys; d=yaml.safe_load(open('docker-compose.yml')); print(len(d['services']), 'services')"
```

Attendu : `16 services`.

- [ ] **Step 5: Commit**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
git add -A
git commit -m "refactor(package): la pile de deploiement passe sous stack/

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Rendre le compose indépendant de la machine

Quatre corrections, toutes rendues nécessaires par le fait que le compose cesse d'être propre à un hôte : le nom de projet dérivait du nom du répertoire, trois montages pointaient sur un chemin absolu qui va disparaître, deux GID matériels étaient codés en dur, et quatre services montaient `/dev/dri` sans condition.

**Files:**
- Modify: `stack/docker-compose.yml`
- Create: `stack/docker-compose.qsv.yml`
- Test: `tests/test_compose_portable.py`

**Interfaces:**
- Produces: `stack/docker-compose.qsv.yml` (surcouche matérielle), variables `.env` attendues `VIDEO_GID`, `RENDER_GID`, `COMPOSE_FILE`, `COMPOSE_PROFILES`.

- [ ] **Step 1: Écrire le test qui échoue**

Créer `tests/test_compose_portable.py` :

```python
#!/usr/bin/env python3
"""Le compose ne doit plus rien savoir de la machine sur laquelle il tourne.

Chaque assertion garde une regression precise et deja constatee :
le nom de projet derivait du nom du repertoire (renommer le dossier
orphelinait les 12 conteneurs), trois montages tdarr_cache pointaient sur
/opt/nivuus/MediaManager en absolu, et group_add supposait render=105 —
une valeur qui varie selon la version de Debian.

Run: python3 tests/test_compose_portable.py
"""
import pathlib
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
STACK = REPO / "stack"
MAIN = STACK / "docker-compose.yml"
QSV = STACK / "docker-compose.qsv.yml"

# Les quatre services qui consomment le rendu materiel Intel.
QSV_SERVICES = ("tdarr", "tdarr-node", "plex", "openai-whisper-asr-webservice")

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


main = yaml.safe_load(MAIN.read_text())
qsv = yaml.safe_load(QSV.read_text())

# Le nom de projet est epingle, il ne derive plus du nom du repertoire.
check("nom de projet", main.get("name"), "mediamanager")

# Aucun chemin absolu vers l'ancien emplacement, dans aucun des deux fichiers.
for label, path in (("main", MAIN), ("qsv", QSV)):
    check(f"{label}: pas de chemin absolu /opt/nivuus",
          "/opt/nivuus" in path.read_text(), False)

# Les trois montages de cache Tdarr sont relatifs.
cache_mounts = [
    volume
    for service in main["services"].values()
    for volume in (service.get("volumes") or [])
    if isinstance(volume, str) and volume.endswith(":/temp")
]
check("nombre de montages de cache", len(cache_mounts), 3)
check("montages de cache relatifs",
      set(cache_mounts), {"./tdarr_cache:/temp"})

# Le fichier principal ne monte plus aucun peripherique de rendu.
for name, service in main["services"].items():
    for device in service.get("devices") or []:
        check(f"{name}: aucun /dev/dri dans le fichier principal",
              "/dev/dri" in str(device), False)
    check(f"{name}: aucun group_add dans le fichier principal",
          "group_add" in service, False)

# La surcouche les monte, pour exactement les quatre services concernes.
check("services de la surcouche QSV",
      sorted(qsv["services"]), sorted(QSV_SERVICES))
for name in QSV_SERVICES:
    check(f"{name}: /dev/dri monte par la surcouche",
          qsv["services"][name]["devices"], ["/dev/dri:/dev/dri"])

# Les GID materiels sont des variables, plus des litteraux.
for name in ("tdarr", "tdarr-node"):
    check(f"{name}: GID resolus a l'installation",
          qsv["services"][name]["group_add"],
          ["${VIDEO_GID}", "${RENDER_GID}"])

# Le node NVENC est optionnel : il ne demarre que sur profil.
check("profil du node NVENC",
      main["services"]["tdarr-node-nvenc"].get("profiles"), ["nvenc"])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_compose_portable: OK")
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_compose_portable.py
```

Attendu : ÉCHEC — `FileNotFoundError` sur `stack/docker-compose.qsv.yml`.

- [ ] **Step 3: Épingler le nom de projet**

Ajouter en **première ligne** de `stack/docker-compose.yml`, avant `services:` :

```yaml
# Le nom de projet est epingle : il derivait du nom du repertoire, et le
# renommage de /opt/nivuus/MediaManager en media-manager aurait orpheline les
# douze conteneurs en cours. La valeur est celle que la production utilise
# deja, pour que le renommage ne recree rien.
name: mediamanager

services:
```

- [ ] **Step 4: Rendre les trois montages de cache relatifs**

Remplacer les trois occurrences (services `tdarr`, `tdarr-node`, `tdarr-node-nvenc`) :

```yaml
      - /opt/nivuus/MediaManager/tdarr_cache:/temp
```

par :

```yaml
      - ./tdarr_cache:/temp
```

- [ ] **Step 5: Extraire les blocs matériels dans la surcouche**

Supprimer de `stack/docker-compose.yml` les blocs `devices: [- /dev/dri:/dev/dri]` des services `tdarr`, `tdarr-node`, `plex`, `openai-whisper-asr-webservice`, ainsi que les blocs `group_add: ["44", "105"]` de `tdarr` et `tdarr-node`. Créer `stack/docker-compose.qsv.yml` :

```yaml
# Surcouche d'acceleration materielle Intel (QSV).
#
# Fusionnee au fichier principal par COMPOSE_FILE, que le hook install n'ecrit
# QUE si la machine expose /dev/dri. Sans cette separation, les quatre services
# ci-dessous echouent au demarrage sur une machine sans iGPU — un montage de
# peripherique absent est une erreur, pas une degradation.
#
# Les GID ne sont pas des litteraux : video=44 est stable sur Debian, render ne
# l'est pas (104, 105 ou 106 selon la version). Le hook install les lit dans
# /etc/group de la cible et les ecrit dans le .env.
services:
  tdarr:
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "${VIDEO_GID}"
      - "${RENDER_GID}"

  tdarr-node:
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "${VIDEO_GID}"
      - "${RENDER_GID}"

  plex:
    devices:
      - /dev/dri:/dev/dri

  openai-whisper-asr-webservice:
    devices:
      - /dev/dri:/dev/dri
```

- [ ] **Step 6: Rendre le node NVENC optionnel**

Dans le service `tdarr-node-nvenc` de `stack/docker-compose.yml`, ajouter juste après `restart: unless-stopped` :

```yaml
    # Le second node d'encodage partage la RTX 4070 avec la VM Windows du
    # package console. Il n'a de sens que sur une machine qui a une carte
    # NVIDIA : active par COMPOSE_PROFILES=nvenc, ecrit par le hook install
    # quand la reponse `nvenc_node` du wizard est vraie.
    profiles:
      - nvenc
```

- [ ] **Step 7: Lancer le test pour vérifier qu'il passe**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_compose_portable.py
```

Attendu : `test_compose_portable: OK`.

- [ ] **Step 8: Commit**

```bash
git add stack/docker-compose.yml stack/docker-compose.qsv.yml tests/test_compose_portable.py
git commit -m "feat(stack): compose independant de la machine hote

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Manifeste et wizard

**Files:**
- Create: `nivuus-package.yaml`
- Create: `wizard.yaml`
- Test: `tests/test_manifest_contract.py`

**Interfaces:**
- Produces: les clés de réponses `media_root`, `transcode_dir`, `timezone`, `nvenc_node`, `plex_claim`, `ygg_username`, `ygg_password`, `tmdb_token`, consommées par `hooks/install.py` (Task 5).

- [ ] **Step 1: Écrire le test qui échoue**

Créer `tests/test_manifest_contract.py` :

```python
#!/usr/bin/env python3
"""Le manifeste et le wizard, verifies contre le contrat nivuus.dev/v1.

Le package est un depot autonome : il ne peut pas importer packages/manifest.py
du depot installer. Les regles du contrat sont donc reverifiees ici, a
l'identique, pour qu'une erreur soit vue au commit et pas a la construction de
l'ISO. Quand NIVUUS_INSTALLER_DIR est defini, le vrai parseur du moteur est
utilise en plus — c'est la verification qui fait autorite.

Run: python3 tests/test_manifest_contract.py
"""
import os
import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Le vocabulaire ferme du wizard : six types, pas un de plus.
QUESTION_TYPES = ("bool", "choix", "texte", "secret", "disque", "gpu")
# Les cles que seul un package de tier `platform` peut declarer.
PLATFORM_KEYS = ("kernel-cmdline", "modules", "hugepages-mib")

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


manifest = yaml.safe_load((REPO / "nivuus-package.yaml").read_text())

check("apiVersion", manifest.get("apiVersion"), "nivuus.dev/v1")
check("nom conforme", bool(NAME_RE.match(manifest.get("name", ""))), True)
check("nom", manifest.get("name"), "media-manager")
check("version conforme", bool(VERSION_RE.match(manifest.get("version", ""))), True)
check("tier", manifest.get("tier"), "userspace")
check("libelle non vide", bool((manifest.get("label") or "").strip()), True)

# tier userspace : declarer une cle plateforme fait REFUSER le manifeste par le
# moteur, ce n'est pas ignore silencieusement.
platform = manifest.get("platform") or {}
for key in PLATFORM_KEYS:
    check(f"aucune cle plateforme: {key}", key in platform, False)

# Aucun claim : media-manager et console doivent rester co-installables.
# console reclame le GPU en exclusivite ; un claim identique ici les rendrait
# mutuellement exclusifs, soit l'inverse de la production.
check("aucun claim", manifest.get("claims") or {}, {})

# Les dependances sont declarees en apt, pas deleguees a une feature du moteur :
# c'est ce qui garde l'installation autonome sur une Debian ordinaire possible.
requires = manifest.get("requires") or {}
check("aucune feature requise", requires.get("features") or [], [])
check("aucune capability requise", requires.get("capabilities") or [], [])
for package in ("docker.io", "docker-compose-v2", "python3-requests"):
    check(f"apt declare {package}", package in (manifest.get("apt") or []), True)

# Les hooks declares existent vraiment : un hook manquant fait echouer
# l'installation au moment ou il est lance, pas au moment ou il est declare.
hooks = manifest.get("hooks") or {}
check("phases declarees", sorted(hooks), ["activate", "install"])
for phase, rel in hooks.items():
    check(f"hook {phase} present", (REPO / rel).is_file(), True)

# Le wizard.
questions_rel = (manifest.get("wizard") or {}).get("questions")
check("fichier de questions declare", questions_rel, "wizard.yaml")
questions = yaml.safe_load((REPO / questions_rel).read_text())
check("les questions sont une liste", isinstance(questions, list), True)

keys = [q.get("key") for q in questions]
check("cles uniques", len(keys), len(set(keys)))
check("cles attendues", sorted(keys),
      sorted(["media_root", "transcode_dir", "timezone", "nvenc_node",
              "plex_claim", "ygg_username", "ygg_password", "tmdb_token"]))

for question in questions:
    key = question.get("key")
    check(f"{key}: type dans le vocabulaire",
          question.get("type") in QUESTION_TYPES, True)
    check(f"{key}: libelle non vide",
          bool((question.get("label") or "").strip()), True)
    # Un secret ne porte jamais de defaut : le portail ne le renverrait pas.
    if question.get("type") == "secret":
        check(f"{key}: pas de defaut sur un secret",
              "default" in question, False)

# media_root est du texte, PAS du type `disque`. Le type `disque` designe un
# peripherique bloc reclame en exclusivite et declenche cote moteur le refus
# « ce disque est la cible d'installation ». La mediatheque vit sur un systeme
# de fichiers existant qu'elle ne reclame pas.
by_key = {q["key"]: q for q in questions}
check("media_root est du texte", by_key["media_root"]["type"], "texte")
check("media_root est requis", by_key["media_root"].get("required"), True)
check("nvenc_node est un booleen", by_key["nvenc_node"]["type"], "bool")

# Verification faisant autorite, quand le depot installer est disponible.
installer_dir = os.environ.get("NIVUUS_INSTALLER_DIR")
if installer_dir:
    sys.path.insert(0, os.path.join(installer_dir, "installer"))
    from packages.manifest import load_manifest
    from packages.wizard import load_questions
    parsed = load_manifest(str(REPO / "nivuus-package.yaml"))
    check("moteur: nom", parsed.name, "media-manager")
    check("moteur: tier", parsed.tier, "userspace")
    loaded = load_questions(str(REPO / "wizard.yaml"))
    check("moteur: nombre de questions", len(loaded), len(questions))
else:
    print("NIVUUS_INSTALLER_DIR non defini : verification moteur sautee")

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_manifest_contract: OK")
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_manifest_contract.py
```

Attendu : ÉCHEC — `FileNotFoundError` sur `nivuus-package.yaml`.

- [ ] **Step 3: Écrire le manifeste**

Créer `nivuus-package.yaml` :

```yaml
apiVersion: nivuus.dev/v1
name: media-manager
version: 1.0.0
label: "Mediatheque (Plex, *arr, Tdarr)"
tier: userspace

# Aucun `requires:` et aucun `claims:`, tous deux deliberement.
#
# `requires.features: [docker]` aurait ete le reflexe. C'est le precedent pose
# par console avec firewalld qui tranche : un package declare ses PROPRES
# dependances plutot que de se coupler a la liste de features du moteur, sinon
# l'installation autonome sur une Debian ordinaire — que le contrat existe
# precisement pour permettre — casse. Si l'operateur a coche la feature docker,
# l'apt-get ci-dessous est un no-op idempotent.
#
# `claims: {gpu: exclusive}` aurait ete faux. Tdarr utilise /dev/dri (QSV Intel)
# et un second node NVENC qui PARTAGE la RTX 4070 avec la VM Windows, via les
# hooks libvirt du package console. Un claim exclusif ferait entrer les deux
# packages en conflit dans check_conflicts() et les rendrait mutuellement
# exclusifs — soit exactement l'inverse de la production.
#
# `requires.capabilities` : le vocabulaire du moteur (iommu, gpu-discrete,
# nvme-dedicated, cpu-hybrid) ne contient rien que la mediatheque exige. Les
# demander masquerait le package sur des machines ou il fonctionne.

apt:
  - docker.io
  - docker-compose-v2
  # requests et dotenv : les trois scripts de maintenance en dependent, et ils
  # sont lances par des timers systemd des le premier boot.
  - python3-requests
  - python3-dotenv

wizard:
  questions: wizard.yaml

# Pas de hook `resolve` : le package est userspace, il ne declare ni parametre
# noyau, ni module, ni hugepage — il n'y a rien a resoudre. Et refuser une
# machine sans /dev/dri serait faux : Plex et Tdarr transcodent en logiciel.
# L'absence d'acceleration est traitee en install, par docker-compose.qsv.yml.
hooks:
  install: hooks/install.py
  activate: hooks/activate.py
```

- [ ] **Step 4: Écrire le wizard**

Créer `wizard.yaml` :

```yaml
# DOWNLOADS_DIR, MOVIES_DIR et TV_DIR ne sont PAS demandes : ils sont derives de
# media_root. Ce sont les trois sous-repertoires du meme point de montage, et
# les separer est ce qui a desactive les hardlinks une premiere fois (voir
# CLAUDE.md, « Critical Path Mappings ») — un import de 4,8 Go passe alors de
# quelques secondes a 13 minutes, avec la place occupee deux fois.
- key: media_root
  type: texte
  label: "Racine de la mediatheque (contiendra Downloads, Movies et TV Shows)"
  default: "/media/data"
  required: true

- key: transcode_dir
  type: texte
  label: "Repertoire de transcodage temporaire de Plex"
  default: "/media/backup/.transcode"

- key: timezone
  type: texte
  label: "Fuseau horaire"
  default: "Europe/Paris"

- key: nvenc_node
  type: bool
  label: "Second node de transcodage Tdarr sur GPU NVIDIA (NVENC)"
  default: false

- key: plex_claim
  type: secret
  label: "Jeton de rattachement Plex (claim.plex.tv, facultatif)"

- key: ygg_username
  type: texte
  label: "Identifiant YGG pour l'indexeur Ygege (facultatif)"
  default: ""

- key: ygg_password
  type: secret
  label: "Mot de passe YGG (facultatif)"

- key: tmdb_token
  type: secret
  label: "Jeton API TMDb, requis par Ygege (facultatif)"
```

- [ ] **Step 5: Créer des hooks vides pour satisfaire la déclaration**

Le test vérifie que les hooks déclarés existent. Ils sont écrits pour de bon aux tâches 5 et 7 ; les créer ici en squelettes exécutables :

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
mkdir -p hooks
for phase in install activate; do
  printf '#!/usr/bin/env python3\n"""Squelette — implemente dans la tache suivante."""\nimport sys\nsys.exit(1)\n' > hooks/$phase.py
  chmod +x hooks/$phase.py
done
```

- [ ] **Step 6: Lancer le test pour vérifier qu'il passe**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_manifest_contract.py
NIVUUS_INSTALLER_DIR=/home/mallanic/Projects/Nivuus/packages/installer python3 tests/test_manifest_contract.py
```

Attendu : `test_manifest_contract: OK` deux fois, la seconde sans la ligne « verification moteur sautee ».

- [ ] **Step 7: Commit**

```bash
git add nivuus-package.yaml wizard.yaml hooks/ tests/test_manifest_contract.py
git commit -m "feat(package): manifeste nivuus.dev/v1 et questions du wizard

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Le hook `install`

Dépose `stack/` sur la cible, rend le `.env`, pose les unités systemd. Trois règles portent tout le poids : le `.env` n'est jamais écrasé, les GID sont lus sur la cible, et `/dev/dri` est cherché sur la machine **vivante**, pas sous `--root`.

**Files:**
- Modify: `hooks/install.py`
- Modify: `stack/env.template`
- Test: `tests/test_install_hook.py`

**Interfaces:**
- Consumes: les clés de réponses de `wizard.yaml` (Task 4), `stack/` (Task 2), les unités de `systemd/` (Task 6 — les six fichiers doivent exister avant que ce hook ne s'exécute pour de bon ; le test de cette tâche les fabrique).
- Produces: `{root}/opt/nivuus/media-manager/` peuplé, `.env` en 0600, six unités en `{root}/etc/systemd/system/`.

- [ ] **Step 1: Écrire le test qui échoue**

Créer `tests/test_install_hook.py` :

```python
#!/usr/bin/env python3
"""Le hook install, verifie par ses ARTEFACTS sous une racine temporaire.

Jamais par des appels simules : tout l'interet de ce hook est ce qu'il laisse
sur le systeme de fichiers cible, et un test qui simulerait la copie ne
prouverait rien a son sujet.

Les deux cas qui ont motive le plus de code sont ici : un .env existant n'est
JAMAIS ecrase (en production il porte les cles API reelles et les identifiants
YGG), et une reponse booleenne recue sous forme de chaine est refusee, jamais
convertie — bool("false") vaut True en Python, et ce piege a deja ete rencontre
deux fois dans ce projet.

Run: python3 tests/test_install_hook.py
"""
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "install.py"
DEST_REL = "opt/nivuus/media-manager"

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def context(answers):
    return json.dumps({
        "package": {"name": "media-manager", "version": "1.0.0",
                    "root": str(REPO)},
        "hw": {"gpus": [{"slot": "00:02.0", "vendor": "intel",
                         "discrete": False}]},
        "answers": answers,
    })


def run(root, answers):
    return subprocess.run(
        [sys.executable, str(HOOK), "--phase", "install", "--root", root],
        input=context(answers), capture_output=True, text=True, cwd=str(REPO))


def fake_group_file(root):
    """Une cible ou render vaut 106, pas la valeur par defaut 105."""
    path = pathlib.Path(root) / "etc" / "group"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("root:x:0:\nvideo:x:44:\nrender:x:106:\n")


def env_values(path):
    values = {}
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


ANSWERS = {
    "media_root": "/srv/media",
    "transcode_dir": "/srv/transcode",
    "timezone": "Europe/Paris",
    "nvenc_node": True,
    "plex_claim": "claim-abc",
    "ygg_username": "moi",
    "ygg_password": "secret",
    "tmdb_token": "jeton",
}

# --- Installation nominale ------------------------------------------------
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    proc = run(root, ANSWERS)
    check("code de sortie", proc.returncode, 0)

    dest = pathlib.Path(root) / DEST_REL

    # La pile entiere a ete deposee, y compris les assets Tdarr.
    for rel in ("docker-compose.yml", "docker-compose.qsv.yml",
                "reset-error.py", "update_wanted.py", "media_cleanup.py",
                "tdarr/flows/OLED4K_norm_v6.json",
                "tdarr/bin/fetch-binaries.sh",
                "scripts/tdarr/build_flow_v6.py"):
        check(f"depose: {rel}", (dest / rel).is_file(), True)

    # env.template ne doit PAS voyager : il n'a plus d'objet une fois rendu,
    # et un operateur qui le trouve a cote du .env ne sait pas lequel fait foi.
    check("env.template non depose", (dest / "env.template").exists(), False)

    env_path = dest / ".env"
    check(".env ecrit", env_path.is_file(), True)
    # 0600 : le fichier porte plex_claim, ygg_password et tmdb_token en clair,
    # et recevra les cles API a la phase activate.
    check(".env en 0600",
          stat.S_IMODE(env_path.stat().st_mode), 0o600)

    values = env_values(env_path)
    check("MEDIA_ROOT", values["MEDIA_ROOT"], "/srv/media")
    check("DOWNLOADS_DIR derive", values["DOWNLOADS_DIR"], "/srv/media/Downloads")
    check("MOVIES_DIR derive", values["MOVIES_DIR"], "/srv/media/Movies")
    check("TV_DIR derive", values["TV_DIR"], "/srv/media/TV Shows")
    check("TRANSCODE_DIR", values["TRANSCODE_DIR"], "/srv/transcode")
    check("TZ", values["TZ"], "Europe/Paris")
    check("PUID", values["PUID"], "1000")
    # Les GID viennent du /etc/group de la CIBLE, pas de l'hote qui installe.
    check("VIDEO_GID lu sur la cible", values["VIDEO_GID"], "44")
    check("RENDER_GID lu sur la cible", values["RENDER_GID"], "106")
    check("profil NVENC actif", values["COMPOSE_PROFILES"], "nvenc")
    check("secrets reportes", values["TMDB_TOKEN"], "jeton")
    # Les cles API n'existent pas encore : chaque service la genere a son
    # premier demarrage. La phase activate les recoltera.
    check("cle Radarr vide", values["RADARR_API_KEY"], "")

    # COMPOSE_FILE reflete la presence reelle de /dev/dri sur cette machine.
    expected_files = ("docker-compose.yml:docker-compose.qsv.yml"
                      if os.path.isdir("/dev/dri") else "docker-compose.yml")
    check("COMPOSE_FILE", values["COMPOSE_FILE"], expected_files)

    # Les six unites sont posees, en 0644 : une unite est une donnee, pas un
    # programme.
    units = pathlib.Path(root) / "etc/systemd/system"
    for job in ("reset-error", "update-wanted", "cleanup"):
        for kind in ("service", "timer"):
            unit = units / f"media-manager-{job}.{kind}"
            check(f"unite posee: {unit.name}", unit.is_file(), True)
            check(f"{unit.name} en 0644",
                  stat.S_IMODE(unit.stat().st_mode), 0o644)
    # Posees, deliberement PAS armees : c'est la phase activate qui arme, une
    # fois qu'il y a une pile a maintenir.
    check("aucun timer arme a l'install",
          (units / "timers.target.wants").exists(), False)

# --- Le .env existant n'est jamais ecrase ---------------------------------
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    dest = pathlib.Path(root) / DEST_REL
    dest.mkdir(parents=True)
    (dest / ".env").write_text(
        "# fichier de production\n"
        "MEDIA_ROOT=/media/data\n"
        "RADARR_API_KEY=cle-reelle-a-conserver\n")

    proc = run(root, ANSWERS)
    check("code de sortie (fusion)", proc.returncode, 0)

    values = env_values(dest / ".env")
    check("valeur existante preservee", values["MEDIA_ROOT"], "/media/data")
    check("cle API preservee", values["RADARR_API_KEY"], "cle-reelle-a-conserver")
    # Les variables nouvelles sont ajoutees, sinon le compose ne demarrerait
    # pas apres la bascule.
    check("VIDEO_GID ajoute", values["VIDEO_GID"], "44")
    check("RENDER_GID ajoute", values["RENDER_GID"], "106")
    check("COMPOSE_PROFILES ajoute", values["COMPOSE_PROFILES"], "nvenc")
    check("commentaire preserve",
          "# fichier de production" in (dest / ".env").read_text(), True)

# --- Une reponse booleenne mal typee est refusee, pas convertie -----------
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    bad = dict(ANSWERS, nvenc_node="false")
    proc = run(root, bad)
    check("code de sortie (booleen invalide)", proc.returncode, 1)
    check("la cause est nommee", "nvenc_node" in proc.stderr, True)
    # Rien n'a ete ecrit : l'echec arrive avant le premier octet depose.
    check("aucun artefact laisse",
          (pathlib.Path(root) / DEST_REL).exists(), False)

# --- Sans /etc/group sur la cible, les valeurs Debian par defaut ----------
with tempfile.TemporaryDirectory() as root:
    proc = run(root, ANSWERS)
    check("code de sortie (pas de /etc/group)", proc.returncode, 0)
    values = env_values(pathlib.Path(root) / DEST_REL / ".env")
    check("VIDEO_GID par defaut", values["VIDEO_GID"], "44")
    check("RENDER_GID par defaut", values["RENDER_GID"], "105")

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_install_hook: OK")
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_install_hook.py
```

Attendu : ÉCHEC — le squelette sort en 1, `check("code de sortie", 1, 0)`.

- [ ] **Step 3: Transformer `stack/env.template`**

Réécrire `stack/env.template` (l'ancien `.env.example`) en remplaçant les valeurs pilotées par le wizard par des jetons `@CLE@`. Les jetons sont en `@…@` et non `${…}` pour ne pas entrer en collision avec l'interpolation de Docker Compose, qui lit ce même fichier une fois rendu.

```
# =============================================================================
# MediaManager — rendu par hooks/install.py, ne pas editer a la main les
# valeurs pilotees par le wizard : une reinstallation les recalcule.
# Les cles API, elles, sont ecrites par hooks/activate.py apres le premier
# demarrage des services, et jamais ecrasees ensuite.
# =============================================================================

TZ=@TZ@
PUID=@PUID@
PGID=@PGID@

# =============================================================================
# Chemins (absolus, sur l'hote)
# Downloads, Movies et TV Shows sont derives de MEDIA_ROOT et doivent rester
# sous le meme point de montage : c'est ce qui permet les hardlinks a l'import.
# =============================================================================
MEDIA_ROOT=@MEDIA_ROOT@
DOWNLOADS_DIR=@DOWNLOADS_DIR@
MOVIES_DIR=@MOVIES_DIR@
TV_DIR=@TV_DIR@
TRANSCODE_DIR=@TRANSCODE_DIR@

# =============================================================================
# Composition de la pile
# COMPOSE_FILE fusionne la surcouche d'acceleration materielle seulement si la
# machine expose /dev/dri. COMPOSE_PROFILES active le node NVENC.
# =============================================================================
COMPOSE_FILE=@COMPOSE_FILE@
COMPOSE_PROFILES=@COMPOSE_PROFILES@

# GID des groupes video et render de cet hote, lus dans /etc/group.
VIDEO_GID=@VIDEO_GID@
RENDER_GID=@RENDER_GID@

# =============================================================================
# Cles API — generees par chaque service a son premier demarrage.
# hooks/activate.py recolte celles de Radarr, Sonarr et Prowlarr dans leur
# config.xml. Tautulli et Seerr n'exposent pas la leur de cette facon : les
# renseigner a la main depuis Parametres > General.
# =============================================================================
RADARR_API_KEY=
SONARR_API_KEY=
PROWLARR_API_KEY=
BAZARR_API_KEY=
TAUTULLI_API_KEY=
OVERSEERR_API_KEY=

# =============================================================================
# URLs des services (utilisees par les scripts de maintenance)
# =============================================================================
RADARR_URL=http://localhost:7878
SONARR_URL=http://localhost:8989
TAUTULLI_URL=http://localhost:8181
OVERSEERR_URL=http://localhost:5055

# =============================================================================
# Identifiants d'indexeur (facultatifs)
# =============================================================================
YGG_USERNAME=@YGG_USERNAME@
YGG_PASSWORD=@YGG_PASSWORD@
TMDB_TOKEN=@TMDB_TOKEN@
PLEX_CLAIM=@PLEX_CLAIM@
```

- [ ] **Step 4: Écrire le hook**

Remplacer `hooks/install.py` :

```python
#!/usr/bin/env python3
"""Phase install du package media-manager : deposer la pile sur la cible.

Le sous-arbre stack/ EST le repertoire de deploiement, a l'octet pres, donc
la depose est une copie recursive — il n'y a pas de liste de fichiers a tenir
a jour, et donc pas de fichier qu'on oublie d'ajouter en meme temps qu'un
service.

TROIS REGLES PORTENT LE RESTE.

1. LE .env N'EST JAMAIS ECRASE. En production il porte les cles API reelles,
   les identifiants YGG et le jeton TMDb. Une reinstallation qui le
   reecrirait detruirait tout cela sans bruit — et cette phase tourne aussi
   sur une machine deja installee (la bascule de production passe par
   `install.py --root /`). Les cles absentes sont AJOUTEES, les presentes ne
   sont pas touchees.

2. LES GID SONT LUS SUR LA CIBLE. group_add supposait video=44 et render=105.
   video=44 est stable sur Debian, render ne l'est pas (104, 105 ou 106 selon
   la version). Un GID faux ne produit pas d'erreur : le conteneur demarre et
   n'a simplement pas acces au peripherique de rendu, ce qui se manifeste
   beaucoup plus tard, en transcodage logiciel silencieux.

3. /dev/dri EST CHERCHE SUR LA MACHINE VIVANTE, PAS SOUS --root. Pendant une
   installation depuis l'ISO, --root vaut /mnt/target et le /dev de la cible
   n'est pas peuple : debootstrap n'y cree qu'un /dev minimal. Chercher
   {root}/dev/dri repondrait donc « absent » sur toute machine installee
   depuis l'ISO, y compris celles qui ont un iGPU. La machine qui execute
   l'installateur EST la machine cible : c'est son /dev/dri qui fait foi.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STACK = os.path.join(HERE, "stack")
UNITS_SRC = os.path.join(HERE, "systemd")

DEST_REL = "opt/nivuus/media-manager"
UNIT_REL_DIR = "etc/systemd/system"
TEMPLATE_NAME = "env.template"

UNITS = [
    "media-manager-reset-error.service", "media-manager-reset-error.timer",
    "media-manager-update-wanted.service", "media-manager-update-wanted.timer",
    "media-manager-cleanup.service", "media-manager-cleanup.timer",
]

# Valeurs Debian par defaut, utilisees seulement si /etc/group est illisible.
DEFAULT_GIDS = {"video": 44, "render": 105}

# Le repertoire de peripheriques de rendu, sur la machine vivante (regle 3).
RENDER_NODES = "/dev/dri"


def emit(event):
    print(json.dumps(event), flush=True)


def group_gid(root, name):
    """GID d'un groupe dans le /etc/group de la cible, sinon la valeur Debian."""
    path = os.path.join(root, "etc/group")
    try:
        with open(path) as fh:
            for line in fh:
                fields = line.split(":")
                if len(fields) > 2 and fields[0] == name:
                    return int(fields[2])
    except (OSError, ValueError):
        pass
    return DEFAULT_GIDS[name]


def bool_answer(answers, key):
    """Une reponse booleenne, refusee si elle n'en est pas une.

    bool("false") vaut True en Python. Ce hook lit son contexte sur stdin et
    un config.json ecrit a la main — le chemin autonome que le contrat existe
    pour permettre — ne passe par aucun validateur. Choisir silencieusement
    une lecture est exactement ainsi que le bug se produit.
    """
    value = answers.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"la reponse {key!r} attend true/false, recu {value!r}")
    return value


def text_answer(answers, key, default=""):
    value = answers.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"la reponse {key!r} attend une chaine, recu {value!r}")
    return value.strip() or default


def parse_env(text):
    """Les cles definies dans un .env, dans l'ordre de lecture."""
    keys = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            keys.append(stripped.partition("=")[0].strip())
    return keys


def merge_env(existing, rendered):
    """Le .env existant, augmente des seules cles qu'il n'a pas encore.

    Rien d'existant n'est touche : ni les valeurs, ni les commentaires, ni
    l'ordre. Les cles nouvelles sont ajoutees en fin de fichier sous un
    en-tete qui dit d'ou elles viennent.
    """
    have = set(parse_env(existing))
    added = [line for line in rendered.splitlines()
             if "=" in line and not line.strip().startswith("#")
             and line.partition("=")[0].strip() not in have]
    if not added:
        return existing
    tail = "\n# --- Ajoute par le package media-manager ---\n" + "\n".join(added)
    return existing.rstrip("\n") + "\n" + tail + "\n"


def render_env(values):
    with open(os.path.join(STACK, TEMPLATE_NAME)) as fh:
        text = fh.read()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    return text


def place_unit(name, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    shutil.copyfile(os.path.join(UNITS_SRC, name), dest)
    # 0644 : une unite systemd est une donnee, pas un programme.
    os.chmod(dest, 0o644)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--root", default="/")
    args = parser.parse_args()

    ctx = json.load(sys.stdin)
    answers = ctx.get("answers") or {}
    root = args.root.rstrip("/") or "/"

    # Valider AVANT de deposer le premier octet : un appelant et le package en
    # desaccord sur le contrat est une erreur, pas quelque chose a contourner
    # au milieu d'une copie.
    try:
        nvenc = bool_answer(answers, "nvenc_node")
        media_root = text_answer(answers, "media_root", "/media/data").rstrip("/")
        transcode = text_answer(answers, "transcode_dir", "/media/backup/.transcode")
        timezone = text_answer(answers, "timezone", "Europe/Paris")
        plex_claim = text_answer(answers, "plex_claim")
        ygg_user = text_answer(answers, "ygg_username")
        ygg_pass = text_answer(answers, "ygg_password")
        tmdb = text_answer(answers, "tmdb_token")
    except ValueError as exc:
        print(f"media-manager install: {exc}", file=sys.stderr)
        return 1

    dest = os.path.join(root, DEST_REL)

    emit({"event": "progress", "pct": 20, "msg": "Depose de la pile"})
    shutil.copytree(STACK, dest, symlinks=True, dirs_exist_ok=True)
    # Le gabarit a fait son travail ; le laisser a cote du .env rendu ne
    # laisserait pas savoir lequel fait foi.
    template_copy = os.path.join(dest, TEMPLATE_NAME)
    if os.path.exists(template_copy):
        os.remove(template_copy)

    emit({"event": "progress", "pct": 55, "msg": "Rendu de la configuration"})
    compose_files = "docker-compose.yml"
    if os.path.isdir(RENDER_NODES):
        compose_files += ":docker-compose.qsv.yml"
    else:
        emit({"event": "progress", "pct": 55,
              "msg": f"{RENDER_NODES} absent : transcodage logiciel"})

    rendered = render_env({
        "TZ": timezone,
        "PUID": "1000",
        "PGID": "1000",
        "MEDIA_ROOT": media_root,
        "DOWNLOADS_DIR": f"{media_root}/Downloads",
        "MOVIES_DIR": f"{media_root}/Movies",
        "TV_DIR": f"{media_root}/TV Shows",
        "TRANSCODE_DIR": transcode,
        "COMPOSE_FILE": compose_files,
        "COMPOSE_PROFILES": "nvenc" if nvenc else "",
        "VIDEO_GID": str(group_gid(root, "video")),
        "RENDER_GID": str(group_gid(root, "render")),
        "YGG_USERNAME": ygg_user,
        "YGG_PASSWORD": ygg_pass,
        "TMDB_TOKEN": tmdb,
        "PLEX_CLAIM": plex_claim,
    })

    env_path = os.path.join(dest, ".env")
    if os.path.isfile(env_path):
        with open(env_path) as fh:
            rendered = merge_env(fh.read(), rendered)
        emit({"event": "progress", "pct": 70,
              "msg": ".env existant conserve, variables manquantes ajoutees"})
    with open(env_path, "w") as fh:
        fh.write(rendered)
    # 0600 : identifiants YGG, jeton TMDb, jeton Plex, et bientot les cles API.
    os.chmod(env_path, 0o600)

    emit({"event": "progress", "pct": 85, "msg": "Unites de maintenance posees"})
    unit_dir = os.path.join(root, UNIT_REL_DIR)
    for unit in UNITS:
        place_unit(unit, unit_dir)

    emit({"event": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Lancer le test — il échouera encore, faute d'unités**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_install_hook.py
```

Attendu : ÉCHEC sur `FileNotFoundError` dans `systemd/`. C'est la dépendance déclarée vers la Task 6. Créer les six unités **au contenu définitif de la Task 6** maintenant, puis relancer.

- [ ] **Step 6: Écrire les six unités (contenu détaillé en Task 6)**

Suivre les steps 3 et 4 de la Task 6, puis revenir ici.

- [ ] **Step 7: Lancer le test pour vérifier qu'il passe**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_install_hook.py
```

Attendu : `test_install_hook: OK`.

- [ ] **Step 8: Commit**

```bash
git add hooks/install.py stack/env.template tests/test_install_hook.py systemd/
git commit -m "feat(package): hook install — depose la pile et rend le .env

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Les unités de maintenance

Trois lignes de la crontab root pointent sur `/opt/nivuus/MediaManager` et mourraient au renommage. Elles deviennent trois paires `.service`/`.timer` livrées par le package.

**Files:**
- Create: `systemd/media-manager-reset-error.{service,timer}`
- Create: `systemd/media-manager-update-wanted.{service,timer}`
- Create: `systemd/media-manager-cleanup.{service,timer}`
- Test: `tests/test_maintenance_units.py`

**Interfaces:**
- Consumes: `stack/reset-error.py`, `stack/update_wanted.py`, `stack/media_cleanup.py` (Task 2).
- Produces: les six unités déposées par `hooks/install.py` (Task 5) et armées par `hooks/activate.py` (Task 7).

- [ ] **Step 1: Écrire le test qui échoue**

Créer `tests/test_maintenance_units.py` :

```python
#!/usr/bin/env python3
"""Les unites de maintenance, verifiees comme des DIRECTIVES, pas des chaines.

Les unites systemd sont de l'INI : les analyser plutot que chercher dans le
texte brut est ce qui rend l'assertion vraie a propos d'une directive, et non
d'une ligne qui pourrait tres bien etre commentee.

strict=False parce que systemd tolere une cle repetee (la derniere gagne) la
ou configparser leve. optionxform=str parce que configparser met les cles en
minuscules alors que les directives systemd sont sensibles a la casse —
TimeoutStartSec deviendrait timeoutstartsec.

Run: python3 tests/test_maintenance_units.py
"""
import configparser
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
UNITS = REPO / "systemd"
STACK = REPO / "stack"
DEPLOY = "/opt/nivuus/media-manager"

# job -> (script attendu, heure d'execution)
JOBS = {
    "reset-error": ("reset-error.py", "*-*-* 06:00:00"),
    "update-wanted": ("update_wanted.py", "*-*-* 07:00:00"),
    "cleanup": ("media_cleanup.py", "*-*-* 08:00:00"),
}

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def load_unit(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


for job, (script, calendar) in JOBS.items():
    service = load_unit(UNITS / f"media-manager-{job}.service")
    timer = load_unit(UNITS / f"media-manager-{job}.timer")

    check(f"{job}: oneshot", service["Service"]["Type"], "oneshot")
    exec_start = service["Service"]["ExecStart"]
    check(f"{job}: le script est lance depuis le deploiement",
          exec_start.startswith(f"/usr/bin/python3 {DEPLOY}/{script}"), True)
    # Le script existe vraiment dans la pile : cette assertion attrape un
    # renommage cote stack/ que rien d'autre ne verrait avant le premier
    # declenchement du timer, a 6h du matin.
    check(f"{job}: {script} present dans stack/",
          (STACK / script).is_file(), True)
    check(f"{job}: environnement charge",
          service["Service"]["EnvironmentFile"], f"{DEPLOY}/.env")
    check(f"{job}: repertoire de travail",
          service["Service"]["WorkingDirectory"], DEPLOY)
    # Les trois scripts parlent aux conteneurs : sans docker, ils echouent.
    check(f"{job}: ordonne apres docker",
          "docker.service" in service["Unit"]["After"], True)

    check(f"{job}: horaire", timer["Timer"]["OnCalendar"], calendar)
    # Persistent : une machine eteinte a 6h doit rattraper au demarrage, sinon
    # les telechargements en erreur s'accumulent jusqu'au lendemain.
    check(f"{job}: rattrapage apres extinction",
          timer["Timer"]["Persistent"], "true")
    check(f"{job}: cible d'armement",
          timer["Install"]["WantedBy"], "timers.target")

# Le nettoyage journalise, comme la ligne de crontab qu'il remplace.
cleanup = load_unit(UNITS / "media-manager-cleanup.service")
check("cleanup: journal conserve",
      f"--log {DEPLOY}/cleanup.log" in cleanup["Service"]["ExecStart"], True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_maintenance_units: OK")
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_maintenance_units.py
```

Attendu : ÉCHEC — `KeyError: 'Service'`, les fichiers n'existent pas et configparser lit un ensemble vide.

- [ ] **Step 3: Écrire les trois services**

`systemd/media-manager-reset-error.service` :

```ini
[Unit]
Description=MediaManager — nettoyage des telechargements en erreur
Documentation=file:/opt/nivuus/media-manager/reset-error.py
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/nivuus/media-manager
EnvironmentFile=/opt/nivuus/media-manager/.env
ExecStart=/usr/bin/python3 /opt/nivuus/media-manager/reset-error.py
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=3600
```

`systemd/media-manager-update-wanted.service` :

```ini
[Unit]
Description=MediaManager — recherche des episodes et films manquants
Documentation=file:/opt/nivuus/media-manager/update_wanted.py
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/nivuus/media-manager
EnvironmentFile=/opt/nivuus/media-manager/.env
ExecStart=/usr/bin/python3 /opt/nivuus/media-manager/update_wanted.py
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=3600
```

`systemd/media-manager-cleanup.service` :

```ini
[Unit]
Description=MediaManager — menage disque correle aux statistiques Tautulli
Documentation=file:/opt/nivuus/media-manager/media_cleanup.py
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/nivuus/media-manager
EnvironmentFile=/opt/nivuus/media-manager/.env
ExecStart=/usr/bin/python3 /opt/nivuus/media-manager/media_cleanup.py --log /opt/nivuus/media-manager/cleanup.log
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=3600
```

- [ ] **Step 4: Écrire les trois timers**

`systemd/media-manager-reset-error.timer` :

```ini
[Unit]
Description=MediaManager — nettoyage quotidien des erreurs (06:00)

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
```

`systemd/media-manager-update-wanted.timer` :

```ini
[Unit]
Description=MediaManager — recherche quotidienne des manquants (07:00)

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
```

`systemd/media-manager-cleanup.timer` :

```ini
[Unit]
Description=MediaManager — menage disque quotidien (08:00)

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
```

- [ ] **Step 5: Lancer le test pour vérifier qu'il passe**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_maintenance_units.py
```

Attendu : `test_maintenance_units: OK`.

- [ ] **Step 6: Vérifier que systemd accepte les unités**

```bash
systemd-analyze verify /home/mallanic/Projects/Nivuus/packages/media-manager/systemd/media-manager-reset-error.service 2>&1 | grep -v "EnvironmentFile\|Documentation" || true
```

Attendu : aucune erreur autre que l'absence de `/opt/nivuus/media-manager/.env`, normale hors déploiement.

- [ ] **Step 7: Commit**

```bash
git add systemd/ tests/test_maintenance_units.py
git commit -m "feat(package): trois timers systemd remplacent la crontab

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Le hook `activate`

Au premier boot, réseau disponible : démarrer la pile, récolter les clés API que les services viennent de générer, armer les timers. Sans la récolte, les trois timers tourneraient à vide indéfiniment — armés, annoncés, et silencieusement inertes.

**Files:**
- Modify: `hooks/activate.py`
- Test: `tests/test_activate_hook.py`

**Interfaces:**
- Consumes: `{root}/opt/nivuus/media-manager/` et les six unités posées par `hooks/install.py` (Task 5).
- Produces: `harvest_key(path) -> str`, `fill_env(text, keys) -> str`, `arm(root, unit, wants) -> None` — utilisées par les tests et rien d'autre.

- [ ] **Step 1: Écrire le test qui échoue**

Créer `tests/test_activate_hook.py` :

```python
#!/usr/bin/env python3
"""Le hook activate, verifie par ses fonctions pures et ses artefacts.

Ce qui est teste ici est ce qui peut l'etre sans Docker : la lecture d'une cle
API dans un config.xml d'*arr, le remplissage du .env, et l'armement des
timers par symlink. Le `docker compose up -d` lui-meme est verifie a la
bascule de production (Task 9), pas ici — le simuler ne prouverait rien.

La regle qui compte : fill_env ne remplit que les valeurs VIDES. En production
le .env porte deja les cles reelles, et les ecraser par ce qu'un conteneur
fraichement demarre a genere casserait les trois scripts de maintenance.

Run: python3 tests/test_activate_hook.py
"""
import importlib.util
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "activate_hook", REPO / "hooks" / "activate.py")
activate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activate)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# --- Lecture d'une cle dans un config.xml d'*arr --------------------------
with tempfile.TemporaryDirectory() as tmp:
    config = pathlib.Path(tmp) / "config.xml"
    config.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<Config>\n"
        "  <BindAddress>*</BindAddress>\n"
        "  <Port>7878</Port>\n"
        "  <ApiKey>0123456789abcdef0123456789abcdef</ApiKey>\n"
        "</Config>\n")
    check("cle lue", activate.harvest_key(str(config)),
          "0123456789abcdef0123456789abcdef")

    # Un fichier absent ne leve pas : le service peut simplement ne pas avoir
    # fini de demarrer, et la phase doit reessayer plutot qu'echouer.
    check("fichier absent", activate.harvest_key(str(config) + ".nope"), "")

    # Un XML tronque — un service tue en plein ecriture — ne leve pas non plus.
    broken = pathlib.Path(tmp) / "broken.xml"
    broken.write_text("<Config><ApiKey>tronq")
    check("xml invalide", activate.harvest_key(str(broken)), "")

# --- Remplissage du .env --------------------------------------------------
ENV = (
    "# en-tete\n"
    "MEDIA_ROOT=/media/data\n"
    "RADARR_API_KEY=\n"
    "SONARR_API_KEY=deja-renseignee\n"
    "PROWLARR_API_KEY=\n"
)

filled = activate.fill_env(ENV, {
    "RADARR_API_KEY": "cle-radarr",
    "SONARR_API_KEY": "cle-ecrasante",
    "PROWLARR_API_KEY": "",
})

check("valeur vide remplie", "RADARR_API_KEY=cle-radarr" in filled, True)
# La regle qui compte : une valeur deja renseignee n'est jamais ecrasee.
check("valeur existante preservee",
      "SONARR_API_KEY=deja-renseignee" in filled, True)
check("aucune trace de l'ecrasement", "cle-ecrasante" in filled, False)
# Une recolte infructueuse ne vide ni ne casse la ligne.
check("recolte vide sans effet", "PROWLARR_API_KEY=\n" in filled, True)
check("commentaire preserve", filled.startswith("# en-tete\n"), True)
check("ligne hors sujet preservee", "MEDIA_ROOT=/media/data" in filled, True)

# --- Armement des timers par symlink --------------------------------------
with tempfile.TemporaryDirectory() as root:
    units = pathlib.Path(root) / "etc/systemd/system"
    units.mkdir(parents=True)
    (units / "media-manager-cleanup.timer").write_text("[Timer]\n")

    activate.arm(root, "media-manager-cleanup.timer", "timers.target.wants")
    link = units / "timers.target.wants" / "media-manager-cleanup.timer"
    check("lien cree", link.is_symlink(), True)
    # La cible est un chemin ABSOLU dans l'espace de noms du systeme demarre,
    # pas dans la racine jetable : systemd le resout apres le redemarrage,
    # quand cette racine EST /.
    check("cible absolue", link.readlink().as_posix(),
          "/etc/systemd/system/media-manager-cleanup.timer")

    # Idempotent : rejouer la phase ne doit pas echouer.
    activate.arm(root, "media-manager-cleanup.timer", "timers.target.wants")
    check("idempotent", link.is_symlink(), True)

    # Un fichier ordinaire a la place du lien est le bug, pas un etat a
    # preserver : systemd ignore une entree .wants qui n'est pas un lien, avec
    # « is not a symlink, ignoring ». Neuf entrees dans ce cas existent deja
    # sur cet hote.
    link.unlink()
    link.write_text("pas un lien\n")
    activate.arm(root, "media-manager-cleanup.timer", "timers.target.wants")
    check("fichier ordinaire remplace", link.is_symlink(), True)

    # Une unite absente leve : un lien pendant se lit comme « armee » alors
    # que rien ne se declenchera jamais.
    try:
        activate.arm(root, "media-manager-fantome.timer", "timers.target.wants")
        failures.append("unite absente: aucune exception levee")
    except FileNotFoundError:
        pass

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_activate_hook: OK")
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_activate_hook.py
```

Attendu : ÉCHEC — `SystemExit: 1` levé à l'import du squelette.

- [ ] **Step 3: Écrire le hook**

Remplacer `hooks/activate.py` :

```python
#!/usr/bin/env python3
"""Phase activate du package media-manager : demarrer et cabler la pile.

Trois choses, dans cet ordre, parce que chacune depend de la precedente.

1. DEMARRER LA PILE. C'est ici, et pas en phase install, parce qu'il faut le
   reseau : seize images doivent etre tirees.

2. RECOLTER LES CLES API. Elles n'existent pas au moment du wizard — chaque
   service la genere a son premier demarrage, dans le config.xml de son
   volume de configuration. Sans cette recolte, les trois timers de
   maintenance tourneraient a vide indefiniment : armes, annonces, et
   silencieusement inertes. Seuls Radarr, Sonarr et Prowlarr sont recoltables
   ainsi ; Bazarr, Tautulli et Seerr rangent la leur ailleurs et restent a
   renseigner a la main.

3. ARMER LES TIMERS. Par SYMLINK, jamais par `systemctl enable` : systemctl
   echoue silencieusement en environnement contraint — une sous-commande de
   consultation n'affiche simplement rien — donc un enable qui « a rendu » ne
   dit rien. Un lien existe ou leve. Neuf entrees de sockets.target.wants sur
   l'hote de reference sont des fichiers ORDINAIRES, que systemd ignore avec
   « is not a symlink, ignoring » : chaque lien pose ici est verifie.

Les liens seuls ne rendent correct que le PROCHAIN demarrage. L'unite qui
execute cette phase est WantedBy=multi-user.target, donc elle tourne APRES que
timers.target a ete atteint : sans un daemon-reload et un start explicite, les
timers ne tiquent qu'au deuxieme redemarrage alors que le fichier d'etat dit
que le package est active. Ils sont donc demarres ici aussi, en tolerant
l'echec, puisque les liens garantissent le boot suivant de toute facon.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

DEPLOY = "/opt/nivuus/media-manager"
UNIT_DIR = "etc/systemd/system"

TIMERS = [
    "media-manager-reset-error.timer",
    "media-manager-update-wanted.timer",
    "media-manager-cleanup.timer",
]

# Variable du .env -> config.xml qui porte la cle, relatif au deploiement.
HARVEST = {
    "RADARR_API_KEY": "radarr/config.xml",
    "SONARR_API_KEY": "sonarr/config.xml",
    "PROWLARR_API_KEY": "prowlarr/config.xml",
}

# Un service qui tire son image puis initialise sa base met des minutes, pas
# des secondes. Borne large, mais bornee : la phase dispose de 7200 s au total.
HARVEST_TIMEOUT = 600
HARVEST_INTERVAL = 10


def emit(event):
    print(json.dumps(event), flush=True)


def harvest_key(path):
    """La valeur de <ApiKey> dans un config.xml d'*arr, "" si indisponible.

    Ne leve jamais : un fichier absent signifie « le service n'a pas fini de
    demarrer », et un XML tronque « il ecrivait pendant qu'on lisait ». Les
    deux sont des etats transitoires que l'appelant doit reessayer, pas des
    erreurs qui doivent faire echouer l'activation.
    """
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return ""
    node = root.find("ApiKey")
    return (node.text or "").strip() if node is not None else ""


def fill_env(text, keys):
    """Le .env, avec les seules valeurs VIDES renseignees.

    Une valeur deja presente n'est jamais ecrasee : en production le .env
    porte les cles reelles, et les remplacer par ce qu'un conteneur
    fraichement demarre a genere casserait les trois scripts de maintenance.
    Une recolte infructueuse ("") ne touche rien non plus.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key in keys and keys[key] and not value.strip():
                lines.append(f"{key}={keys[key]}")
                continue
        lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def arm(root, unit, wants):
    """Lier une unite dans son repertoire .wants. Idempotent.

    Leve FileNotFoundError si l'unite est absente : un lien pendant est pire
    que pas de lien, parce qu'il se lit comme « armee ».
    """
    unit_path = os.path.join(root, UNIT_DIR, unit)
    if not os.path.isfile(unit_path):
        raise FileNotFoundError(unit_path)

    wants_dir = os.path.join(root, UNIT_DIR, wants)
    os.makedirs(wants_dir, exist_ok=True)
    link = os.path.join(wants_dir, unit)

    target = f"/{UNIT_DIR}/{unit}"
    if os.path.islink(link) and os.readlink(link) == target:
        return
    if os.path.lexists(link):
        os.remove(link)      # un fichier ordinaire ici est le bug, pas un etat
    os.symlink(target, link)


def compose(*args):
    """Docker Compose dans le repertoire de deploiement."""
    return subprocess.run(["docker", "compose", *args], cwd=DEPLOY,
                          capture_output=True, text=True)


def start_units(units):
    """Recharger systemd et demarrer les unites. Ne leve jamais.

    systemctl est legitimement inutilisable en environnement contraint, et
    chaque unite est deja liee — le prochain demarrage est correct avec ou
    sans ceci.
    """
    failed = []
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    for unit in units:
        proc = subprocess.run(["systemctl", "start", unit],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            failed.append(unit)
    return failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--root", default="/")
    args = parser.parse_args()
    json.load(sys.stdin)          # le contexte est lu, rien n'en depend ici
    root = args.root.rstrip("/") or "/"

    emit({"event": "progress", "pct": 10, "msg": "Demarrage de la mediatheque"})
    proc = compose("up", "-d")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        print(f"media-manager activate: docker compose up a echoue "
              f"({tail[-1] if tail else 'pas de stderr'})", file=sys.stderr)
        return 1

    emit({"event": "progress", "pct": 50, "msg": "Recolte des cles API"})
    deadline = time.time() + HARVEST_TIMEOUT
    keys = {}
    while time.time() < deadline:
        keys = {var: harvest_key(os.path.join(DEPLOY, rel))
                for var, rel in HARVEST.items()}
        if all(keys.values()):
            break
        time.sleep(HARVEST_INTERVAL)

    missing = sorted(var for var, value in keys.items() if not value)
    if missing:
        # Pas un echec : la pile tourne, et une cle manquante se renseigne a la
        # main. Mais jamais silencieux — les timers concernes ne feront rien.
        emit({"event": "progress", "pct": 70,
              "msg": "Cles non recoltees, a renseigner a la main : "
                     + ", ".join(missing)})

    env_path = os.path.join(DEPLOY, ".env")
    with open(env_path) as fh:
        filled = fill_env(fh.read(), keys)
    with open(env_path, "w") as fh:
        fh.write(filled)
    os.chmod(env_path, 0o600)

    emit({"event": "progress", "pct": 85, "msg": "Armement des timers"})
    for timer in TIMERS:
        arm(root, timer, "timers.target.wants")
    failed = start_units(TIMERS)
    if failed:
        emit({"event": "progress", "pct": 95,
              "msg": "Timers lies mais non demarres, actifs au prochain "
                     "redemarrage : " + ", ".join(failed)})

    emit({"event": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Lancer le test pour vérifier qu'il passe**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager && python3 tests/test_activate_hook.py
```

Attendu : `test_activate_hook: OK`.

- [ ] **Step 5: Commit**

```bash
git add hooks/activate.py tests/test_activate_hook.py
git commit -m "feat(package): hook activate — demarre, recolte les cles, arme les timers

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Suite de tests, documentation du package et câblage installer

**Files:**
- Create: `Makefile`
- Rewrite: `README.md`
- Modify: `CLAUDE.md`
- Modify: `/home/mallanic/Projects/Nivuus/packages/installer/installer/README.md`
- Modify: `/home/mallanic/Projects/Nivuus/packages/installer/CLAUDE.md`

**Interfaces:**
- Consumes: les quatre suites `tests/test_*.py` (Tasks 3, 4, 5, 6, 7).
- Produces: `make test` à la racine du package, sur le modèle de `console/Makefile`.

- [ ] **Step 1: Écrire le Makefile**

Créer `Makefile` :

```makefile
# Package Nivuus media-manager — cible de test.
#
# Les suites sont des scripts Python autonomes, pas du pytest : c'est le style
# du depot installer, et il ne demande rien d'autre que python3 + PyYAML.
#
# NIVUUS_INSTALLER_DIR fait valider le manifeste par le VRAI parseur du moteur
# (installer/packages/manifest.py) au lieu de la reverification locale. C'est
# la verification qui fait autorite ; la locale existe pour que le depot reste
# testable seul.
#   make test NIVUUS_INSTALLER_DIR=$$HOME/Projects/Nivuus/packages/installer

PACKAGE_DIR := $(CURDIR)
PYTHON ?= python3

.PHONY: test help

help:
	@grep -E '^[a-zA-Z_-]+:.*' $(MAKEFILE_LIST) | sed 's/:.*//' | sort

test:
	@for t in test_compose_portable test_manifest_contract \
	          test_maintenance_units test_install_hook test_activate_hook; do \
	    echo "--- $$t"; \
	    $(PYTHON) $(PACKAGE_DIR)/tests/$$t.py || exit 1; \
	done
```

- [ ] **Step 2: Lancer la suite complète**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
make test
make test NIVUUS_INSTALLER_DIR=/home/mallanic/Projects/Nivuus/packages/installer
```

Attendu : cinq suites `OK`, deux fois.

- [ ] **Step 3: Réécrire le README du package**

Remplacer `README.md`. Il doit couvrir, dans cet ordre : ce qu'est la médiathèque (les seize services et le flux de la demande à la bibliothèque, repris de l'ancien README), le fait que c'est **un package Nivuus** et ce que cela implique (`nivuus-package.yaml`, deux phases, `stack/` = répertoire de déploiement), comment l'installer via l'ISO :

````markdown
## Installation

Ce dépôt est un **package Nivuus** : l'installer l'embarque dans son ISO et
l'installe en deux phases.

```bash
cd ~/Projects/Nivuus/packages/installer/installer
PACKAGE_REPOS="$HOME/Projects/Nivuus/packages/media-manager" sudo -E make build-iso
```

L'export se fait par `git archive HEAD` : **seuls les fichiers commités
voyagent**.

| Phase | Quand | Ce qu'elle fait |
|---|---|---|
| `install` | Sur le système de fichiers cible | Dépose `stack/` en `/opt/nivuus/media-manager`, rend le `.env`, pose les six unités de maintenance |
| `activate` | Après le redémarrage, réseau disponible | `docker compose up -d`, récolte les clés API de Radarr/Sonarr/Prowlarr, arme les trois timers |

Sur une machine déjà installée, la phase `install` s'exécute directement :

```bash
echo '{"package":{},"hw":{},"answers":{"media_root":"/media/data","nvenc_node":false}}' \
  | sudo python3 hooks/install.py --phase install --root /
```

## Tests

```bash
make test
make test NIVUUS_INSTALLER_DIR=$HOME/Projects/Nivuus/packages/installer
```
````

- [ ] **Step 4: Mettre à jour le CLAUDE.md du package**

En tête de `CLAUDE.md`, après « Project Overview », insérer une section qui dit ce que la structure implique — c'est ce qu'une session future doit savoir avant de toucher quoi que ce soit :

```markdown
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
package et armées par le hook `activate`.
```

Puis corriger dans le reste du fichier toutes les occurrences de
`/opt/nivuus/MediaManager` et les chemins de scripts :

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
grep -n "MediaManager\|python3 reset-error\|python3 update_wanted\|python3 media_cleanup" CLAUDE.md
```

Remplacer chaque occurrence par le nouveau chemin, et la mention « Runs daily
at 06:00 via root crontab » par « Lancé quotidiennement à 06:00 par
`media-manager-reset-error.timer` ».

- [ ] **Step 5: Câbler le package dans la documentation de l'installer**

Dans `/home/mallanic/Projects/Nivuus/packages/installer/installer/README.md`, section `## Packages`, après le bloc `console`, ajouter :

````markdown
### `media-manager`, package tierce partie de référence

`~/Projects/Nivuus/packages/media-manager` est le premier package **hors de ce
dépôt** : seize conteneurs (Plex, la suite *arr, Tdarr, Bazarr) déposés en
`/opt/nivuus/media-manager` puis démarrés au premier boot.

```bash
PACKAGE_REPOS="$HOME/Projects/Nivuus/packages/media-manager" sudo -E make build-iso
```

Il n'a ni `resolve`, ni `claims`, ni `requires` : `tier: userspace`, deux
phases, et ses dépendances déclarées en `apt:`. C'est la démonstration que le
contrat suffit à un package qui ne touche pas la chaîne de démarrage.

Les deux packages s'embarquent ensemble, séparés par une espace :

```bash
PACKAGE_REPOS="$PWD/console $HOME/Projects/Nivuus/packages/media-manager" \
  sudo -E make build-iso
```
````

Dans `/home/mallanic/Projects/Nivuus/packages/installer/CLAUDE.md`, ajouter à
la table « Sibling repositories » la ligne :

```markdown
| `nivuus/media-manager` | Médiathèque (Plex, *arr, Tdarr) — package `nivuus.dev/v1`, tier `userspace` |
```

- [ ] **Step 6: Vérifier que les deux packages s'embarquent ensemble**

Sans construire l'ISO entière, rejouer la boucle d'export de `build.sh` :

```bash
cd /tmp && rm -rf pkgtest && mkdir pkgtest
for pkg in /home/mallanic/Projects/Nivuus/packages/installer/console \
           /home/mallanic/Projects/Nivuus/packages/media-manager; do
  test -f "$pkg/nivuus-package.yaml" || { echo "MANQUE: $pkg"; exit 1; }
  name=$(basename "$pkg"); mkdir -p "/tmp/pkgtest/$name"
  git -C "$pkg" archive HEAD | tar -x -C "/tmp/pkgtest/$name"
done
ls /tmp/pkgtest/media-manager/           # manifeste, wizard, hooks, stack, systemd
ls /tmp/pkgtest/media-manager/stack/     # docker-compose.yml et le reste
NIVUUS_PACKAGES_DIR=/tmp/pkgtest python3 -c "
import sys; sys.path.insert(0, '/home/mallanic/Projects/Nivuus/packages/installer/installer')
from packages.discovery import discover
from packages.conflicts import check_conflicts
manifests, errors = discover()
print('valides:', [m.name for m in manifests])
print('erreurs:', errors)
print('conflits:', check_conflicts(manifests))
"
```

Attendu : `valides: ['console', 'media-manager']`, `erreurs: []`, `conflits: []`.
Le résultat « conflits vides » est l'assertion qui compte : elle prouve que
l'absence de `claims:` garde les deux packages co-installables.

- [ ] **Step 7: Commit**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
git add Makefile README.md CLAUDE.md
git commit -m "docs(package): README, CLAUDE.md et cible make test

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"

cd /home/mallanic/Projects/Nivuus/packages/installer
git add installer/README.md CLAUDE.md
git commit -m "docs(packages): media-manager, package tierce partie de reference

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Bascule de la production

La médiathèque est coupée le temps de l'opération. 34 Go sur un seul système de fichiers : le déplacement est un `rename()`, instantané. Aucun volume nommé, tout est bind mount — il n'y a pas d'état Docker à perdre.

**Files:**
- Move: `/opt/nivuus/MediaManager` → `/opt/nivuus/media-manager`
- Modify: crontab root

**Interfaces:**
- Consumes: `hooks/install.py` (Task 5), `hooks/activate.py` (Task 7).

- [ ] **Step 1: Relever l'état de référence, pour pouvoir le comparer après**

```bash
cd /opt/nivuus/MediaManager
docker compose ps --format '{{.Name}}\t{{.Status}}' | sort > /tmp/avant-bascule.txt
cat /tmp/avant-bascule.txt | wc -l          # attendu: 12
cp .env /tmp/env-avant-bascule              # filet de securite : cles API reelles
crontab -l > /tmp/crontab-avant-bascule
```

- [ ] **Step 2: Arrêter la pile**

```bash
cd /opt/nivuus/MediaManager && docker compose down
docker ps --filter name=mediamanager --format '{{.Names}}'   # attendu: vide
```

- [ ] **Step 3: Renommer**

```bash
mv /opt/nivuus/MediaManager /opt/nivuus/media-manager
ls /opt/nivuus/media-manager/radarr/config.xml     # les donnees ont suivi
```

- [ ] **Step 4: Retirer la source, garder les données et le `.env`**

Le dépôt vit désormais dans `packages/media-manager` ; ce qui reste ici est un
déploiement, pas une copie de travail.

```bash
cd /opt/nivuus/media-manager
rm -rf .git .gitignore .claude .superpowers docs __pycache__ \
       CLAUDE.md README.md .env.example \
       docker-compose.yml docker-compose.yml.bak docker-compose.yml.avant-hardlinks \
       reset-error.py update_wanted.py media_cleanup.py scripts
ls .env                                    # CONSERVE : il porte les cles reelles
ls tdarr_cache radarr sonarr plex          # CONSERVES : ce sont les donnees
```

`tdarr/flows/`, `tdarr/bin/` et `tdarr/server/Tdarr/Plugins/` ne sont pas
supprimés : `tdarr/server/` contient aussi la base du serveur Tdarr. Le hook
`install` va les écraser par les versions du package, ce qui est voulu.

- [ ] **Step 5: Rejouer la phase install sur le système vivant**

Les réponses reprennent les valeurs réelles du `.env` conservé. Le hook ne
l'écrasera pas : il ajoutera les quatre variables nouvelles (`COMPOSE_FILE`,
`COMPOSE_PROFILES`, `VIDEO_GID`, `RENDER_GID`).

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
python3 - <<'PY' | sudo python3 hooks/install.py --phase install --root /
import json
print(json.dumps({
    "package": {"name": "media-manager", "version": "1.0.0"},
    "hw": {},
    "answers": {
        "media_root": "/media/data",
        "transcode_dir": "/media/backup/.transcode",
        "timezone": "Europe/Paris",
        "nvenc_node": True,
        "plex_claim": "", "ygg_username": "", "ygg_password": "", "tmdb_token": "",
    },
}))
PY
```

- [ ] **Step 6: Vérifier ce que le hook a écrit**

```bash
cd /opt/nivuus/media-manager
grep -E '^(RADARR_API_KEY|MEDIA_ROOT|COMPOSE_FILE|COMPOSE_PROFILES|VIDEO_GID|RENDER_GID)=' .env
stat -c '%a' .env                              # attendu: 600
ls /etc/systemd/system/media-manager-*         # attendu: 6 unites
docker compose config >/dev/null && echo "compose valide"
docker compose config | grep -c '/dev/dri'     # attendu: 4
```

`RADARR_API_KEY` doit porter la clé réelle d'avant la bascule — c'est
l'assertion qui prouve que le `.env` n'a pas été écrasé. La comparer à
`/tmp/env-avant-bascule` en cas de doute.

- [ ] **Step 7: Retirer les trois lignes de crontab**

```bash
crontab -l | grep -v '/opt/nivuus/MediaManager' | crontab -
crontab -l | grep -c MediaManager              # attendu: 0
crontab -l | wc -l                             # attendu: 3 de moins qu'avant
```

- [ ] **Step 8: Rejouer la phase activate**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
echo '{"package":{},"hw":{},"answers":{}}' | sudo python3 hooks/activate.py --phase activate --root /
```

- [ ] **Step 9: Vérifier que la médiathèque est revenue**

```bash
cd /opt/nivuus/media-manager
docker compose ps --format '{{.Name}}\t{{.Status}}' | sort > /tmp/apres-bascule.txt
diff /tmp/avant-bascule.txt /tmp/apres-bascule.txt   # seuls les uptimes different
systemctl list-timers 'media-manager-*' --all        # attendu: 3 timers actifs
curl -sf -o /dev/null -w '%{http_code}\n' http://localhost:7878   # attendu: 200 ou 401
curl -sf -o /dev/null -w '%{http_code}\n' http://localhost:8265   # Tdarr
```

- [ ] **Step 10: Vérifier qu'un job de maintenance tourne réellement**

C'est la seule preuve que la chaîne `.env` → `EnvironmentFile` → script
fonctionne. Un timer armé qui échoue à chaque déclenchement à 6h du matin est
exactement le mode de panne que cette étape existe pour exclure.

```bash
systemctl start media-manager-reset-error.service
systemctl status media-manager-reset-error.service --no-pager | head -20
journalctl -u media-manager-reset-error.service -n 40 --no-pager
```

Attendu : `status=0/SUCCESS`, et un journal qui montre des appels à Radarr et
Sonarr — pas une erreur d'authentification, qui signalerait que la clé API
n'est pas parvenue au script.

- [ ] **Step 11: Vérifier le comportement de `media_cleanup.py` sans clé Tautulli**

`TAUTULLI_API_KEY` et `OVERSEERR_API_KEY` ne sont pas récoltables. Sur cet
hôte elles sont déjà renseignées, donc le script tourne ; sur une
installation neuve elles seront vides.

```bash
cd /opt/nivuus/media-manager
env -u TAUTULLI_API_KEY TAUTULLI_API_KEY= python3 media_cleanup.py --help >/dev/null && echo "importable"
grep -n "TAUTULLI_API_KEY" media_cleanup.py | head
```

Si le script lève au lieu de se dégrader quand la clé est vide, **ne pas armer
`media-manager-cleanup.timer` par défaut** : le retirer de la liste `TIMERS` de
`hooks/activate.py`, ajouter le motif au test `tests/test_activate_hook.py`, et
documenter dans le README qu'il s'arme à la main une fois la clé renseignée.
Sinon, laisser en l'état.

- [ ] **Step 12: Commit**

```bash
cd /home/mallanic/Projects/Nivuus/packages/media-manager
git add -A
git commit -m "chore(prod): bascule de /opt/nivuus/MediaManager vers le package

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 13: Consigner le ménage restant, sans le faire**

Le renommage a emporté tel quel ce qui ne sert plus. Leur suppression est une
décision distincte, à prendre une fois la bascule éprouvée :

```bash
du -sh /opt/nivuus/media-manager/{unmanic,backup-fusion-4k-2026-08-18,overseerr-backup,aria2-config} 2>/dev/null
ls /opt/nivuus/media-manager/{zoogvpn.ovpn,aria2-watchdog.sh,migrate_tags.py} 2>/dev/null
```

Total attendu : environ 4,1 Go.

**Retour arrière**, tant que la bascule n'est pas validée :

```bash
cd /opt/nivuus/media-manager && docker compose down
mv /opt/nivuus/media-manager /opt/nivuus/MediaManager
cd /opt/nivuus/MediaManager
git -C /home/mallanic/Projects/Nivuus/packages/media-manager archive \
    --prefix='' HEAD stack | tar -x --strip-components=1
cp /tmp/env-avant-bascule .env
crontab /tmp/crontab-avant-bascule
docker compose down && docker compose up -d
```

---

## Self-review

**Couverture de la spec** — chaque section de la spec pointe vers une tâche :
structure → 2, manifeste → 4, wizard → 4, clés API → 7, compose (4 corrections)
→ 3, timers → 6, migration → 9, câblage ISO → 8. Le hook `install` (Task 5) et
le hook `activate` (Task 7) couvrent la table des trois phases.

**Correction apportée à la spec** — la spec annonçait une récolte des clés dans
« Radarr, Sonarr, Prowlarr **et Bazarr** ». Bazarr ne range pas la sienne dans
un `config.xml` : la récolte se limite aux trois *arr, et `BAZARR_API_KEY`
reste à renseigner à la main comme `TAUTULLI_API_KEY` et `OVERSEERR_API_KEY`.
Aucun script de maintenance n'utilise la clé de Bazarr, donc rien n'en dépend.

**Dépendance croisée assumée** — la Task 5 a besoin des six unités de la Task 6
pour que son test passe. Elle le dit et renvoie aux steps concernés plutôt que
de les dupliquer. Exécuter 6 avant 5 est également valide.

**Cohérence des noms** — `harvest_key`, `fill_env` et `arm` portent le même nom
dans `hooks/activate.py` et dans `tests/test_activate_hook.py` ;
`merge_env`, `group_gid`, `bool_answer` et `text_answer` n'existent que dans
`hooks/install.py`, vérifiées par artefacts. Les six noms d'unités sont
identiques dans `hooks/install.py` (`UNITS`), `hooks/activate.py` (`TIMERS`,
qui n'en liste que trois — les timers), `systemd/` et les deux suites de test.
Les huit clés de `wizard.yaml` correspondent une à une aux lectures de
`hooks/install.py`.
