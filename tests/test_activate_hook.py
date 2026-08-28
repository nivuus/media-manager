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

# --- Le timer de menage n'est arme qu'avec une cle Tautulli ---------------
# media_cleanup.py sort en 1 quand TAUTULLI_API_KEY est vide, et le timer le
# lance sans --status : l'armer sans la cle donnerait une unite en echec tous
# les jours a 08:00. La cle n'est pas recoltable, elle se saisit a la main.
check("les trois timers avec la cle",
      activate.timers_to_arm({"TAUTULLI_API_KEY": "une-cle"}),
      activate.TIMERS)
check("menage non arme sans la cle",
      activate.timers_to_arm({"TAUTULLI_API_KEY": ""}),
      ["media-manager-reset-error.timer", "media-manager-update-wanted.timer"])
check("menage non arme si la cle est absente",
      activate.timers_to_arm({}),
      ["media-manager-reset-error.timer", "media-manager-update-wanted.timer"])

check("lecture d'un .env",
      activate.env_values("# note\nA=1\n\nB = deux \nC\n"),
      {"A": "1", "B": "deux"})

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
