#!/usr/bin/env python3
"""The activate hook: pure functions, artifacts, and its docker/compose
failure and success paths under a fake subprocess.run.

What is tested without a real Docker: reading an API key from an *arr's
config.xml, filling the .env, arming timers by symlink, and run_phase()'s
prerequisite/compose paths (docker missing, `config`/`ps`/`up` failing, and
one full success run) with a fake subprocess.run that dispatches on argv,
the same way maintenance_fakes.FakeApi dispatches on (method, url). A real
`docker compose up` against a real Docker daemon is still left to the
production cutover — simulating a container actually starting would prove
nothing — but every other path through this section, success included, is
exactly what this suite must prove: a compose failure used to be silently
swallowed into "nothing to create" or "no container exists" (see
ActivationError in activate.py), and the success path is what catches a
mutant that makes `up` fail unconditionally instead.

La regle qui compte : fill_env ne remplit que les valeurs VIDES. En production
le .env porte deja les cles reelles, et les ecraser par ce qu'un conteneur
fraichement demarre a genere casserait les trois scripts de maintenance.

Run: python3 tests/test_activate_hook.py
"""
import importlib.util
import pathlib
import stat
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
# activate.py imports its sibling atomic_env.py the same way the deployed
# stack/ scripts import their sibling maintenance package: by directory,
# because Python only puts a *directly run* script's own directory on
# sys.path automatically. exec_module() here is not a direct run.
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "tests"))

import activate_fakes  # noqa: E402

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

# --- The harvest writes the .env atomically, through write_env ------------
# activate.py must use the same atomic write as install.py (hooks/
# atomic_env.py): a harvest that changes nothing must leave the file
# completely untouched, same inode and mtime.
check("activate.py uses atomic_env's write_env",
      activate.write_env.__module__, "atomic_env")

with tempfile.TemporaryDirectory() as tmp:
    env_path = pathlib.Path(tmp) / ".env"
    env_path.write_text(ENV)
    before = env_path.stat()

    # A harvest that found nothing new (empty keys) changes nothing.
    unchanged = activate.fill_env(ENV, {"RADARR_API_KEY": "", "PROWLARR_API_KEY": ""})
    activate.write_env(str(env_path), unchanged)
    after = env_path.stat()
    check("no-op harvest: same inode", after.st_ino, before.st_ino)
    check("no-op harvest: same mtime", after.st_mtime_ns, before.st_mtime_ns)

    # A harvest that fills a key does change the file, in place (same path),
    # mode 0600.
    activate.write_env(str(env_path), filled)
    check("filled harvest: content written", env_path.read_text(), filled)
    check("filled harvest: mode 0600",
          stat.S_IMODE(env_path.stat().st_mode), 0o600)

# --- Ne jamais ressusciter un service arrete par quelqu'un d'autre --------
# La regression que ce bloc verrouille a ete commise en production le
# 2026-08-28 : un `docker compose up -d` global a relance le node CPU de Tdarr
# pendant une partie, alors que les hooks libvirt du package console venaient
# de l'arreter pour rendre les coeurs a la VM.
DECLARED = ["radarr", "sonarr", "tdarr", "tdarr-node", "tdarr-node-nvenc"]

# Installation neuve : aucun conteneur, tout est a creer.
check("installation neuve", activate.services_to_start(DECLARED, []), DECLARED)

# Les deux nodes ont un conteneur, arrete par les hooks libvirt : on n'y
# touche pas, meme s'ils ne tournent pas.
check("nodes arretes non ressuscites",
      activate.services_to_start(DECLARED, DECLARED), [])

# Un service ajoute au compose est cree, sans reveiller les nodes.
check("nouveau service seul",
      activate.services_to_start(DECLARED + ["bazarr"], DECLARED), ["bazarr"])

# L'ordre de declaration est conserve.
check("ordre conserve",
      activate.services_to_start(DECLARED, ["tdarr"]),
      ["radarr", "sonarr", "tdarr-node", "tdarr-node-nvenc"])

