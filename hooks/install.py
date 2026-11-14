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
   sont pas touchees. Two exceptions: a profile the answers turn on is
   added to COMPOSE_PROFILES, never removed (add_profiles); turning
   translation on fills its endpoint, model and key where they are empty,
   never where they are set (fill_empty_values).

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

4. A GIT CHECKOUT DEPLOYS ONLY WHAT GIT TRACKS. `git ls-files -z stack/`
   picks the file list instead of a raw directory walk, so an untracked
   stack/.env or a service's runtime config.xml sitting in the working tree
   is never shipped. A `git archive` export (build.sh) has no .git and
   already went through the commit filter at export time, so it is copied
   as the whole tree, exactly as before. `.git` as a worktree's own pointer
   FILE (not a directory) still counts as a checkout. The copy itself (and
   its symlink hardening: no symlinked source, no writing through a
   symlinked destination component) lives in safe_copy.py, next to it.
"""
import argparse
import json
import os
import sys

from atomic_env import write_env
from data_dirs import ensure_data_dirs
from retired import retire_stack_files, retire_units
from safe_copy import copy_stack
from subtitles import PROFILE as SUBTITLE_PROFILE, translation_env
from units import (FAILURE_DROPIN_NAME, UNIT_REL_DIR, UNITS, _is_our_dropin,
                   place_unit, sync_failure_dropins)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STACK = os.path.join(HERE, "stack")

DEST_REL = "opt/nivuus/media-manager"

# The identity the stack's services run as; rendered into the .env and used
# to own the data directories of services that do not run as root.
PUID = 1000
PGID = 1000
TEMPLATE_NAME = "env.template"

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


def add_profiles(text, wanted):
    """The .env with every profile in `wanted` present in COMPOSE_PROFILES.

    The one exception to "an existing value is never touched" (rule 1): a
    profile the wizard's answers turn on is ADDED to an existing
    COMPOSE_PROFILES. Without it, recording `subtitle_translation=true` on an
    installed host rendered every SUBTITLE_* value and then silently never
    started Lingarr. Nothing is ever removed: a profile enabled by hand (the
    documented way to turn SABnzbd back on) stays, and turning one off is
    still a hand edit. Only the last assignment is rewritten, since that is
    the one compose reads; quoting and every other line are left as they are.
    """
    lines = text.splitlines(keepends=True)
    index = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#") and stripped.partition("=")[0].strip() == "COMPOSE_PROFILES":
            index = i
    if index is None:
        return text
    key, _, value = lines[index].rstrip("\n").partition("=")
    value = value.strip()
    quote = value[0] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else ""
    current = [p.strip() for p in value.strip(quote).split(",") if p.strip()]
    missing = [p for p in wanted if p not in current]
    if not missing:
        return text
    newline = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"{key}={quote}{','.join(current + missing)}{quote}{newline}"
    return "".join(lines)


def fill_empty_values(text, values):
    """The .env with each key of `values` filled where its value is EMPTY.

    The second exception to rule 1, for values a fresh install renders empty
    on purpose: the translation endpoint, model and key are always rendered
    since 1.4.0, empty while translation is off, so merge_env() alone left
    them empty when it was turned on later. Only the last assignment counts
    (it is the one compose reads); a non-empty value, or an empty answer,
    changes nothing. Quoting and every other line are kept.
    """
    lines = text.splitlines(keepends=True)
    last = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            last[stripped.partition("=")[0].strip()] = i
    for key, wanted in values.items():
        if not wanted or key not in last:
            continue
        index = last[key]
        name, _, value = lines[index].rstrip("\n").partition("=")
        value = value.strip()
        quote = value[0] if len(value) == 2 and value[0] == value[1] and value[0] in "'\"" else ""
        if value and not quote:
            continue
        newline = "\n" if lines[index].endswith("\n") else ""
        lines[index] = f"{name}={quote}{wanted}{quote}{newline}"
    return "".join(lines)


def env_identity(text):
    """(uid, gid) that compose will run non-root services as, from a .env text.

    Last assignment wins, as in a dotenv file; one pair of matching quotes is
    stripped. Anything but plain decimal digits is refused by name: a wrong
    owner would not fail here, the container would fail to write later.
    """
    found = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            found[key.strip()] = value.strip()
    ids = []
    for key in ("PUID", "PGID"):
        value = found.get(key, "")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not (value.isascii() and value.isdigit()):
            raise ValueError(
                f"{key} in .env must be a non-negative integer, got {value!r}"
                if key in found else f"{key} is missing from .env")
        ids.append(int(value))
    return ids[0], ids[1]


def render_env(values):
    with open(os.path.join(STACK, TEMPLATE_NAME)) as fh:
        text = fh.read()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    return text




def install(root, answers):
    """Deploy the stack under root, render its .env and place the units."""
    # Valider AVANT de deposer le premier octet : un appelant et le package en
    # desaccord sur le contrat est une erreur, pas quelque chose a contourner
    # au milieu d'une copie. A failing git-tracked-files lookup (rule 4) must
    # fail just as loudly, and just as early: falling back to a raw copy is
    # exactly what would ship an untracked .env or config.xml.
    nvenc = bool_answer(answers, "nvenc_node")
    usenet = bool_answer(answers, "usenet")
    media_root = text_answer(answers, "media_root", "/media/data").rstrip("/")
    transcode = text_answer(answers, "transcode_dir",
                            "/media/backup/.transcode")
    timezone = text_answer(answers, "timezone", "Europe/Paris")
    plex_claim = text_answer(answers, "plex_claim")
    subtitle_translation = bool_answer(answers, "subtitle_translation")
    subtitle_env = translation_env(
        subtitle_translation,
        text_answer(answers, "subtitle_translation_endpoint"),
        text_answer(answers, "subtitle_translation_model"),
        text_answer(answers, "subtitle_translation_api_key"),
        text_answer(answers, "subtitle_source_languages", "en"),
        text_answer(answers, "subtitle_target_languages", "fr"))

    dest = os.path.join(root, DEST_REL)

    emit({"event": "progress", "pct": 20, "msg": "Depose de la pile"})
    copy_stack(HERE, STACK, dest)

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
    if subtitle_translation:
        profiles.append(SUBTITLE_PROFILE)

    rendered = render_env({
        "TZ": timezone,
        "PUID": str(PUID),
        "PGID": str(PGID),
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
        **subtitle_env,
    })

    env_path = os.path.join(dest, ".env")
    if os.path.isfile(env_path):
        with open(env_path) as fh:
            rendered = add_profiles(merge_env(fh.read(), rendered), profiles)
        if subtitle_translation:
            rendered = fill_empty_values(rendered, {key: subtitle_env[key] for key in (
                "SUBTITLE_TRANSLATION_ENDPOINT", "SUBTITLE_TRANSLATION_MODEL",
                "SUBTITLE_TRANSLATION_API_KEY")})
        emit({"event": "progress", "pct": 70,
              "msg": ".env existant conserve, variables manquantes ajoutees"})
    # Docker would create these as root, unwritable by a non-root service.
    # Owned by the identity the EFFECTIVE .env gives compose (`user:
    # ${PUID}:${PGID}`): an existing .env keeps its own values through the
    # merge, so the PUID/PGID constants are only what a fresh .env renders.
    # Done before write_env: a malformed PUID/PGID or an unusable data dir
    # fails the install before the .env is touched.
    uid, gid = env_identity(rendered)
    ensure_data_dirs(dest, uid, gid)

    # Atomic: the Plex claim token and, soon, the API keys live here. A
    # reinstall with unchanged values must not even touch the file (same
    # inode and mtime), and mode 0600 is guaranteed by write_env itself.
    write_env(env_path, rendered)

    emit({"event": "progress", "pct": 85, "msg": "Unites de maintenance posees"})
    unit_dir = os.path.join(root, UNIT_REL_DIR)
    for unit in UNITS:
        if not place_unit(unit, unit_dir):
            emit({"event": "progress", "pct": 85,
                  "msg": f"{os.path.join(unit_dir, unit)}: masked by the operator"
                         " (a symlink), left alone"})
    for path in sync_failure_dropins(root):
        emit({"event": "progress", "pct": 85,
              "msg": f"{path}: a symlink, left alone"})
    for path in (retire_units(unit_dir, FAILURE_DROPIN_NAME, _is_our_dropin)
                 + retire_stack_files(dest)):
        emit({"event": "progress", "pct": 90, "msg": f"Retired: {path}"})

    emit({"event": "done"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--root", default="/")
    args = parser.parse_args()

    ctx = json.load(sys.stdin)
    answers = ctx.get("answers") or {}
    root = args.root.rstrip("/") or "/"

    # Every failure, before or after the copy, ends here with one clean,
    # named error and exit 1. OSError covers the git binary itself being
    # missing and any write failure (permissions, disk full, a file where
    # a directory belongs, ...) in the copy, the .env, the units or their
    # drop-ins: a bare crash is not "loud", it is a traceback instead.
    try:
        install(root, answers)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"media-manager install: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
