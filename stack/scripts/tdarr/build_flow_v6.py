#!/usr/bin/env python3
"""Génère tdarr/flows/OLED4K_norm_v6.json (flow HDR/DV hybride, chaînes HDR & SDR séparées)."""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "tdarr", "flows", "OLED4K_norm_v6.json")

# Le nœud ffmpegCommandSetVideoEncoder mappe DÉJÀ tous les flux (vidéo encodée,
# reste copié). On NE re-mappe PAS et on NE force PAS de profil ici : re-mapper
# créait un flux vidéo parasite en libx264, et -profile:v main10 (global) était
# rejeté par libx264 ET par hevc_qsv sur source 8-bit. On garde le bit-depth natif
# (HDR 10-bit préservé par QSV) + copie de toutes les pistes (Atmos intact).
# Custom args = seulement passthrough couleur (HDR) + file de muxing.
MUX = "-max_muxing_queue_size 9999"

def node(nid, name, plugin, repo="Community", version="1.0.0", inputs=None, x=0, y=0):
    return {"name": name, "sourceRepo": repo, "pluginName": plugin, "version": version,
            "id": nid, "position": {"x": x, "y": y}, "fpEnabled": True, "inputsDB": inputs or {}}

def edge(src, handle, tgt, eid):
    return {"source": src, "sourceHandle": str(handle), "target": tgt, "id": eid,
            "type": "smoothstep", "animated": True}

nodes, edges = [], []
def add(n): nodes.append(n)
def link(s, h, t, i): edges.append(edge(s, h, t, i))

# forceEncoding=True INDISPENSABLE : le plugin ne ré-encode que si forceEncoding OU
# codec_name != cible (ffmpegCommandSetVideoEncoder l.240). Sans ça, une source déjà
# HEVC routée ici par le garde-fou bitrate serait COPIÉE (pas ré-encodée) -> aucun gain.
# Tout fichier qui atteint la chaîne d'encodage est destiné au ré-encode, donc sûr.
QSV_ENC = {"outputCodec": "hevc", "ffmpegPresetEnabled": False, "ffmpegQualityEnabled": True,
           "ffmpegQuality": "{{args.variables.user.quality}}", "hardwareEncoding": True,
           "hardwareType": "qsv", "hardwareDecoding": False, "forceEncoding": True}
# NVENC (RTX 4070, node tdarr-node-nvenc). Le plugin émet `-global_quality` pour
# hevc_qsv mais `-qp` pour tout autre encodeur GPU : l'échelle n'est donc PAS la même
# que celle du QSV, d'où NVQ ci-dessous. On laisse le plugin gérer le débit (constqp) :
# mesuré plus efficace que `-rc vbr -cq` (à SSIM 0.9861 : qp20 = 28.5 Mo vs cq26 = 33.6 Mo).
NVENC_ENC = {"outputCodec": "hevc", "ffmpegPresetEnabled": False, "ffmpegQualityEnabled": True,
             "ffmpegQuality": "{{args.variables.user.quality}}", "hardwareEncoding": True,
             "hardwareType": "nvenc", "hardwareDecoding": False, "forceEncoding": True}
CPU_ENC = {"outputCodec": "hevc", "ffmpegPresetEnabled": True, "ffmpegPreset": "medium",
           "ffmpegQualityEnabled": True, "ffmpegQuality": "{{args.variables.user.quality}}",
           "hardwareEncoding": False, "hardwareType": "auto", "hardwareDecoding": False,
           "forceEncoding": True}
