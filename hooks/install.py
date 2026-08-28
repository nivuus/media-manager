#!/usr/bin/env python3
"""Phase install du package media-manager : deposer la pile sur la cible.

Le sous-arbre stack/ EST le repertoire de deploiement, a l'octet pres, donc
la depose est une copie recursive — il n'y a pas de liste de fichiers a tenir
a jour, et donc pas de fichier qu'on oublie d'ajouter en meme temps qu'un
service.

TROIS REGLES PORTENT LE RESTE.

1. LE .env N'EST JAMAIS ECRASE. En production il porte les cles API reelles,
   et le jeton de rattachement Plex. Une reinstallation qui le
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
        usenet = bool_answer(answers, "usenet")
        media_root = text_answer(answers, "media_root", "/media/data").rstrip("/")
        transcode = text_answer(answers, "transcode_dir",
                                "/media/backup/.transcode")
        timezone = text_answer(answers, "timezone", "Europe/Paris")
        plex_claim = text_answer(answers, "plex_claim")
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

    # docker compose lit COMPOSE_PROFILES comme une liste separee par des
    # virgules ; une chaine vide n'active aucun profil.
    profiles = []
    if nvenc:
        profiles.append("nvenc")
    if usenet:
        profiles.append("usenet")

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
        "COMPOSE_PROFILES": ",".join(profiles),
        "VIDEO_GID": str(group_gid(root, "video")),
        "RENDER_GID": str(group_gid(root, "render")),
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
    # 0600 : le jeton de rattachement Plex, et bientot les cles API.
    os.chmod(env_path, 0o600)

    emit({"event": "progress", "pct": 85, "msg": "Unites de maintenance posees"})
    unit_dir = os.path.join(root, UNIT_REL_DIR)
    for unit in UNITS:
        place_unit(unit, unit_dir)

    emit({"event": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
