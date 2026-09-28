#!/usr/bin/env python3
"""Phase activate du package media-manager : demarrer et cabler la pile.

Trois choses, dans cet ordre, parce que chacune depend de la precedente.

1. DEMARRER LA PILE — mais SEULEMENT les services qui n'ont aucun conteneur.
   C'est ici, et pas en phase install, parce qu'il faut le reseau : quatorze
   images doivent etre tirees.

   UN `docker compose up -d` GLOBAL EST FAUX ICI, et l'erreur a ete commise :
   il redemarre aussi les conteneurs ARRETES, or les deux nodes Tdarr sont
   deliberement stoppes par les hooks libvirt du package console pendant une
   partie — le node CPU pour rendre les coeurs a la VM, le node NVENC parce
   qu'un conteneur tenant la carte empeche le bind vfio. Les relancer au
   milieu d'une partie est exactement ce que ces hooks existent pour eviter.
   Un service qui a deja un conteneur a donc un proprietaire ; cette phase ne
   le touche pas.

   Un echec partiel n'est pas fatal non plus : sur une machine ou la VM tient
   la carte, tdarr-node-nvenc ne PEUT pas demarrer (le socket
   nvidia-persistenced n'existe pas), et ce n'est pas une raison de declarer
   l'installation de la mediatheque en echec. Seul un demarrage ou RIEN ne
   tourne l'est.

2. HARVEST THE API KEYS. They do not exist yet at wizard time — each service
   generates its own on first start, inside its config volume's config.xml.
   Without this harvest, reset-error and update-wanted (the two timers this
   phase arms — see the TIMERS comment below on cleanup) would run
   indefinitely against an empty key: armed, announced, and silently inert.
   Only Radarr, Sonarr and Prowlarr can be harvested this way; Bazarr,
   Tautulli and Seerr keep theirs elsewhere and stay to be filled in by
   hand.

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

from atomic_env import write_env

DEPLOY = "/opt/nivuus/media-manager"
UNIT_DIR = "etc/systemd/system"

# media-manager-cleanup.timer is deliberately NOT armed here (audit H2):
# media_cleanup.py over-deletes and ranks re-requested titles first, and the
# unit is disabled on the reference host pending a replacement. Its unit
# files are still shipped by install.py, so an operator can still run it by
# hand, or arm it deliberately, once the flow is fixed.
TIMERS = [
    "media-manager-reset-error.timer",
    "media-manager-update-wanted.timer",
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


def compose_services(*args):
    """La liste de services rendue par une sous-commande compose. [] si echec."""
    proc = compose(*args)
    if proc.returncode != 0:
        return []
    return [name for name in (proc.stdout or "").split() if name]


def services_to_start(declared, existing):
    """Les services declares qui n'ont encore AUCUN conteneur.

    Un service qui a deja un conteneur — meme arrete — a un proprietaire, et
    ce n'est pas cette phase. Voir la regle 1 du docstring du module : les
    hooks libvirt du package console arretent les deux nodes Tdarr pendant
    une partie, et un `up -d` global les ressusciterait au milieu du jeu.

    L'ordre de `declared` est conserve : compose respecte les depends_on de
    toute facon, mais une liste stable rend la trace de progression lisible.
    """
    have = set(existing)
    return [name for name in declared if name not in have]


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
    todo = services_to_start(compose_services("config", "--services"),
                             compose_services("ps", "-a", "--services"))
    if todo:
        proc = compose("up", "-d", *todo)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            emit({"event": "progress", "pct": 40,
                  "msg": "Demarrage partiel : "
                         + (tail[-1][:160] if tail else "sans detail")})
            # Fatal seulement si RIEN ne tourne : un service qui ne peut pas
            # demarrer (la carte est a la VM) n'invalide pas la mediatheque.
            if not compose_services("ps", "--services"):
                print("media-manager activate: aucun service n'a demarre",
                      file=sys.stderr)
                return 1
    else:
        emit({"event": "progress", "pct": 40,
              "msg": "Tous les services ont deja un conteneur, rien a creer"})

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
    # Atomic: a harvest that changes nothing (every key already set) must
    # not touch the file at all, and mode 0600 is guaranteed by write_env.
    write_env(env_path, filled)

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