# (handle de sortie de checkVideoResolution) -> bucket qualité.
# Mapping repris de v5 ; les handles 1-9 dépendent de la version de Tdarr,
# à revérifier dans l'UI si un palier de résolution semble mal routé.
RES_MAP = [(1,"SD"),(2,"SD"),(3,"720"),(4,"1080"),(5,"1080"),(6,"4K"),(7,"4K"),(8,"4K"),(9,"1080")]
QUAL = {"SD":"22","720":"21","1080":"20","4K":"19"}
# Échelle NVENC calibrée sur 90 s de « 1922 (2017) » (h264 1080p), SSIM vs source :
#   QSV -global_quality 20 -> 21.75 Mo / 0.984032
#   NVENC -qp 22           -> 20.45 Mo / 0.983994  (même qualité, 6 % plus petit)
# Soit un décalage constant de +2 sur l'échelle QSV. Recalibrer si l'encodeur change.
NVQ = {k: str(int(v) + 2) for k, v in QUAL.items()}
# Inputs of ensureStereoPerLanguage: EBU-style loudness target for the added
# AAC stereo tracks.
STEREO = {"i": "-16.0", "lra": "11.0", "tp": "-1.5", "bitrate": "256k"}
# Subtitle tracks ffmpeg has no codec for (Matroska S_TEXT/WEBVTT) are taken
# out of every ffmpeg command by setAsideUnreadableSubtitles, which lists their
# track IDs in this flow variable; after ffmpeg, mkvmerge copies them back from
# the original. Copying them through ffmpeg fails the whole job.
UNREADABLE_SUBS = "args.variables.user.unreadableSubtitleTracks"
MERGE_SUBS = {
    "useCustomCliPath": False, "userCli": "mkvmerge", "doesCommandCreateOutputFile": True,
    "userOutputFilePath": "${cacheDir}/${fileName}.mkv",
    "cliArguments": ('-o "${outputFilePath}" "{{{args.inputFileObj._id}}}" '
                     "--no-video --no-audio --no-attachments --no-chapters --no-global-tags "
                     "--subtitle-tracks {{{" + UNREADABLE_SUBS + "}}} "
                     '"{{{args.originalLibraryFile._id}}}"'),
    "outputFileBecomesWorkingFile": True,
}

