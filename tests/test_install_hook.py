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
import importlib.util
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
from unittest import mock

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
    "usenet": False,
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
    check("profil NVENC seul", values["COMPOSE_PROFILES"], "nvenc")
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

# --- COMPOSE_PROFILES reflete les deux reponses optionnelles --------------
# docker compose lit une liste separee par des virgules ; une chaine vide
# n'active aucun profil, donc ni le node NVENC ni SABnzbd ne demarrent.
for nvenc, usenet, expected in (
        (True, True, "nvenc,usenet"),
        (False, True, "usenet"),
        (False, False, ""),
):
    with tempfile.TemporaryDirectory() as root:
        fake_group_file(root)
        proc = run(root, dict(ANSWERS, nvenc_node=nvenc, usenet=usenet))
        check(f"profils (nvenc={nvenc}, usenet={usenet}): code", proc.returncode, 0)
        values = env_values(pathlib.Path(root) / DEST_REL / ".env")
        check(f"profils (nvenc={nvenc}, usenet={usenet})",
              values["COMPOSE_PROFILES"], expected)

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

# --- A reinstall with unchanged effective content touches nothing ---------
# write_env() (hooks/atomic_env.py) must skip the write entirely when the
# rendered content already matches: same inode, same mtime. Running install
# twice with identical answers is the case that exercises it end to end.
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    proc = run(root, ANSWERS)
    check("reinstall setup: exit status", proc.returncode, 0)
    env_path = pathlib.Path(root) / DEST_REL / ".env"
    before = env_path.stat()

    proc = run(root, ANSWERS)
    check("reinstall: exit status", proc.returncode, 0)
    after = env_path.stat()
    check("reinstall: same inode (file untouched)", after.st_ino, before.st_ino)
    check("reinstall: same mtime (file untouched)",
          after.st_mtime_ns, before.st_mtime_ns)
    check("reinstall: no leftover temp file",
          sorted(p.name for p in (pathlib.Path(root) / DEST_REL).glob(".env*")),
          [".env"])

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

# --- OnFailure= drop-in: only when the host has systemd-failure-notify@ ----
# The three deployed media-manager-*.service units carried a hand-added
# OnFailure=systemd-failure-notify@%n.service line on the reference host,
# added by hand because a reinstall drops it. systemd-failure-notify@.service
# belongs to the host, not to this package, so the drop-in must only
# reference it when it actually exists there.
FAILURE_NOTIFY_UNIT = "systemd-failure-notify@.service"
DROPIN_NAME = "10-on-failure.conf"
DROPIN_CONTENT = "[Unit]\nOnFailure=systemd-failure-notify@%n.service\n"
MAINTENANCE_SERVICES = (
    "media-manager-reset-error.service",
    "media-manager-update-wanted.service",
    "media-manager-cleanup.service",
)


def dropin_path(root, unit):
    return pathlib.Path(root) / "etc/systemd/system" / f"{unit}.d" / DROPIN_NAME


def fake_failure_notify(root):
    path = pathlib.Path(root) / "etc/systemd/system" / FAILURE_NOTIFY_UNIT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Unit]\nDescription=host-provided\n")


with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    fake_failure_notify(root)
    proc = run(root, ANSWERS)
    check("failure-notify present: exit status", proc.returncode, 0)
    for unit in MAINTENANCE_SERVICES:
        path = dropin_path(root, unit)
        check(f"failure-notify present: drop-in written for {unit}",
              path.is_file(), True)
        check(f"failure-notify present: drop-in content for {unit}",
              path.read_text(), DROPIN_CONTENT)
    # Only the three .service units get a drop-in, never the .timer units.
    for unit in ("media-manager-reset-error.timer",
                "media-manager-update-wanted.timer",
                "media-manager-cleanup.timer"):
        check(f"failure-notify present: no drop-in for {unit}",
              dropin_path(root, unit).exists(), False)

# --- No systemd-failure-notify@ on the host: no drop-in at all -------------
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    proc = run(root, ANSWERS)
    check("failure-notify absent: exit status", proc.returncode, 0)
    for unit in MAINTENANCE_SERVICES:
        check(f"failure-notify absent: no drop-in for {unit}",
              dropin_path(root, unit).exists(), False)

# --- A previous install's drop-in is removed once the host unit is gone ---
# "and nothing else": a sibling drop-in an operator added by hand in the same
# directory must survive.
with tempfile.TemporaryDirectory() as root:
    fake_group_file(root)
    stale = dropin_path(root, "media-manager-reset-error.service")
    stale.parent.mkdir(parents=True)
    stale.write_text(DROPIN_CONTENT)
    sibling = stale.parent / "20-operator-added.conf"
    sibling.write_text("# kept\n")

    proc = run(root, ANSWERS)
    check("failure-notify removed: exit status", proc.returncode, 0)
    check("failure-notify removed: stale drop-in gone", stale.exists(), False)
    check("failure-notify removed: sibling file untouched",
          sibling.read_text(), "# kept\n")

