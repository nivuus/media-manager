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
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
STACK = REPO / "stack"
MAIN = STACK / "docker-compose.yml"
QSV = STACK / "docker-compose.qsv.yml"

# Les trois services qui consomment le rendu materiel Intel.
QSV_SERVICES = ("tdarr", "tdarr-node", "plex")

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

# La surcouche les monte, pour exactement les trois services concernes.
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

# SABnzbd est optionnel de la meme facon : eteint sur l'hote de reference,
# rallumable en ajoutant `usenet` a COMPOSE_PROFILES sans toucher au package.
check("profil de SABnzbd",
      main["services"]["sabnzbd"].get("profiles"), ["usenet"])

# Un service derriere un profil ne doit etre la cible d'aucun depends_on : un
# service actif qui depend d'un service non selectionne fait echouer le
# demarrage entier.
optional = {name for name, svc in main["services"].items() if svc.get("profiles")}
# A depends_on entry naming a service that is not declared is exactly what
# `docker compose config` refuses with "service ... depends on undefined
# service" — checked here, without running docker, so removing a service
# (e.g. the Whisper ASR one) can never leave a stale reference behind.
declared = set(main["services"])
for name, svc in main["services"].items():
    deps = svc.get("depends_on") or {}
    for dep in (deps if isinstance(deps, (list, dict)) else []):
        check(f"{name}: depends_on targets a declared service ({dep})",
              dep in declared, True)
        check(f"{name} ne depend pas du service optionnel {dep}",
              dep in optional, False)

# Toute variable que le wizard fait ecrire dans le .env doit etre CONSOMMEE
# par un service. PLEX_CLAIM ne l'etait pas : la question etait posee, la
# valeur ecrite, et aucun conteneur ne la lisait — un rattachement Plex
# silencieusement sans effet. Une variable orpheline est pire qu'une variable
# absente, parce qu'elle promet quelque chose.
WIZARD_VARS = ("MEDIA_ROOT", "DOWNLOADS_DIR", "MOVIES_DIR", "TV_DIR",
               "TRANSCODE_DIR", "TZ", "PUID", "PGID", "PLEX_CLAIM",
               "VIDEO_GID", "RENDER_GID")
referenced = MAIN.read_text() + QSV.read_text()
for var in WIZARD_VARS:
    check(f"{var} consomme par un service",
          "${" + var + "}" in referenced, True)

# Reciproquement, le gabarit doit declarer tout ce que le compose interpole,
# sinon docker compose substitue une chaine vide sans le dire.
template = (STACK / "env.template").read_text()
declared = {line.split("=", 1)[0].strip()
            for line in template.splitlines()
            if "=" in line and not line.strip().startswith("#")}
interpolated = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", referenced))
check("aucune variable interpolee absente du gabarit",
      sorted(interpolated - declared), [])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_compose_portable: OK")