def encode_chain(p, color, x0):
    """Construit une chaîne NVENC|QSV + fallback CPU. Retourne l'id du nœud d'entrée.

    L'aiguillage NVENC/QSV se fait PAR NODE via checkNodeHardwareEncoder : le node
    tdarr-node-nvenc (RTX 4070) n'a pas /dev/dri et sort en 1, le node QSV (iGPU) n'a
    pas la carte NVIDIA et sort en 2. Chaque encodeur a sa propre échelle de qualité,
    d'où la duplication du checkVideoResolution et des variables de qualité : ça évite
    de faire dépendre les arguments custom d'une substitution de variable.
    """
    add(node(f"{p}_START", f"Begin {p}", "ffmpegCommandStart", x=x0, y=500))
    add(node(f"{p}_MKV", f"Set MKV {p}", "ffmpegCommandSetContainer",
             inputs={"container": "mkv", "forceConform": True}, x=x0, y=560))
    add(node(f"{p}_RM", f"Remove Data {p}", "ffmpegCommandRemoveDataStreams", x=x0, y=620))
    add(node(f"{p}_KEEPV", f"Keep Primary Video {p}", "keepPrimaryVideo", repo="Local", x=x0, y=635))
    add(node(f"{p}_VTT", f"Set Aside Unreadable Subs {p}", "setAsideUnreadableSubtitles", repo="Local",
             x=x0, y=642))
    add(node(f"{p}_NCHK", f"Node has NVENC? {p}", "checkNodeHardwareEncoder",
             inputs={"hardwareEncoder": "hevc_nvenc"}, x=x0, y=650))
    # --- sous-chaîne NVENC (sortie 1 : le node a la RTX) ---
    add(node(f"{p}_NRES", f"Check Resolution NVENC {p}", "checkVideoResolution", x=x0-300, y=680))
    for key in ("SD", "720", "1080", "4K"):
        add(node(f"{p}_N{key}", f"{key} Q NVENC", "setFlowVariable",
                 inputs={"variable": "quality", "value": NVQ[key]}, x=x0-300, y=740))
    add(node(f"{p}_NENC", f"Enc NVENC HEVC {p}", "ffmpegCommandSetVideoEncoder", inputs=NVENC_ENC, x=x0-300, y=800))
    add(node(f"{p}_NARGS", f"Args NVENC {p}", "ffmpegCommandCustomArguments",
             inputs={"inputArguments": "", "outputArguments": (color + MUX).strip()}, x=x0-300, y=860))
    add(node(f"{p}_NSTEREO", f"Stereo per language NVENC {p}", "ensureStereoPerLanguage", repo="Local",
             inputs=STEREO, x=x0-300, y=890))
    add(node(f"{p}_NEXEC", f"Exec NVENC {p}", "ffmpegCommandExecute", x=x0-300, y=920))
    # --- sous-chaîne QSV (sortie 2 : pas de NVENC sur ce node) ---
    add(node(f"{p}_RES", f"Check Resolution {p}", "checkVideoResolution", x=x0, y=680))
    for key in ("SD", "720", "1080", "4K"):
        add(node(f"{p}_{key}", f"{key} Q", "setFlowVariable",
                 inputs={"variable": "quality", "value": QUAL[key]}, x=x0, y=740))
    add(node(f"{p}_ENC", f"Enc QSV HEVC 10bit {p}", "ffmpegCommandSetVideoEncoder", inputs=QSV_ENC, x=x0, y=800))
    add(node(f"{p}_ARGS", f"Args QSV {p}", "ffmpegCommandCustomArguments",
             inputs={"inputArguments": "", "outputArguments": (color + MUX).strip()}, x=x0, y=860))
    add(node(f"{p}_STEREO", f"Stereo per language QSV {p}", "ensureStereoPerLanguage", repo="Local",
             inputs=STEREO, x=x0, y=890))
    add(node(f"{p}_EXEC", f"Exec QSV {p}", "ffmpegCommandExecute", x=x0, y=920))
    add(node(f"{p}_CSTART", f"Begin CPU {p}", "ffmpegCommandStart", x=x0+300, y=920))
    add(node(f"{p}_CMKV", f"Set MKV CPU {p}", "ffmpegCommandSetContainer",
             inputs={"container": "mkv", "forceConform": True}, x=x0+300, y=980))
    add(node(f"{p}_CRM", f"Remove Data CPU {p}", "ffmpegCommandRemoveDataStreams", x=x0+300, y=1040))
    add(node(f"{p}_CKEEPV", f"Keep Primary Video CPU {p}", "keepPrimaryVideo", repo="Local", x=x0+300, y=1070))
    add(node(f"{p}_CVTT", f"Set Aside Unreadable Subs CPU {p}", "setAsideUnreadableSubtitles",
             repo="Local", x=x0+300, y=1085))
    add(node(f"{p}_CENC", f"Enc CPU libx265 {p}", "ffmpegCommandSetVideoEncoder", inputs=CPU_ENC, x=x0+300, y=1100))
    add(node(f"{p}_CARGS", f"Args CPU {p}", "ffmpegCommandCustomArguments",
             inputs={"inputArguments": "", "outputArguments": (color + MUX).strip()}, x=x0+300, y=1160))
    add(node(f"{p}_CSTEREO", f"Stereo per language CPU {p}", "ensureStereoPerLanguage", repo="Local",
             inputs=STEREO, x=x0+300, y=1190))
    add(node(f"{p}_CEXEC", f"Exec CPU {p}", "ffmpegCommandExecute", x=x0+300, y=1220))
    # câblage commun + aiguillage par node
    link(f"{p}_START", 1, f"{p}_MKV", f"{p}_e1"); link(f"{p}_MKV", 1, f"{p}_RM", f"{p}_e2")
    link(f"{p}_RM", 1, f"{p}_KEEPV", f"{p}_e3"); link(f"{p}_KEEPV", 1, f"{p}_VTT", f"{p}_e3k")
    link(f"{p}_VTT", 1, f"{p}_NCHK", f"{p}_e3v")
    link(f"{p}_NCHK", 1, f"{p}_NRES", f"{p}_n0"); link(f"{p}_NCHK", 2, f"{p}_RES", f"{p}_n1")
    # câblage NVENC
    for h, key in RES_MAP:
        link(f"{p}_NRES", h, f"{p}_N{key}", f"{p}_nr{h}")
    for key in ("SD", "720", "1080", "4K"):
        link(f"{p}_N{key}", 1, f"{p}_NENC", f"{p}_nq{key}")
    link(f"{p}_NENC", 1, f"{p}_NARGS", f"{p}_n4"); link(f"{p}_NARGS", 1, f"{p}_NSTEREO", f"{p}_n5")
    link(f"{p}_NSTEREO", 1, f"{p}_NEXEC", f"{p}_n5a"); link(f"{p}_NSTEREO", 2, f"{p}_NEXEC", f"{p}_n5b")
    link(f"{p}_NEXEC", 1, "V6_SUBS", f"{p}_n6"); link(f"{p}_NEXEC", "err1", f"{p}_CSTART", f"{p}_nerr")
    # câblage QSV
    for h, key in RES_MAP:
        link(f"{p}_RES", h, f"{p}_{key}", f"{p}_er{h}")
    for key in ("SD", "720", "1080", "4K"):
        link(f"{p}_{key}", 1, f"{p}_ENC", f"{p}_eq{key}")
    link(f"{p}_ENC", 1, f"{p}_ARGS", f"{p}_e4"); link(f"{p}_ARGS", 1, f"{p}_STEREO", f"{p}_e5")
    link(f"{p}_STEREO", 1, f"{p}_EXEC", f"{p}_e5a"); link(f"{p}_STEREO", 2, f"{p}_EXEC", f"{p}_e5b")
    link(f"{p}_EXEC", 1, "V6_SUBS", f"{p}_e6"); link(f"{p}_EXEC", "err1", f"{p}_CSTART", f"{p}_eerr")
    # câblage CPU
    link(f"{p}_CSTART", 1, f"{p}_CMKV", f"{p}_c1"); link(f"{p}_CMKV", 1, f"{p}_CRM", f"{p}_c2")
    link(f"{p}_CRM", 1, f"{p}_CKEEPV", f"{p}_c3"); link(f"{p}_CKEEPV", 1, f"{p}_CVTT", f"{p}_c3k")
    link(f"{p}_CVTT", 1, f"{p}_CENC", f"{p}_c3v")
    link(f"{p}_CENC", 1, f"{p}_CARGS", f"{p}_c4"); link(f"{p}_CARGS", 1, f"{p}_CSTEREO", f"{p}_c5")
    link(f"{p}_CSTEREO", 1, f"{p}_CEXEC", f"{p}_c5a"); link(f"{p}_CSTEREO", 2, f"{p}_CEXEC", f"{p}_c5b")
    # A CPU failure after a GPU failure is an error, not a reason to keep the
    # original quietly: no err1 edge, so the job ends as transcodeError.
    link(f"{p}_CEXEC", 1, "V6_SUBS", f"{p}_c6")
    return f"{p}_START"

