#!/usr/bin/env python3
"""Le hook install, verifie par ses ARTEFACTS sous une racine temporaire.

Jamais par des appels simules : tout l'interet de ce hook est ce qu'il laisse
sur le systeme de fichiers cible, et un test qui simulerait la copie ne
prouverait rien a son sujet.

Les deux cas qui ont motive le plus de code sont ici : un .env existant n'est
JAMAIS ecrase (en production il porte les cles API reelles et les identifiants
Plex), et une reponse booleenne recue sous forme de chaine est refusee, jamais
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
    # 0600 : le fichier porte plex_claim en clair, et recevra les cles API
    # a la phase activate.
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
    check("secret reporte", values["PLEX_CLAIM"], "claim-abc")
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
