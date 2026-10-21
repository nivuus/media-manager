#!/usr/bin/env python3
"""The generated Tdarr flow is a sound graph, and its guards cannot be bypassed.

The flow is JSON wired by hand in build_flow_v6.py; a misplaced edge does not
fail anywhere until a file takes that path in production. This walks the
committed flow and checks the properties the flow is built on:

  - every edge joins two existing nodes, every node is reachable from the input;
  - no path reaches an ffmpeg execution without first setting aside the
    subtitle tracks ffmpeg cannot read (it would fail on them);
  - every path from an ffmpeg execution to Replace Original File goes through
    the subtitle restore check, the duration guard and the size guard;
  - the committed JSON is what the build script produces.

Run: python3 tests/test_tdarr_flow.py
"""
import json
import pathlib
import subprocess
import sys

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parents[1]
FLOW = REPO / "stack" / "tdarr" / "flows" / "OLED4K_norm_v6.json"
BUILD = REPO / "stack" / "scripts" / "tdarr" / "build_flow_v6.py"
LOCAL = REPO / "stack" / "tdarr" / "server" / "Tdarr" / "Plugins" / "FlowPlugins" / "LocalFlowPlugins"

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def paths(succ, start, stop):
    """Every simple path from start, cut at the first node for which stop() holds."""
    stack = [[start]]
    while stack:
        path = stack.pop()
        node = path[-1]
        if len(path) > 1 and stop(node):
            yield path
            continue
        nexts = [n for n in succ.get(node, []) if n not in path]
        if not nexts:
            yield path
        for n in nexts:
            stack.append(path + [n])


flow = json.loads(FLOW.read_text())
nodes = {n["id"]: n for n in flow["flowPlugins"]}
plugin = {nid: n["pluginName"] for nid, n in nodes.items()}
succ = {}
for e in flow["flowEdges"]:
    check(f"edge {e['id']} source exists", e["source"] in nodes, True)
    check(f"edge {e['id']} target exists", e["target"] in nodes, True)
    succ.setdefault(e["source"], []).append(e["target"])

starts = [nid for nid, p in plugin.items() if p == "inputFile"]
check("one input node", len(starts), 1)
seen, todo = set(), list(starts)
while todo:
    nid = todo.pop()
    if nid not in seen:
        seen.add(nid)
        todo.extend(succ.get(nid, []))
check("unreachable nodes", sorted(set(nodes) - seen), [])

for nid, n in nodes.items():
    if n["sourceRepo"] == "Local":
        check(f"{nid}: Local plugin {n['pluginName']} exists",
              any(LOCAL.glob(f"*/{n['pluginName']}/{n['version']}/index.js")), True)

# Before ffmpeg: from every command start, the subtitles are set aside before execution.
for start in [nid for nid, p in plugin.items() if p == "ffmpegCommandStart"]:
    for path in paths(succ, start, lambda nid: plugin[nid] == "ffmpegCommandExecute"):
        if plugin[path[-1]] == "ffmpegCommandExecute":
            check(f"{' -> '.join(path)} sets subtitles aside",
                  any(plugin[nid] == "setAsideUnreadableSubtitles" for nid in path), True)

# After ffmpeg: nothing replaces the original without the restore check and both guards.
GUARDS = ["checkFlowVariable", "compareFileDurationRatio", "compareFileSizeRatio"]
reached = 0
for execute in [nid for nid, p in plugin.items() if p == "ffmpegCommandExecute"]:
    for path in paths(succ, execute, lambda nid: plugin[nid] in ("replaceOriginalFile", "ffmpegCommandStart")):
        if plugin[path[-1]] != "replaceOriginalFile":
            continue
        reached += 1
        on_path = [plugin[nid] for nid in path if plugin[nid] in GUARDS]
        check(f"{' -> '.join(path)} guards in order", on_path, GUARDS)
check("some execution reaches Replace Original File", reached > 0, True)

# The restore step really is mkvmerge reading the set-aside track IDs from the original.
merge = [n for n in nodes.values() if n["pluginName"] == "runCli"]
check("one mkvmerge restore step", len(merge), 1)
if merge:
    args = merge[0]["inputsDB"]["cliArguments"]
    check("restore reads the original", "{{{args.originalLibraryFile._id}}}" in args, True)
    check("restore takes the set-aside tracks",
          "--subtitle-tracks {{{args.variables.user.unreadableSubtitleTracks}}}" in args, True)

built = subprocess.run([sys.executable, str(BUILD)], capture_output=True, text=True)
check("build script runs", built.returncode, 0)
check("committed flow is the built one", json.loads(FLOW.read_text()) == flow, True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_tdarr_flow: OK")