# --- entrée + aiguillage ---
add(node("V6_IN", "Input File", "inputFile", x=500, y=0))
add(node("V6_HC", "Run Health Check", "runHealthCheck", x=500, y=100))
add(node("V6_MED", "Check File Medium", "checkFileMedium", inputs={"medium": "video"}, x=500, y=200))
add(node("V6_SKIP", "Skip", "comment", inputs={"comment": "Fichier laissé tel quel"}, x=1200, y=200))
# --- COMPATIBILITY guard (goal: compatibility over size) ---
# A file is "already compatible" when its video is HEVC or H264 AND every audio
# language has an AAC stereo track AND exactly one audio track is default. The
# audio half is decided by the Local plugin ensureStereoPerLanguage, not by
# checkAudioCodec + checkChannelCount: those two only proved that SOME AAC track
# and SOME 2-channel stream existed, not that they were the same track, nor that
# each language had one.
#   - video neither HEVC nor H264 (AV1, VC1, MPEG4...) -> HEVC re-encode
#   - video fine but audio not as required -> copy everything, fix the audio
add(node("V6_VHEVC", "Video is HEVC?", "checkVideoCodec", inputs={"codec": "hevc"}, x=500, y=250))
add(node("V6_VH264", "Video is H264?", "checkVideoCodec", inputs={"codec": "h264"}, x=350, y=300))
add(node("V6_COMPAT", "Already Compatible", "comment",
         inputs={"comment": "HEVC/H264 video and a normalised AAC stereo track per language: original kept"}, x=1200, y=300))
