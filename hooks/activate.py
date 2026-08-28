#!/usr/bin/env python3
"""Phase activate du package media-manager : demarrer et cabler la pile.

Trois choses, dans cet ordre, parce que chacune depend de la precedente.

1. DEMARRER LA PILE. C'est ici, et pas en phase install, parce qu'il faut le
   reseau : quinze images doivent etre tirees.

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