# --- A git checkout deploys only what git tracks under stack/ -------------
# build.sh exports via `git archive HEAD`, so an export already went through
# the commit filter and is copied as-is. A checkout has not: copying the
# whole subtree would ship whatever sits there uncommitted, most often a
# real .env or a service's runtime config.xml. This fixture is a SEPARATE,
# self-contained package directory (its own hooks/, systemd/, stack/, .git)
# so HERE resolves inside it when its own copy of install.py runs, and the
# real repository is never touched.
def build_git_checkout(pkg):
    shutil.copytree(REPO / "hooks", pkg / "hooks")
    shutil.copytree(REPO / "systemd", pkg / "systemd")
    (pkg / "stack").mkdir()
    shutil.copy2(REPO / "stack" / "env.template", pkg / "stack" / "env.template")
    (pkg / "stack" / "docker-compose.yml").write_text("services: {}\n")

    def git(*args):
        proc = subprocess.run(["git", *args], cwd=str(pkg),
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    git("init", "-q")
    git("add", "stack/env.template", "stack/docker-compose.yml")
    git("-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "tracked files")

    # Untracked: must never reach the deployment.
    (pkg / "stack" / ".env").write_text("RADARR_API_KEY=leaked-untracked-key\n")
    (pkg / "stack" / "radarr").mkdir()
    (pkg / "stack" / "radarr" / "config.xml").write_text("<Config/>\n")


with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    build_git_checkout(pkg)
    fake_group_file(root)

    proc = subprocess.run(
        [sys.executable, str(pkg / "hooks" / "install.py"),
         "--phase", "install", "--root", root],
        input=context(ANSWERS), capture_output=True, text=True, cwd=str(pkg))
    check("checkout: exit status", proc.returncode, 0)

    dest = pathlib.Path(root) / DEST_REL
    check("checkout: tracked file deployed",
          (dest / "docker-compose.yml").is_file(), True)
    check("checkout: untracked config.xml not deployed",
          (dest / "radarr" / "config.xml").exists(), False)
    # The rendered .env is always written; the proof the untracked stack/.env
    # was never copied (and therefore never merge_env()-preserved) is that
    # its content never reaches the deployed one.
    check("checkout: untracked .env content not deployed",
          "leaked-untracked-key" in (dest / ".env").read_text(), False)

# --- A git-archive export (no .git) still deploys the whole tree ----------
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    shutil.copytree(REPO / "hooks", pkg / "hooks")
    shutil.copytree(REPO / "systemd", pkg / "systemd")
    (pkg / "stack").mkdir()
    shutil.copy2(REPO / "stack" / "env.template", pkg / "stack" / "env.template")
    (pkg / "stack" / "docker-compose.yml").write_text("services: {}\n")
    # No .git anywhere: this is what `git archive HEAD | tar -x` produces.
    fake_group_file(root)

    proc = subprocess.run(
        [sys.executable, str(pkg / "hooks" / "install.py"),
         "--phase", "install", "--root", root],
        input=context(ANSWERS), capture_output=True, text=True, cwd=str(pkg))
    check("archive export: exit status", proc.returncode, 0)
    dest = pathlib.Path(root) / DEST_REL
    check("archive export: file deployed",
          (dest / "docker-compose.yml").is_file(), True)

# --- The git command's exact argv is pinned (Ruling 18) --------------------
# git 2.47 on the reference host refuses a repository it does not own when
# run as root ("detected dubious ownership": the checkout is the operator's,
# install runs as root), unless safe.directory names it explicitly. The real
# git binary still runs here (a transparent spy, not a mock) — only the argv
# it was called with is pinned.
sys.path.insert(0, str(REPO / "hooks"))
_spec = importlib.util.spec_from_file_location("install_hook_argv", HOOK)
install_hook_argv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_hook_argv)

_real_run = subprocess.run
_git_calls = []


def _spy_run(argv, **kwargs):
    _git_calls.append(list(argv))
    return _real_run(argv, **kwargs)


with mock.patch("subprocess.run", new=_spy_run):
    install_hook_argv.git_tracked_stack_files(str(REPO))

check("git argv pinned", _git_calls, [
    ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO),
     "ls-files", "-z", "stack/"],
])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_install_hook: OK")
