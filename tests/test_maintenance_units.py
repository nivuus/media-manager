#!/usr/bin/env python3
"""Les unites de maintenance, verifiees comme des DIRECTIVES, pas des chaines.

Les unites systemd sont de l'INI : les analyser plutot que chercher dans le
texte brut est ce qui rend l'assertion vraie a propos d'une directive, et non
d'une ligne qui pourrait tres bien etre commentee.

strict=False parce que systemd tolere une cle repetee (la derniere gagne) la
ou configparser leve. optionxform=str parce que configparser met les cles en
minuscules alors que les directives systemd sont sensibles a la casse —
TimeoutStartSec deviendrait timeoutstartsec.

Run: python3 tests/test_maintenance_units.py
"""
import configparser
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
UNITS = REPO / "systemd"
STACK = REPO / "stack"
DEPLOY = "/opt/nivuus/media-manager"

# job -> (script attendu, heure d'execution)
JOBS = {
    "reset-error": ("reset-error.py", "*-*-* 06:00:00"),
    "update-wanted": ("update_wanted.py", "*-*-* 07:00:00"),
    "cleanup": ("media_cleanup.py", "*-*-* 08:00:00"),
}

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def load_unit(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


for job, (script, calendar) in JOBS.items():
    service = load_unit(UNITS / f"media-manager-{job}.service")
    timer = load_unit(UNITS / f"media-manager-{job}.timer")

    check(f"{job}: oneshot", service["Service"]["Type"], "oneshot")
    exec_start = service["Service"]["ExecStart"]
    check(f"{job}: le script est lance depuis le deploiement",
          exec_start.startswith(f"/usr/bin/python3 {DEPLOY}/{script}"), True)
    # Le script existe vraiment dans la pile : cette assertion attrape un
    # renommage cote stack/ que rien d'autre ne verrait avant le premier
    # declenchement du timer, a 6h du matin.
    check(f"{job}: {script} present dans stack/",
          (STACK / script).is_file(), True)
    check(f"{job}: environnement charge",
          service["Service"]["EnvironmentFile"], f"{DEPLOY}/.env")
    check(f"{job}: repertoire de travail",
          service["Service"]["WorkingDirectory"], DEPLOY)
    # Les trois scripts parlent aux conteneurs : sans docker, ils echouent.
    check(f"{job}: ordonne apres docker",
          "docker.service" in service["Unit"]["After"], True)

    check(f"{job}: horaire", timer["Timer"]["OnCalendar"], calendar)
    # Persistent : une machine eteinte a 6h doit rattraper au demarrage, sinon
    # les telechargements en erreur s'accumulent jusqu'au lendemain.
    check(f"{job}: rattrapage apres extinction",
          timer["Timer"]["Persistent"], "true")
    check(f"{job}: cible d'armement",
          timer["Install"]["WantedBy"], "timers.target")

# Le nettoyage journalise, comme la ligne de crontab qu'il remplace.
cleanup = load_unit(UNITS / "media-manager-cleanup.service")
check("cleanup: journal conserve",
      f"--log {DEPLOY}/cleanup.log" in cleanup["Service"]["ExecStart"], True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_maintenance_units: OK")
