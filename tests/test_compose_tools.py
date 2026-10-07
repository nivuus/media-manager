#!/usr/bin/env python3
"""The tool services included by docker-compose.yml (Recyclarr, Lingarr),
and the Recyclarr configuration they ship.

test_compose_portable.py covers docker-compose.yml and its QSV overlay; the
included files are checked here, with the same rules: every variable they
interpolate is declared by the env template, every wizard variable is read
by a service, and nothing depends on an optional service.

Run: python3 tests/test_compose_tools.py
"""
import json
import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
STACK = REPO / "stack"
MAIN = yaml.safe_load((STACK / "docker-compose.yml").read_text())

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# --- The includes ----------------------------------------------------------
INCLUDES = ["docker-compose.recyclarr.yml", "docker-compose.lingarr.yml"]
check("included files", MAIN.get("include"), INCLUDES)
included = {}
for name in INCLUDES:
    included.update(yaml.safe_load((STACK / name).read_text())["services"])
check("included services", sorted(included), ["lingarr", "recyclarr"])
check("no service declared twice", set(included) & set(MAIN["services"]), set())

texts = "".join((STACK / name).read_text() for name in INCLUDES)
template = (STACK / "env.template").read_text()
declared_vars = {line.split("=", 1)[0].strip() for line in template.splitlines()
                 if "=" in line and not line.strip().startswith("#")}
check("every interpolated variable is in the template",
      sorted(set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", texts)) - declared_vars), [])
for var in ("SUBTITLE_TRANSLATION_ENDPOINT", "SUBTITLE_TRANSLATION_MODEL",
            "SUBTITLE_TRANSLATION_API_KEY", "SUBTITLE_SOURCE_LANGUAGES",
            "SUBTITLE_TARGET_LANGUAGES"):
    check(f"{var} read by a service", "${" + var + "}" in texts, True)

every = {**MAIN["services"], **included}
for name, svc in included.items():
    check(f"{name}: auto-update label",
          svc.get("labels", {}).get("com.centurylinklabs.watchtower.enable"), True)
    check(f"{name}: runs as the stack user", svc.get("user"), "${PUID}:${PGID}")
    for dep in svc.get("depends_on") or []:
        check(f"{name}: depends on a declared service ({dep})", dep in every, True)
optional = {name for name, svc in every.items() if svc.get("profiles")}
for name, svc in every.items():
    for dep in svc.get("depends_on") or []:
        check(f"{name} does not depend on optional {dep}", dep in optional, False)

# --- Recyclarr ----------------------------------------------------------------
recyclarr = included["recyclarr"]
check("recyclarr: image pinned to its major",
      recyclarr["image"], "ghcr.io/recyclarr/recyclarr:8")
check("recyclarr: config volume", recyclarr["volumes"], ["./recyclarr:/config"])

# --- Lingarr -----------------------------------------------------------------
lingarr = included["lingarr"]
check("lingarr: optional", lingarr.get("profiles"), ["lingarr"])
check("lingarr: loopback only", lingarr.get("ports"), ["127.0.0.1:9876:9876"])
check("lingarr: reaches host services",
      lingarr.get("extra_hosts"), ["host.docker.internal:host-gateway"])
# Lingarr opens the paths Radarr/Sonarr report as-is: same inner paths as
# their single /data mount, and the libraries only, never Downloads.
check("lingarr: volumes", lingarr["volumes"], [
    "./lingarr:/app/config", "${MOVIES_DIR}:/data/Movies", "${TV_DIR}:/data/TV Shows"])
env = dict(item.split("=", 1) for item in lingarr["environment"])
check("lingarr: auth off (see the compose comment)", env.get("AUTH_ENABLED"), "false")
check("lingarr: no telemetry", env.get("TELEMETRY_ENABLED"), "false")

# --- The Recyclarr configuration ---------------------------------------------
class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("!env_var", lambda loader, node: ("env", loader.construct_scalar(node)))
config = yaml.load((STACK / "recyclarr" / "recyclarr.yml").read_text(), Loader=Loader)
settings = yaml.safe_load((STACK / "recyclarr" / "settings.yml").read_text())
providers = {p["service"]: p["path"] for p in settings["resource_providers"]}

for service in ("radarr", "sonarr"):
    instance = config[service][service]
    check(f"{service}: key from the environment", instance["api_key"],
          ("env", f"{service.upper()}_API_KEY"))
    folder = STACK / "recyclarr" / providers[service].removeprefix("/config/")
    files = {}
    for path in sorted(folder.glob("*.json")):
        doc = json.loads(path.read_text())
        check(f"{path.name}: fields", sorted(doc), sorted(
            ["trash_id", "name", "includeCustomFormatWhenRenaming", "specifications"]))
        files[doc["trash_id"]] = doc["name"]
    used = [tid for entry in instance["custom_formats"] for tid in entry["trash_ids"]]
    check(f"{service}: each format synced once", len(used), len(set(used)))
    # Our own formats carry a nivuus- id and ship as JSON here; anything else is
    # a TRaSH guide format, whose ids are 32 hex digits. A file nobody references
    # would be synced by nothing: a format believed managed that is not.
    local = [tid for tid in used if tid.startswith(f"nivuus-{service}-")]
    check(f"{service}: referenced local formats == shipped formats", sorted(local), sorted(files))
    guide = [tid for tid in used if tid not in local]
    check(f"{service}: guide formats are TRaSH ids",
          [tid for tid in guide if not re.fullmatch(r"[0-9a-f]{32}", tid)], [])
    profiles = [p["name"] for p in instance["quality_profiles"]]
    check(f"{service}: both profiles managed", profiles, ["1080p", "4K"])
    for entry in instance["custom_formats"]:
        targets = [a["name"] for a in entry["assign_scores_to"]]
        check(f"{service} {entry['trash_ids']}: scored in both profiles",
              targets, profiles)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_compose_tools: OK")