# --- The two maintenance timers are armed unconditionally ----------------
# media-manager-cleanup.timer is gone: Maintainerr replaced media_cleanup.py,
# and install.py retires the unit (hooks/retired.py).
check("the maintenance timers are armed",
      activate.TIMERS,
      ["media-manager-reset-error.timer", "media-manager-update-wanted.timer"])

# --- Armement des timers par symlink --------------------------------------
with tempfile.TemporaryDirectory() as root:
    units = pathlib.Path(root) / "etc/systemd/system"
    units.mkdir(parents=True)
    (units / "media-manager-reset-error.timer").write_text("[Timer]\n")

    activate.arm(root, "media-manager-reset-error.timer", "timers.target.wants")
    link = units / "timers.target.wants" / "media-manager-reset-error.timer"
    check("lien cree", link.is_symlink(), True)
    # La cible est un chemin ABSOLU dans l'espace de noms du systeme demarre,
    # pas dans la racine jetable : systemd le resout apres le redemarrage,
    # quand cette racine EST /.
    check("cible absolue", link.readlink().as_posix(),
          "/etc/systemd/system/media-manager-reset-error.timer")

    # Idempotent : rejouer la phase ne doit pas echouer.
    activate.arm(root, "media-manager-reset-error.timer", "timers.target.wants")
    check("idempotent", link.is_symlink(), True)

    # Un fichier ordinaire a la place du lien est le bug, pas un etat a
    # preserver : systemd ignore une entree .wants qui n'est pas un lien, avec
    # « is not a symlink, ignoring ». Neuf entrees dans ce cas existent deja
    # sur cet hote.
    link.unlink()
    link.write_text("pas un lien\n")
    activate.arm(root, "media-manager-reset-error.timer", "timers.target.wants")
    check("fichier ordinaire remplace", link.is_symlink(), True)

    # Une unite absente leve : un lien pendant se lit comme « armee » alors
    # que rien ne se declenchera jamais.
    try:
        activate.arm(root, "media-manager-fantome.timer", "timers.target.wants")
        failures.append("unite absente: aucune exception levee")
    except FileNotFoundError:
        pass

# --- run_phase()'s docker/compose failure paths, fake subprocess.run ------
# Each case isolates exactly ONE failing call; the fake dispatches on argv,
# like FakeApi dispatches on (method, url) in maintenance_fakes.py, and
# fails the test loudly on any call it was not told to expect. activate.DEPLOY
# is patched to a scratch directory for EVERY case (fix round 1, item 5):
# make test runs as root, DEPLOY is hardcoded to the real
# /opt/nivuus/media-manager, and nothing here may ever touch it.


completed = activate_fakes.completed
run_phase_with = activate_fakes.phase_runner(activate, REPO)


VERSION, CONFIG, PS_A = activate_fakes.VERSION, activate_fakes.CONFIG, activate_fakes.PS_A

# Docker itself is not installed: subprocess.run() raises, it does not
# return a non-zero completed process.
code, err = run_phase_with({VERSION: FileNotFoundError("[Errno 2] docker: not found")})
check("docker command missing: exit status", code, 1)
check("docker command missing: names the requirement",
      "compose v2 plugin is required" in err, True)

# Docker is installed but the compose v2 plugin is not.
code, err = run_phase_with({
    VERSION: completed(VERSION, returncode=1,
                       stderr="docker: 'compose' is not a docker command.\n"),
})
check("compose plugin missing: exit status", code, 1)
check("compose plugin missing: names the requirement",
      "compose v2 plugin is required" in err, True)
check("compose plugin missing: stderr detail kept",
      "is not a docker command" in err, True)

# `docker compose config --services` fails: must raise, not read as "nothing
# to create" (which used to make the phase report success regardless).
code, err = run_phase_with({
    VERSION: completed(VERSION),
    CONFIG: completed(CONFIG, returncode=1,
                      stderr="services.radarr.image: required\n"),
})
check("config fails: exit status", code, 1)
check("config fails: command named", "compose config" in err, True)
check("config fails: stderr kept", "services.radarr.image" in err, True)

