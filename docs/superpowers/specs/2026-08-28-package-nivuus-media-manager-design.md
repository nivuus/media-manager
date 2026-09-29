# MediaManager en package Nivuus — design

**Date** : 2026-08-28
**Statut** : validé, prêt pour le plan d'implémentation

## Objectif

Sortir MediaManager de `/opt/nivuus/MediaManager` — où il est à la fois le
dépôt source et le répertoire de production — et en faire un **package Nivuus**
sous `~/Projects/Nivuus/packages/media-manager`, installable par l'installer au
même titre que `console`.

Deux natures cohabitent aujourd'hui dans le même dossier :

| | Contenu | Volume |
|---|---|---|
| **Source** | 18 fichiers suivis : compose, 3 scripts de maintenance, assets Tdarr, docs | ~200 Ko |
| **État** | 20 répertoires de configuration de conteneurs (`radarr/`, `plex/`, `tdarr/server/`…) | 34 Go |

Le package ne porte que la première ; la seconde reste sur l'hôte et survit à
la bascule.

## Le contrat que le package doit respecter

`installer/packages/` implémente `nivuus.dev/v1`. Ce que le moteur attend, et
qui contraint tout ce qui suit :

- un `nivuus-package.yaml` **à la racine du dépôt** — `build.sh` saute tout
  répertoire qui n'en a pas ;
- l'export vers l'ISO se fait par `git archive HEAD` : **seuls les fichiers
  suivis par git voyagent**. Un fichier oublié dans `.gitignore` n'existe pas
  pour l'installer ;
- trois phases facultatives, `resolve` (avant toute écriture, lecture seule),
  `install` (écrit sous `--root`), `activate` (après le redémarrage, réseau
  disponible) ; chaque hook lit un contexte JSON sur stdin et émet du jsonl sur
  stdout ;
- `tier: userspace` interdit `kernel-cmdline`, `modules` et `hugepages-mib` —
  le parseur du manifeste refuse le fichier, il ne l'ignore pas ;
- la découverte se fait sous `/opt/nivuus-packages/*/` (surchargeable par
  `NIVUUS_PACKAGES_DIR`).

## Structure

```
packages/media-manager/
├── nivuus-package.yaml
├── wizard.yaml
├── hooks/
│   ├── install.py
│   └── activate.py
├── stack/                          ⟵ copié tel quel vers le répertoire de déploiement
│   ├── docker-compose.yml
│   ├── docker-compose.qsv.yml
│   ├── env.template
│   ├── reset-error.py
│   ├── update_wanted.py
│   ├── media_cleanup.py
│   ├── scripts/tdarr/
│   └── tdarr/{flows,bin,server/…/FlowPlugins/…}
├── systemd/
│   ├── media-manager-reset-error.{service,timer}
│   ├── media-manager-update-wanted.{service,timer}
│   └── media-manager-cleanup.{service,timer}
├── docs/
├── CLAUDE.md · README.md · LICENSE · .gitignore
```

**`stack/` est le répertoire de déploiement, à l'octet près.** `install.py` se
réduit alors à un `copytree` suivi du rendu du `.env` : il n'y a pas de liste
de fichiers à tenir à jour, donc pas de fichier qu'on oublie d'ajouter en même
temps qu'un service. Les bind mounts relatifs (`./radarr:/config`) restent
valides sans retouche parce que la racine du sous-arbre devient la racine du
déploiement.

Le déploiement va en `/opt/nivuus/media-manager` (minuscules, aligné sur le
nom du package, cohérent avec `/opt/nivuus-packages/media-manager`).

### Pas de hook `resolve`

Le package est `userspace` : il ne déclare ni paramètre noyau, ni module, ni
hugepage. Il n'y a donc rien à résoudre. Et refuser une machine sans `/dev/dri`
serait faux : Plex et Tdarr transcodent en logiciel, la médiathèque fonctionne
— moins vite. L'absence d'accélération est traitée en `install` (voir
`docker-compose.qsv.yml`), pas par un refus.

## Manifeste

```yaml
apiVersion: nivuus.dev/v1
name: media-manager
version: 1.0.0
label: "Médiathèque (Plex, *arr, Tdarr)"
tier: userspace

apt:
  - docker.io
  - docker-compose-v2
  - python3-requests
  - python3-dotenv

wizard:
  questions: wizard.yaml

hooks:
  install: hooks/install.py
  activate: hooks/activate.py
```

Trois décisions portées par ce fichier :