# --- garde-fou BITRATE (compat-first MAIS on ré-encode les fichiers obèses) ---
# Un codec compatible (HEVC/H264) n'est PLUS gardé aveuglément : s'il est trop gros
# pour sa résolution (REMUX 4K à 50-80 Mbps, BluRay 1080p lourds), on le ré-encode
# quand même vers la cible qualité, sinon on le conserve tel quel (Path A/B).
# Seuils GLOBAUX (bit_rate = débit overall, audio inclus ; vérifié sur Interstellar 58,9 Mbps) :
#   4K/1440p > 35 Mbps -> ré-encode | 1080p > 20 Mbps -> ré-encode | 720p/SD/other -> conservé.
# checkOverallBitrate : sortie 1 = DANS la plage [0, seuil] (sous le seuil, on garde),
# sortie 2 = hors plage (au-dessus, on ré-encode). bit_rate manquant (0) -> sortie 1 = gardé.
add(node("V6_BRES", "Res for bitrate guard", "checkVideoResolution", x=480, y=430))
add(node("V6_BR1080", "1080p bitrate > 20 Mbps?", "checkOverallBitrate",
         inputs={"unit": "mbps", "greaterThan": 0, "lessThan": 20}, x=360, y=500))
add(node("V6_BR4K", "4K bitrate > 35 Mbps?", "checkOverallBitrate",
         inputs={"unit": "mbps", "greaterThan": 0, "lessThan": 35}, x=600, y=500))
add(node("V6_HDR", "Check HDR Video", "checkHdr", x=350, y=400))
# Dolby Vision is re-encoded as HDR10 (RPU lost); only the base layer is kept,
# keepPrimaryVideo drops a profile 7 enhancement-layer track.
# --- compatibility path: copy every stream, fix the audio only if needed ---
add(node("C_START", "Begin Audio Fix", "ffmpegCommandStart", x=900, y=560))
add(node("C_VTT", "Set Aside Unreadable Subs", "setAsideUnreadableSubtitles", repo="Local", x=900, y=590))
add(node("C_STEREO", "Stereo per language", "ensureStereoPerLanguage", repo="Local",
         inputs=STEREO, x=900, y=620))
add(node("C_EXEC", "Exec Audio Fix", "ffmpegCommandExecute", x=900, y=680))
# compareFileSizeRatio : sortie 1 = taille DANS la plage (on remplace), 2 = hors plage.
# Compat > taille : plus de borne haute (on prend le résultat même plus gros). Seule
# reste la borne basse anti-corruption : < 5 % de l'original = sortie tronquée.
# A truncated output is a failure and is reported as one (failFlow), never turned
# into a silent "original kept" success.
add(node("V6_SUBS", "Unreadable subs set aside?", "checkFlowVariable",
         inputs={"variable": UNREADABLE_SUBS, "condition": "==", "value": ""}, x=1000, y=1200))
add(node("V6_SUBM", "Restore subs (mkvmerge)", "runCli", inputs=MERGE_SUBS, x=1300, y=1230))
# compareFileDurationRatio: output 1 = within [99.5 %, 100.5 %] of the original,
# 2 = shorter, 3 = longer. A job that lost or gained running time is a failure.
add(node("V6_DUR", "Check File Duration Ratio", "compareFileDurationRatio",
         inputs={"greaterThan": "99.5", "lessThan": "100.5"}, x=1000, y=1260))
add(node("V6_DFAIL", "Fail: duration differs", "failFlow", x=1300, y=1290))
add(node("V6_SIZE", "Check File Size Ratio", "compareFileSizeRatio",
         inputs={"greaterThan": 5, "lessThan": 100000}, x=1000, y=1320))
add(node("V6_FAIL", "Fail: output truncated", "failFlow", x=1300, y=1320))
add(node("V6_REPL", "Replace Original File", "replaceOriginalFile", x=1000, y=1380))
add(node("V6_CLR", "Clear Cache", "clearCache", x=1000, y=1440))
add(node("V6_OK", "Success", "comment",
         inputs={"comment": "HEVC (NVENC or QSV per node) or original video, every other stream copied, "
                            "HDR10 metadata kept, one normalised AAC stereo track per language, "
                            "subtitles ffmpeg cannot read restored by mkvmerge."}, x=1000, y=1500))
