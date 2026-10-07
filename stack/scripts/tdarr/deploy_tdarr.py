#!/usr/bin/env python3
"""Bring the live Tdarr server in line with this repository, through its API.

Usage: deploy_tdarr.py [--apply] [--url http://localhost:8266]   (dry run by default)

What it converges, idempotently:
  - the canonical flow (tdarr/flows/OLED4K_norm_v6.json) is upserted;
  - the production libraries run it;
  - test libraries (SAMPLE*) and every flow no library uses are removed;
  - node worker limits match NODE_LIMITS.

It talks to the running server instead of writing its SQLite file: the server
keeps flows, libraries and nodes in memory, so a direct database write is
either ignored or overwritten, and a copy of the database taken while it runs
is not a consistent backup. Tdarr takes its own daily backups (Backups/).

Files already processed by an older flow revision are NOT re-evaluated by
Tdarr when the flow changes; run requeue_outdated.py after deploying.
"""
import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FLOW = os.path.join(ROOT, "tdarr/flows/OLED4K_norm_v6.json")
CANONICAL = "OLED4K_norm_v6"
PRODUCTION_LIBRARIES = ("Movies", "TV Shows")
TEST_LIBRARY_PREFIX = "SAMPLE"

# Worker limits per node name, for nodes whose scheduler is off (with the
# scheduler on, the hourly schedule decides and these are not used).
# NvencNode: 2 transcode workers for its 2 CPUs (docker-compose `cpus: 2`).
# It ran 5, mostly full-file remuxes that only added an audio track, all
# contending for the same two cores.
NODE_LIMITS = {"NvencNode": {"transcodegpu": 2}}


class Tdarr:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def post(self, path, data):
        req = urllib.request.Request(
            f"{self.url}/api/v2/{path}", data=json.dumps({"data": data}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
        return json.loads(body) if body else None

    def get(self, path):
        with urllib.request.urlopen(f"{self.url}/api/v2/{path}", timeout=60) as resp:
            return json.loads(resp.read())

    def crud(self, collection, mode, doc_id=None, obj=None):
        data = {"collection": collection, "mode": mode}
        if doc_id is not None:
            data["docID"] = doc_id
        if obj is not None:
            data["obj"] = obj
        return self.post("cruddb", data)


def as_list(result):
    return list(result.values()) if isinstance(result, dict) else list(result or [])


def converge(api, apply):
    def do(label, action):
        print(("  " if apply else "  [dry] ") + label)
        if apply:
            action()

    with open(FLOW) as fh:
        flow = json.load(fh)
    if flow["_id"] != CANONICAL:
        raise SystemExit(f"unexpected flow id {flow['_id']!r} in {FLOW}")

    print("flow")
    do(f"upsert {CANONICAL} ({len(flow['flowPlugins'])} nodes)",
       lambda: api.crud("FlowsJSONDB", "insert" if api.crud("FlowsJSONDB", "getById", CANONICAL) is None
                        else "update", CANONICAL, flow))

    print("libraries")
    libraries = as_list(api.crud("LibrarySettingsJSONDB", "getAll"))
    names = {lib.get("name") for lib in libraries}
    missing = [n for n in PRODUCTION_LIBRARIES if n not in names]
    if missing:
        raise SystemExit(f"production libraries not found: {missing}")
    kept = []
    for lib in libraries:
        name = lib.get("name", "")
        if name.startswith(TEST_LIBRARY_PREFIX):
            do(f"remove test library {name!r}",
               lambda lib=lib: api.crud("LibrarySettingsJSONDB", "removeOne", lib["_id"]))
            continue
        kept.append(lib)
        if name in PRODUCTION_LIBRARIES:
            stale = (lib.get("flowId") != CANONICAL
                     or (lib.get("decisionMaker") or {}).get("flowId", CANONICAL) != CANONICAL)
            if stale:
                dm = dict(lib.get("decisionMaker") or {}, flowId=CANONICAL)
                do(f"{name}: flowId -> {CANONICAL}",
                   lambda lib=lib, dm=dm: api.crud("LibrarySettingsJSONDB", "update", lib["_id"],
                                                   {"flowId": CANONICAL, "decisionMaker": dm}))

    print("flows")
    used = {CANONICAL} | {lib.get("flowId") for lib in kept}
    for other in as_list(api.crud("FlowsJSONDB", "getAll")):
        if other["_id"] not in used:
            do(f"remove unused flow {other['_id']!r} ({other.get('name', '')})",
               lambda other=other: api.crud("FlowsJSONDB", "removeOne", other["_id"]))

    print("nodes")
    for node_id, node in api.get("get-nodes").items():
        wanted = NODE_LIMITS.get(node.get("nodeName"), {})
        for worker_type, target in wanted.items():
            current = node["workerLimits"][worker_type]
            step = "increase" if target > current else "decrease"
            for _ in range(abs(target - current)):
                do(f"{node['nodeName']}: {worker_type} {step} (-> {target})",
                   lambda node_id=node_id, worker_type=worker_type, step=step: api.post(
                       "alter-worker-limit",
                       {"nodeID": node_id, "workerType": worker_type, "process": step}))
            if target != current:
                # The live limit and the stored one are separate; keep both.
                limits = dict(node["workerLimits"], **{worker_type: target})
                do(f"{node['nodeName']}: persist {worker_type}={target}",
                   lambda name=node["nodeName"], limits=limits: api.crud(
                       "NodeJSONDB", "update", name, {"workerLimits": limits}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--url", default="http://localhost:8266")
    args = parser.parse_args()
    print("== APPLY ==" if args.apply else "== DRY RUN (add --apply to write) ==")
    converge(Tdarr(args.url), args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
