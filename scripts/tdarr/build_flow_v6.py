#!/usr/bin/env python3
"""Génère tdarr/flows/OLED4K_norm_v6.json (flow HDR/DV hybride, chaînes HDR & SDR séparées)."""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "tdarr", "flows", "OLED4K_norm_v6.json")

ENC_AUDIO = ("-map 0:a:0 -map 0:v:0 -map 0:a -c:a copy -c:a:0 aac -ac:0 2 -b:a:0 256k "
             "-filter:a:0 loudnorm=I=-16:TP=-1.5:LRA=11 -map 0:s? -c:s copy "
             "-max_muxing_queue_size 9999 -vsync 0 -disposition:a:0 0 -disposition:a:1 default")

def node(nid, name, plugin, repo="Community", version="1.0.0", inputs=None, x=0, y=0):
    return {"name": name, "sourceRepo": repo, "pluginName": plugin, "version": version,
            "id": nid, "position": {"x": x, "y": y}, "fpEnabled": True, "inputsDB": inputs or {}}

def edge(src, handle, tgt, eid):
    return {"source": src, "sourceHandle": str(handle), "target": tgt, "id": eid,
            "type": "smoothstep", "animated": True}

nodes, edges = [], []
def add(n): nodes.append(n)
def link(s, h, t, i): edges.append(edge(s, h, t, i))

QSV_ENC = {"outputCodec": "hevc", "ffmpegPresetEnabled": False, "ffmpegQualityEnabled": True,
           "ffmpegQuality": "{{args.variables.user.quality}}", "hardwareEncoding": True,
           "hardwareType": "qsv", "hardwareDecoding": False, "forceEncoding": False}
CPU_ENC = {"outputCodec": "hevc", "ffmpegPresetEnabled": True, "ffmpegPreset": "medium",
           "ffmpegQualityEnabled": True, "ffmpegQuality": "{{args.variables.user.quality}}",
           "hardwareEncoding": False, "hardwareType": "auto", "hardwareDecoding": False,
           "forceEncoding": True}
RES_MAP = [(1,"SD"),(2,"SD"),(3,"720"),(4,"1080"),(5,"1080"),(6,"4K"),(7,"4K"),(8,"4K"),(9,"1080")]
QUAL = {"SD":"22","720":"21","1080":"20","4K":"19"}

def encode_chain(p, color, x0):
    """Construit une chaîne QSV + fallback CPU. Retourne l'id du nœud d'entrée."""
    add(node(f"{p}_START", f"Begin QSV {p}", "ffmpegCommandStart", x=x0, y=500))
    add(node(f"{p}_MKV", f"Set MKV {p}", "ffmpegCommandSetContainer",
             inputs={"container": "mkv", "forceConform": True}, x=x0, y=560))
    add(node(f"{p}_RM", f"Remove Data {p}", "ffmpegCommandRemoveDataStreams", x=x0, y=620))
    add(node(f"{p}_RES", f"Check Resolution {p}", "checkVideoResolution", x=x0, y=680))
    for key in ("SD", "720", "1080", "4K"):
        add(node(f"{p}_{key}", f"{key} Q", "setFlowVariable",
                 inputs={"variable": "quality", "value": QUAL[key]}, x=x0, y=740))
    add(node(f"{p}_ENC", f"Enc QSV HEVC 10bit {p}", "ffmpegCommandSetVideoEncoder", inputs=QSV_ENC, x=x0, y=800))
    add(node(f"{p}_ARGS", f"Args QSV {p}", "ffmpegCommandCustomArguments",
             inputs={"inputArguments": "", "outputArguments": color + ENC_AUDIO}, x=x0, y=860))
    add(node(f"{p}_EXEC", f"Exec QSV {p}", "ffmpegCommandExecute", x=x0, y=920))
    add(node(f"{p}_CSTART", f"Begin CPU {p}", "ffmpegCommandStart", x=x0+300, y=920))
    add(node(f"{p}_CMKV", f"Set MKV CPU {p}", "ffmpegCommandSetContainer",
             inputs={"container": "mkv", "forceConform": True}, x=x0+300, y=980))
    add(node(f"{p}_CRM", f"Remove Data CPU {p}", "ffmpegCommandRemoveDataStreams", x=x0+300, y=1040))
    add(node(f"{p}_CENC", f"Enc CPU libx265 {p}", "ffmpegCommandSetVideoEncoder", inputs=CPU_ENC, x=x0+300, y=1100))
    add(node(f"{p}_CARGS", f"Args CPU {p}", "ffmpegCommandCustomArguments",
             inputs={"inputArguments": "", "outputArguments": color + "-profile:v main10 " + ENC_AUDIO}, x=x0+300, y=1160))
    add(node(f"{p}_CEXEC", f"Exec CPU {p}", "ffmpegCommandExecute", x=x0+300, y=1220))
    # câblage QSV
    link(f"{p}_START", 1, f"{p}_MKV", f"{p}_e1"); link(f"{p}_MKV", 1, f"{p}_RM", f"{p}_e2")
    link(f"{p}_RM", 1, f"{p}_RES", f"{p}_e3")
    for h, key in RES_MAP:
        link(f"{p}_RES", h, f"{p}_{key}", f"{p}_er{h}")
    for key in ("SD", "720", "1080", "4K"):
        link(f"{p}_{key}", 1, f"{p}_ENC", f"{p}_eq{key}")
    link(f"{p}_ENC", 1, f"{p}_ARGS", f"{p}_e4"); link(f"{p}_ARGS", 1, f"{p}_EXEC", f"{p}_e5")
    link(f"{p}_EXEC", 1, "V6_SIZE", f"{p}_e6"); link(f"{p}_EXEC", "err1", f"{p}_CSTART", f"{p}_eerr")
    # câblage CPU
    link(f"{p}_CSTART", 1, f"{p}_CMKV", f"{p}_c1"); link(f"{p}_CMKV", 1, f"{p}_CRM", f"{p}_c2")
    link(f"{p}_CRM", 1, f"{p}_CENC", f"{p}_c3"); link(f"{p}_CENC", 1, f"{p}_CARGS", f"{p}_c4")
    link(f"{p}_CARGS", 1, f"{p}_CEXEC", f"{p}_c5")
    link(f"{p}_CEXEC", 1, "V6_SIZE", f"{p}_c6"); link(f"{p}_CEXEC", "err1", "V6_KEEP", f"{p}_cerr")
    return f"{p}_START"