**`requires.features` est vide, `docker.io` est déclaré en `apt`.** C'est le
précédent posé par `console` avec `firewalld` : un package déclare ses propres
dépendances plutôt que de se coupler à la liste de features du moteur, sinon
l'installation autonome sur une Debian ordinaire — que le contrat existe
précisément pour permettre — casse. Si l'opérateur a coché la feature `docker`,
l'`apt-get install` est un no-op idempotent.

**Aucun `claims:`.** Tdarr utilise `/dev/dri` (QSV Intel) et un second node
NVENC qui **partage** la RTX 4070 avec la VM Windows, via les hooks libvirt du
package `console`. Déclarer `claims: {gpu: exclusive}` ferait entrer
media-manager en conflit avec `console` dans `check_conflicts()` et les rendrait
mutuellement exclusifs — soit exactement l'inverse de la production actuelle.

**Aucune `requires.capabilities`.** Le vocabulaire du moteur (`iommu`,
`gpu-discrete`, `nvme-dedicated`, `cpu-hybrid`) ne contient rien que la
médiathèque exige. Aucun n'est un prérequis : les demander masquerait le
package sur des machines où il marche.

## Wizard

```yaml
- key: media_root      # texte, requis, défaut /media/data
- key: transcode_dir   # texte, défaut /media/backup/.transcode
- key: timezone        # texte, défaut Europe/Paris
- key: nvenc_node      # bool, défaut false
- key: plex_claim      # secret, optionnel
- key: ygg_username    # texte, optionnel
- key: ygg_password    # secret, optionnel
- key: tmdb_token      # secret, optionnel
```

`DOWNLOADS_DIR`, `MOVIES_DIR` et `TV_DIR` sont **dérivés** de `media_root`
(`$MEDIA_ROOT/Downloads`, `/Movies`, `/TV Shows`) plutôt que demandés : ce sont
les trois sous-répertoires du même point de montage, et les séparer est ce qui
a cassé les hardlinks une première fois (voir `CLAUDE.md`, « Critical Path
Mappings »).

`media_root` est de type `texte`, **pas `disque`**. Le type `disque` désigne un
périphérique bloc qu'un package réclame en exclusivité, et déclenche côté
moteur le refus « ce disque est la cible d'installation ». La médiathèque vit
sur un système de fichiers existant qu'elle ne réclame pas.

PUID/PGID sont figés à 1000/1000 : l'utilisateur primaire créé par l'installer.

### Les clés API ne peuvent pas être des questions

`RADARR_API_KEY` et consorts **n'existent pas** au moment du wizard : chaque
service les génère à son premier démarrage. Elles sont donc écrites vides par
`install.py`, puis **récoltées par `activate.py`** après le premier
`compose up -d`, en lisant `ApiKey` dans le `config.xml` que Radarr, Sonarr et
Prowlarr écrivent dans leur volume de configuration.

Sans cette récolte, les trois timers de maintenance tourneraient à vide
indéfiniment — armés, annoncés, et silencieusement inertes. Bazarr, Tautulli et
Seerr ne rangent pas la leur dans un `config.xml` : elles restent vides et
`media_cleanup.py` doit se dégrader proprement (**à vérifier à
l'implémentation** — si le script lève au lieu de se dégrader, son timer n'est
pas armé tant que la clé manque).

## Modifications du compose

Quatre corrections, toutes rendues nécessaires par le fait que le compose cesse
d'être propre à une machine.

**1. Nom de projet épinglé — `name: mediamanager`.** Aujourd'hui le nom de
projet dérive du nom du répertoire ; le renommage en `media-manager`
orphelinerait les douze conteneurs en cours. Épingler l'ancienne valeur rend
l'identité indépendante du chemin, définitivement.

**2. Trois chemins absolus supprimés.** `/opt/nivuus/MediaManager/tdarr_cache:/temp`
apparaît aux lignes 264, 315 et 366 (serveur Tdarr et deux nodes). Il devient
`./tdarr_cache:/temp`. En l'état, ces trois lignes casseraient au renommage —
et le cache Tdarr est le facteur limitant du transcodage (déplacé HDD→NVMe le
2026-07-27 pour cette raison), donc l'échec serait une perte de débit, pas une
erreur bruyante.

**3. GID matériels résolus, plus codés en dur.** `group_add: ["44", "105"]`
suppose `video=44` et `render=105`. `video=44` est stable sur Debian, `render`
ne l'est pas (104, 105 ou 106 selon la version). `install.py` lit
`{root}/etc/group` et écrit `VIDEO_GID`/`RENDER_GID` dans le `.env` ; le compose
les référence.