# `docker compose ps -a --services` fails: must raise, not read as "no
# container exists" (which used to send every service through `up -d`,
# including ones the console package's hooks deliberately stopped).
code, err = run_phase_with({
    VERSION: completed(VERSION),
    CONFIG: completed(CONFIG, stdout="radarr\nsonarr\n"),
    PS_A: completed(PS_A, returncode=1,
                    stderr="Cannot connect to the Docker daemon\n"),
})
check("ps -a fails: exit status", code, 1)
check("ps -a fails: command named", "compose ps -a" in err, True)
check("ps -a fails: stderr kept", "Cannot connect to the Docker daemon" in err, True)

# `docker compose up -d ...` fails: must make the phase exit non-zero too,
# even when it is not a total failure (some services already exist).
UP = ("docker", "compose", "up", "-d", "sonarr", "tdarr-node-nvenc")
code, err = run_phase_with({
    VERSION: completed(VERSION),
    CONFIG: completed(CONFIG, stdout="radarr\nsonarr\ntdarr-node-nvenc\n"),
    PS_A: completed(PS_A, stdout="radarr\n"),
    UP: completed(UP, returncode=1,
                 stderr="Error response from daemon: could not select "
                        "device driver\n"),
})
check("up fails: exit status", code, 1)
check("up fails: command named", "compose up -d" in err, True)
check("up fails: stderr kept", "could not select device driver" in err, True)

# --- Full success path, so a mutant that raises on every `up` is caught ---
# (fix round 1, item 5). `up` is actually called and succeeds (todo is not
# empty), then the harvest loop and the timer arming both run for real,
# against the scratch DEPLOY and --root run_phase_with() sets up. The
# config.xml files are pre-filled so the harvest loop's `all(keys.values())`
# is true on its first pass — otherwise it would sleep for real, in a loop
# bounded by HARVEST_TIMEOUT (up to 600 s).
CONFIG_XML = activate_fakes.CONFIG_XML
UP_SUCCESS = ("docker", "compose", "up", "-d", "sonarr")
left = {}


def read_what_was_left(deploy, root):
    """The .env the harvest wrote, and each timers.target.wants entry -> its link target."""
    left["env"] = (deploy / ".env").read_text()
    wants = root / "etc/systemd/system/timers.target.wants"
    left["armed"] = {entry.name: str(entry.readlink()) if entry.is_symlink() else None
                     for entry in wants.iterdir()}


code, err = run_phase_with({
    VERSION: completed(VERSION),
    CONFIG: completed(CONFIG, stdout="radarr\nsonarr\n"),
    PS_A: completed(PS_A, stdout="radarr\n"),
    UP_SUCCESS: completed(UP_SUCCESS),
    ("systemctl", "daemon-reload"): completed(("systemctl", "daemon-reload")),
    ("systemctl", "start", "media-manager-reset-error.timer"):
        completed(("systemctl", "start", "media-manager-reset-error.timer")),
    ("systemctl", "start", "media-manager-update-wanted.timer"):
        completed(("systemctl", "start", "media-manager-update-wanted.timer")),
}, deploy_files={
    ".env": "RADARR_API_KEY=\nSONARR_API_KEY=\nPROWLARR_API_KEY=\n",
    "radarr/config.xml": CONFIG_XML.format("radarr-key"),
    "sonarr/config.xml": CONFIG_XML.format("sonarr-key"),
    "prowlarr/config.xml": CONFIG_XML.format("prowlarr-key"),
}, inspect=read_what_was_left)
check("success path: exit status", code, 0)
check("success path: no stderr", err, "")
check("success path: harvested keys written to the .env", left.get("env"),
      "RADARR_API_KEY=radarr-key\nSONARR_API_KEY=sonarr-key\n"
      "PROWLARR_API_KEY=prowlarr-key\n")
# Exactly these two links.
check("success path: reset-error and update-wanted armed",
      left.get("armed"), {
          "media-manager-reset-error.timer":
              "/etc/systemd/system/media-manager-reset-error.timer",
          "media-manager-update-wanted.timer":
              "/etc/systemd/system/media-manager-update-wanted.timer",
      })

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_activate_hook: OK")