# --- entrée + aiguillage ---
add(node("V6_IN", "Input File", "inputFile", x=500, y=0))
add(node("V6_HC", "Run Health Check", "runHealthCheck", x=500, y=100))
add(node("V6_MED", "Check File Medium", "checkFileMedium", inputs={"medium": "video"}, x=500, y=200))
add(node("V6_SKIP", "Skip", "comment", inputs={"comment": "Fichier laissé tel quel"}, x=1200, y=200))
add(node("V6_HDR", "Check HDR Video", "checkHdr", x=500, y=300))
add(node("V6_DVCHK", "Check DV candidate (HEVC)", "checkVideoCodec", inputs={"codec": "hevc"}, x=250, y=400))
add(node("V6_DV", "DV Re-encode QSV", "dvReencodeQsv", repo="Local", x=100, y=500))
# --- post-traitement commun ---
add(node("V6_SIZE", "Check File Size Ratio", "compareFileSizeRatio",
         inputs={"greaterThan": 100, "lessThan": 5}, x=1000, y=1320))
add(node("V6_KEEP", "Keep Original", "comment",
         inputs={"comment": "Ré-encode non bénéfique/suspect -> original conservé"}, x=1300, y=1320))
add(node("V6_REPL", "Replace Original File", "replaceOriginalFile", x=1000, y=1380))
add(node("V6_CLR", "Clear Cache", "clearCache", x=1000, y=1440))
add(node("V6_OK", "Success", "comment",
         inputs={"comment": "HEVC Main10 + Atmos copy + AAC norm. HDR/DV préservé."}, x=1000, y=1500))
# --- chaînes d'encodage ---
hdr = encode_chain("H", "-color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc ", 700)
sdr = encode_chain("S", "", 1600)
# --- routage ---
link("V6_IN", 1, "V6_HC", "e_in")
link("V6_HC", 1, "V6_MED", "e_hc1"); link("V6_HC", 2, "V6_SKIP", "e_hc2")
link("V6_MED", 1, "V6_HDR", "e_med1"); link("V6_MED", 2, "V6_SKIP", "e_med2")
link("V6_HDR", 1, "V6_DVCHK", "e_hdr1"); link("V6_HDR", 2, sdr, "e_hdr2")
link("V6_DVCHK", 1, "V6_DV", "e_dv1"); link("V6_DVCHK", 2, hdr, "e_dv2")
link("V6_DV", 1, "V6_SIZE", "e_dvok"); link("V6_DV", 2, hdr, "e_dvfb")
link("V6_SIZE", 1, "V6_KEEP", "e_s1"); link("V6_SIZE", 2, "V6_REPL", "e_s2"); link("V6_SIZE", 3, "V6_REPL", "e_s3")
link("V6_REPL", 1, "V6_CLR", "e_r1"); link("V6_REPL", 2, "V6_KEEP", "e_r2")
link("V6_CLR", 1, "V6_OK", "e_cl1")

flow = {"_id": "OLED4K_norm_v6", "name": "OLED4K norm v6 - HDR/DV hybride QSV [CANONICAL]",
        "priority": 1, "flowPlugins": nodes, "flowEdges": edges}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(flow, f, indent=1)
print(f"OK {len(nodes)} nœuds, {len(edges)} arêtes -> {OUT}")