**4. Accélération matérielle isolée dans `docker-compose.qsv.yml`.** Les blocs
`devices: [/dev/dri:/dev/dri]` et `group_add` des quatre services concernés
(tdarr, tdarr-node, plex, whisper) sortent du fichier principal. `install.py`
n'écrit `COMPOSE_FILE=docker-compose.yml:docker-compose.qsv.yml` dans le `.env`
que si `/dev/dri` existe sur la cible. Sur une machine sans iGPU, les quatre
services démarrent au lieu d'échouer au montage d'un périphérique absent.

Le node NVENC (`tdarr-node-nvenc`) passe sous `profiles: [nvenc]`, activé par
`COMPOSE_PROFILES=nvenc` quand la réponse `nvenc_node` est vraie.

## Maintenance : cron → timers systemd

Trois lignes de la crontab root pointent sur `/opt/nivuus/MediaManager` et
mourraient au renommage :

```
0 6 * * *  python3 /opt/nivuus/MediaManager/reset-error.py
0 7 * * *  python3 /opt/nivuus/MediaManager/update_wanted.py
0 8 * * *  python3 /opt/nivuus/MediaManager/media_cleanup.py --log …/cleanup.log
```

Elles deviennent trois paires `.service`/`.timer` livrées par le package,
`OnCalendar=*-*-* 06:00:00` / `07:00:00` / `08:00:00`, avec
`EnvironmentFile=/opt/nivuus/media-manager/.env` et
`WorkingDirectory=/opt/nivuus/media-manager`.

L'armement se fait **par symlink** dans `timers.target.wants/`, jamais par
`systemctl enable` : la règle est déjà établie dans `console/hooks/activate.py`
— `systemctl` échoue silencieusement en environnement contraint, un symlink
existe ou lève.

## Migration de la production

34 Go sur un seul système de fichiers (`/dev/mapper/nivuus--vg-root`) : le
déplacement est un `rename()`, instantané. Aucun volume nommé, tout est bind
mount — il n'y a pas d'état Docker à perdre.

1. **Committer les ~1200 lignes en cours** sur `tdarr-flow-v6` (9 fichiers :
   réécritures de `reset-error.py`, `update_wanted.py`, `media_cleanup.py`, plus
   documentation). Elles ne transitent pas par un `stash` — 20 commits ne sont
   pas encore poussés et l'ensemble doit survivre au clone.
2. `git clone` local vers `packages/media-manager` : les deux branches et les
   20 commits suivent. `origin` repointé sur `nivuus/media-manager`, aligné sur
   les autres dépôts sibling (`mqtt`, `desk`, `marketplace`, `shell`).
3. Restructuration par `git mv` (l'historique par fichier est préservé) et
   ajout de la couche package. `.gitignore` réécrit : les chemins ignorés
   passent sous `stack/`.
4. Vérification **hors production** : `install.py --root` sur une racine
   jetable, manifeste validé par le moteur (`NIVUUS_PACKAGES_DIR`), suite
   `make test-packages` de l'installer.
5. Bascule : arrêt de la stack → `mv MediaManager media-manager` → suppression
   du `.git` et des fichiers source désormais portés par le package →
   `hooks/install.py --root /` en réutilisant le `.env` courant comme source des
   valeurs → retrait des trois lignes de cron, armement des timers →
   `compose up -d` → vérification des douze conteneurs.
6. `PACKAGE_REPOS` documenté dans `installer/README.md` et `installer/docs/claude/package-engine.md`,
   table des dépôts sibling mise à jour.

## Hors périmètre

**Le ménage des résidus non suivis** que le renommage emporte tels quels :
`unmanic/` (856 Mo, service retiré du compose), `backup-fusion-4k-2026-08-18/`
(940 Mo), `overseerr-backup/` (2,3 Go), `zoogvpn.ovpn`, `aria2-config/`,
`aria2-watchdog.sh`, `migrate_tags.py`, `docker-compose.yml.bak`,
`docker-compose.yml.avant-hardlinks`. Ils sont listés ici pour mémoire ; leur
suppression est une décision distincte, prise après la bascule.

**Le câblage du package dans le portail.** Le wizard web n'offre encore aucun
package (lacune nommée dans `installer/docs/claude/package-engine.md`, 2026-08-27) : la sélection
passe par un `config.json` portant `packages: {"media-manager": {…}}`.
media-manager sera offert par le portail en même temps que `console`, quand
cette lacune sera comblée — ce n'est pas un travail propre à ce package.
