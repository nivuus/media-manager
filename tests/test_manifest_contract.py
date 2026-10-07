#!/usr/bin/env python3
"""Le manifeste et le wizard, verifies contre le contrat nivuus.dev/v1.

Le package est un depot autonome : il ne peut pas importer packages/manifest.py
du depot installer. Les regles du contrat sont donc reverifiees ici, a
l'identique, pour qu'une erreur soit vue au commit et pas a la construction de
l'ISO. Quand NIVUUS_INSTALLER_DIR est defini, le vrai parseur du moteur est
utilise en plus — c'est la verification qui fait autorite.

Run: python3 tests/test_manifest_contract.py
"""
import os
import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Le vocabulaire ferme du wizard : six types, pas un de plus.
QUESTION_TYPES = ("bool", "choix", "texte", "secret", "disque", "gpu")
# Les cles que seul un package de tier `platform` peut declarer.
PLATFORM_KEYS = ("kernel-cmdline", "modules", "hugepages-mib")

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


manifest = yaml.safe_load((REPO / "nivuus-package.yaml").read_text())

check("apiVersion", manifest.get("apiVersion"), "nivuus.dev/v1")
check("nom conforme", bool(NAME_RE.match(manifest.get("name", ""))), True)
check("nom", manifest.get("name"), "media-manager")
check("version conforme", bool(VERSION_RE.match(manifest.get("version", ""))), True)
check("tier", manifest.get("tier"), "userspace")
check("libelle non vide", bool((manifest.get("label") or "").strip()), True)

# tier userspace : declarer une cle plateforme fait REFUSER le manifeste par le
# moteur, ce n'est pas ignore silencieusement.
platform = manifest.get("platform") or {}
for key in PLATFORM_KEYS:
    check(f"aucune cle plateforme: {key}", key in platform, False)

# Aucun claim : media-manager et console doivent rester co-installables.
# console reclame le GPU en exclusivite ; un claim identique ici les rendrait
# mutuellement exclusifs, soit l'inverse de la production.
check("aucun claim", manifest.get("claims") or {}, {})

# Les dependances sont declarees en apt, pas deleguees a une feature du moteur :
# c'est ce qui garde l'installation autonome sur une Debian ordinaire possible.
requires = manifest.get("requires") or {}
check("aucune feature requise", requires.get("features") or [], [])
check("aucune capability requise", requires.get("capabilities") or [], [])

# Docker is deliberately NOT in this list: `docker-compose-v2` does not exist
# as a Debian package name, and `docker.io` conflicts with docker-ce (apt
# would remove Docker CE from the reference host to satisfy it). Docker is a
# prerequisite that hooks/activate.py's ensure_docker() checks for instead.
check("liste apt exacte", manifest.get("apt"),
      ["python3-requests", "python3-dotenv"])

# Les hooks declares existent vraiment : un hook manquant fait echouer
# l'installation au moment ou il est lance, pas au moment ou il est declare.
hooks = manifest.get("hooks") or {}
check("phases declarees", sorted(hooks), ["activate", "install"])
for phase, rel in hooks.items():
    check(f"hook {phase} present", (REPO / rel).is_file(), True)

# Le wizard.
questions_rel = (manifest.get("wizard") or {}).get("questions")
check("fichier de questions declare", questions_rel, "wizard.yaml")
questions = yaml.safe_load((REPO / questions_rel).read_text())
check("les questions sont une liste", isinstance(questions, list), True)

keys = [q.get("key") for q in questions]
check("cles uniques", len(keys), len(set(keys)))
check("cles attendues", sorted(keys),
      sorted(["media_root", "transcode_dir", "timezone", "nvenc_node",
              "usenet", "plex_claim", "subtitle_translation",
              "subtitle_translation_endpoint", "subtitle_translation_model",
              "subtitle_translation_api_key", "subtitle_source_languages",
              "subtitle_target_languages"]))

for question in questions:
    key = question.get("key")
    check(f"{key}: type dans le vocabulaire",
          question.get("type") in QUESTION_TYPES, True)
    check(f"{key}: libelle non vide",
          bool((question.get("label") or "").strip()), True)
    # Un secret ne porte jamais de defaut : le portail ne le renverrait pas.
    if question.get("type") == "secret":
        check(f"{key}: pas de defaut sur un secret",
              "default" in question, False)

# media_root est du texte, PAS du type `disque`. Le type `disque` designe un
# peripherique bloc reclame en exclusivite et declenche cote moteur le refus
# « ce disque est la cible d'installation ». La mediatheque vit sur un systeme
# de fichiers existant qu'elle ne reclame pas.
by_key = {q["key"]: q for q in questions}
check("media_root est du texte", by_key["media_root"]["type"], "texte")
check("media_root est requis", by_key["media_root"].get("required"), True)
check("nvenc_node est un booleen", by_key["nvenc_node"]["type"], "bool")
check("usenet est un booleen", by_key["usenet"]["type"], "bool")
check("subtitle_translation is a boolean, off by default",
      (by_key["subtitle_translation"]["type"],
       by_key["subtitle_translation"].get("default")), ("bool", False))
# The translation key may protect a paid endpoint: never echoed back.
check("subtitle_translation_api_key is a secret",
      by_key["subtitle_translation_api_key"]["type"], "secret")

# Verification faisant autorite, quand le depot installer est disponible.
installer_dir = os.environ.get("NIVUUS_INSTALLER_DIR")
if installer_dir:
    sys.path.insert(0, os.path.join(installer_dir, "installer"))
    from packages.manifest import load_manifest
    from packages.wizard import load_questions
    parsed = load_manifest(str(REPO / "nivuus-package.yaml"))
    check("moteur: nom", parsed.name, "media-manager")
    check("moteur: tier", parsed.tier, "userspace")
    loaded = load_questions(str(REPO / "wizard.yaml"))
    check("moteur: nombre de questions", len(loaded), len(questions))
else:
    print("NIVUUS_INSTALLER_DIR non defini : verification moteur sautee")

# The release source `nivuus update` follows: removing or misspelling it
# would silently stop the package from ever being updated.
check("release source", manifest.get("source"), {"github": "nivuus/media-manager"})
if os.environ.get("NIVUUS_INSTALLER_DIR"):
    check("engine parser: release source",
          load_manifest(str(REPO / "nivuus-package.yaml")).source.github, "nivuus/media-manager")

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_manifest_contract: OK")