# --- chaînes d'encodage ---
hdr = encode_chain("H", "-color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc ", 700)
sdr = encode_chain("S", "", 1600)
# --- routage ---
link("V6_IN", 1, "V6_HC", "e_in")
link("V6_HC", 1, "V6_MED", "e_hc1"); link("V6_HC", 2, "V6_SKIP", "e_hc2")
link("V6_MED", 1, "V6_VHEVC", "e_med1"); link("V6_MED", 2, "V6_SKIP", "e_med2")
# vidéo HEVC ? -> oui : garde-fou bitrate ; non : tester H264
link("V6_VHEVC", 1, "V6_BRES", "e_vh1"); link("V6_VHEVC", 2, "V6_VH264", "e_vh2")
# vidéo H264 ? -> oui : garde-fou bitrate ; non (ni HEVC ni H264) : ré-encoder (Path C)
link("V6_VH264", 1, "V6_BRES", "e_v41"); link("V6_VH264", 2, "V6_HDR", "e_v42")
# garde-fou bitrate : split résolution -> check du débit -> sous seuil = compat path, au-dessus = ré-encode
# checkVideoResolution handles (checked against the plugin source, Tdarr 2.94):
# 1=480p 2=576p 3=720p 4=1080p 5=1440p 6=4KUHD 7=DCI4K 8=8KUHD 9=other
link("V6_BRES", 1, "C_START", "e_br1"); link("V6_BRES", 2, "C_START", "e_br2")
link("V6_BRES", 3, "C_START", "e_br3")                # 720p -> jamais ré-encodé
link("V6_BRES", 4, "V6_BR1080", "e_br4")              # 1080p -> check 20 Mbps
link("V6_BRES", 5, "V6_BR4K", "e_br5")                # 1440p -> seuil 4K (35 Mbps)
link("V6_BRES", 6, "V6_BR4K", "e_br6"); link("V6_BRES", 7, "V6_BR4K", "e_br7")
link("V6_BRES", 8, "V6_BR4K", "e_br8"); link("V6_BRES", 9, "C_START", "e_br9")
# sortie 1 = sous le seuil (on garde -> compat path) ; sortie 2 = au-dessus (on ré-encode)
link("V6_BR1080", 1, "C_START", "e_b10a"); link("V6_BR1080", 2, "V6_HDR", "e_b10b")
link("V6_BR4K", 1, "C_START", "e_b4a"); link("V6_BR4K", 2, "V6_HDR", "e_b4b")
# compat path: output 1 = the audio needs fixing, output 2 = nothing to do
link("C_START", 1, "C_VTT", "e_cs")
link("C_VTT", 1, "C_STEREO", "e_cv")
link("C_STEREO", 1, "C_EXEC", "e_cst1"); link("C_STEREO", 2, "V6_COMPAT", "e_cst2")
link("C_EXEC", 1, "V6_SUBS", "e_cex")
# Path C : ré-encode -> split HDR/SDR
link("V6_HDR", 1, hdr, "e_hdr1"); link("V6_HDR", 2, sdr, "e_hdr2")
# checkFlowVariable: output 1 = the variable is empty (nothing set aside).
link("V6_SUBS", 1, "V6_DUR", "e_sub1"); link("V6_SUBS", 2, "V6_SUBM", "e_sub2")
link("V6_SUBM", 1, "V6_DUR", "e_subm")
link("V6_DUR", 1, "V6_SIZE", "e_d1")
link("V6_DUR", 2, "V6_DFAIL", "e_d2"); link("V6_DUR", 3, "V6_DFAIL", "e_d3")
link("V6_SIZE", 1, "V6_REPL", "e_s1"); link("V6_SIZE", 2, "V6_FAIL", "e_s2")
link("V6_REPL", 1, "V6_CLR", "e_r1")
link("V6_CLR", 1, "V6_OK", "e_cl1")

flow = {"_id": "OLED4K_norm_v6", "name": "OLED4K norm v6 - Compat-first HEVC/H264+AAC (NVENC+QSV) [CANONICAL]",
        "priority": 1, "flowPlugins": nodes, "flowEdges": edges}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(flow, f, indent=1)
print(f"OK {len(nodes)} nœuds, {len(edges)} arêtes -> {OUT}")
