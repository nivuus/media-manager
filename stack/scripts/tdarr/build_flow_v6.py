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
    add(node(f"{p}_NEXEC", f"Exec NVENC {p}", "ffmpegCommandExecute", x=x0-300, y=920))
    # --- sous-chaîne QSV (sortie 2 : pas de NVENC sur ce node) ---
    add(node(f"{p}_RES", f"Check Resolution {p}", "checkVideoResolution", x=x0, y=680))
    for key in ("SD", "720", "1080", "4K"):
        add(node(f"{p}_{key}", f"{key} Q", "setFlowVariable",
                 inputs={"variable": "quality", "value": QUAL[key]}, x=x0, y=740))
    add(node(f"{p}_ENC", f"Enc QSV HEVC 10bit {p}", "ffmpegCommandSetVideoEncoder", inputs=QSV_ENC, x=x0, y=800))
    add(node(f"{p}_ARGS", f"Args QSV {p}", "ffmpegCommandCustomArguments",
             inputs={"inputArguments": "", "outputArguments": (color + MUX).strip()}, x=x0, y=860))
    add(node(f"{p}_EXEC", f"Exec QSV {p}", "ffmpegCommandExecute", x=x0, y=920))
    add(node(f"{p}_CSTART", f"Begin CPU {p}", "ffmpegCommandStart", x=x0+300, y=920))
    add(node(f"{p}_CMKV", f"Set MKV CPU {p}", "ffmpegCommandSetContainer",
             inputs={"container": "mkv", "forceConform": True}, x=x0+300, y=980))
    add(node(f"{p}_CRM", f"Remove Data CPU {p}", "ffmpegCommandRemoveDataStreams", x=x0+300, y=1040))
    add(node(f"{p}_CENC", f"Enc CPU libx265 {p}", "ffmpegCommandSetVideoEncoder", inputs=CPU_ENC, x=x0+300, y=1100))
    add(node(f"{p}_CARGS", f"Args CPU {p}", "ffmpegCommandCustomArguments",
             inputs={"inputArguments": "", "outputArguments": (color + MUX).strip()}, x=x0+300, y=1160))
    add(node(f"{p}_CEXEC", f"Exec CPU {p}", "ffmpegCommandExecute", x=x0+300, y=1220))
    # câblage commun + aiguillage par node
    link(f"{p}_START", 1, f"{p}_MKV", f"{p}_e1"); link(f"{p}_MKV", 1, f"{p}_RM", f"{p}_e2")
    link(f"{p}_RM", 1, f"{p}_NCHK", f"{p}_e3")
    link(f"{p}_NCHK", 1, f"{p}_NRES", f"{p}_n0"); link(f"{p}_NCHK", 2, f"{p}_RES", f"{p}_n1")
    # câblage NVENC
    for h, key in RES_MAP:
        link(f"{p}_NRES", h, f"{p}_N{key}", f"{p}_nr{h}")
    for key in ("SD", "720", "1080", "4K"):
        link(f"{p}_N{key}", 1, f"{p}_NENC", f"{p}_nq{key}")
    link(f"{p}_NENC", 1, f"{p}_NARGS", f"{p}_n4"); link(f"{p}_NARGS", 1, f"{p}_NEXEC", f"{p}_n5")
    link(f"{p}_NEXEC", 1, "V6_AUD", f"{p}_n6"); link(f"{p}_NEXEC", "err1", f"{p}_CSTART", f"{p}_nerr")
    # câblage QSV
    for h, key in RES_MAP:
        link(f"{p}_RES", h, f"{p}_{key}", f"{p}_er{h}")
    for key in ("SD", "720", "1080", "4K"):
        link(f"{p}_{key}", 1, f"{p}_ENC", f"{p}_eq{key}")
    link(f"{p}_ENC", 1, f"{p}_ARGS", f"{p}_e4"); link(f"{p}_ARGS", 1, f"{p}_EXEC", f"{p}_e5")
    link(f"{p}_EXEC", 1, "V6_AUD", f"{p}_e6"); link(f"{p}_EXEC", "err1", f"{p}_CSTART", f"{p}_eerr")
    # câblage CPU
    link(f"{p}_CSTART", 1, f"{p}_CMKV", f"{p}_c1"); link(f"{p}_CMKV", 1, f"{p}_CRM", f"{p}_c2")
    link(f"{p}_CRM", 1, f"{p}_CENC", f"{p}_c3"); link(f"{p}_CENC", 1, f"{p}_CARGS", f"{p}_c4")
    link(f"{p}_CARGS", 1, f"{p}_CEXEC", f"{p}_c5")
    link(f"{p}_CEXEC", 1, "V6_AUD", f"{p}_c6"); link(f"{p}_CEXEC", "err1", "V6_ORIG", f"{p}_cerr")
    return f"{p}_START"

# --- entrée + aiguillage ---
add(node("V6_IN", "Input File", "inputFile", x=500, y=0))
add(node("V6_HC", "Run Health Check", "runHealthCheck", x=500, y=100))
add(node("V6_MED", "Check File Medium", "checkFileMedium", inputs={"medium": "video"}, x=500, y=200))
add(node("V6_SKIP", "Skip", "comment", inputs={"comment": "Fichier laissé tel quel"}, x=1200, y=200))
# --- garde-fou de COMPATIBILITÉ (objectif : compat > taille) ---
# Un fichier est « déjà compatible » s'il a une vidéo HEVC ou H264 ET une piste AAC
# stéréo. Dans ce cas on garde l'original tel quel (pas de ré-encode inutile). Sinon
# on produit la version compatible, quitte à ce qu'elle soit plus grosse :
#   - vidéo pas HEVC/H264 (AV1, VC1, MPEG4…) -> ré-encode HEVC + piste AAC stéréo
#   - vidéo OK mais pas de piste AAC stéréo   -> on ajoute juste la piste (pas de ré-encode)
# checkVideoCodec/checkAudioCodec/checkChannelCount : sortie 1 = présent, 2 = absent.
add(node("V6_VHEVC", "Video is HEVC?", "checkVideoCodec", inputs={"codec": "hevc"}, x=500, y=250))
add(node("V6_VH264", "Video is H264?", "checkVideoCodec", inputs={"codec": "h264"}, x=350, y=300))
add(node("V6_AAC", "Has AAC track?", "checkAudioCodec",
         inputs={"codec": "aac", "checkBitrate": False}, x=650, y=300))
add(node("V6_2CH", "Has stereo track?", "checkChannelCount", inputs={"channelCount": "2"}, x=650, y=360))
add(node("V6_COMPAT", "Already Compatible", "comment",
         inputs={"comment": "Vidéo HEVC/H264 + piste AAC stéréo déjà présente -> original conservé"}, x=1200, y=300))
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
# NB : le plugin Local dvReencodeQsv est retiré du flow actif (bug CLI non testé
# faisait crasher les workers). HDR/DV -> chaîne HDR QSV (plugins Community
# éprouvés). DV re-encodé en HDR10 (RPU perdu) ; préservation DV = chantier v6.2.
# --- post-traitement commun ---
# Piste AAC stéréo normalisée ajoutée après l'encode vidéo (pistes originales
# conservées, Atmos préservé). Plugin Local validé ; fallback = fichier inchangé.
add(node("V6_AUD", "Add Normalized Stereo (AAC)", "addNormalizedStereo", repo="Local",
         inputs={"i": "-16.0", "lra": "11.0", "tp": "-1.5", "bitrate": "256k"}, x=1000, y=1260))
# compareFileSizeRatio : sortie 1 = taille DANS la plage (on remplace), 2 = hors plage
# (on garde l'original). Compat > taille : plus de borne haute (on prend le résultat même
# plus gros). Seule reste la borne basse anti-corruption : < 5 % de l'original = sortie
# tronquée/corrompue -> on garde l'original. (One Piece à 24 %, Mandalorian à 24 % passent.)
add(node("V6_SIZE", "Check File Size Ratio", "compareFileSizeRatio",
         inputs={"greaterThan": 5, "lessThan": 100000}, x=1000, y=1320))
# Toute branche « on garde l'original » DOIT repasser par setWorkingFile : sinon le
# flow se termine avec le fichier de travail encore dans le cache, ce que Tdarr
# refuse -> verdict transcodeError (et le fichier compte comme un échec).
add(node("V6_ORIG", "Reset Working File", "setWorkingFile",
         inputs={"source": "originalFile", "customPath": ""}, x=1300, y=1260))
add(node("V6_KEEP", "Keep Original", "comment",
         inputs={"comment": "Sortie corrompue/suspecte (< 5 %) -> original conservé"}, x=1300, y=1320))
add(node("V6_REPL", "Replace Original File", "replaceOriginalFile", x=1000, y=1380))
add(node("V6_CLR", "Clear Cache", "clearCache", x=1000, y=1440))
add(node("V6_OK", "Success", "comment",
         inputs={"comment": "HEVC (NVENC ou QSV selon le node) + toutes pistes copiées (Atmos ok). HDR10 tags conservés. Piste AAC stéréo normalisée ajoutée."}, x=1000, y=1500))
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
# garde-fou bitrate : split résolution -> check du débit -> sous seuil = audio compat, au-dessus = ré-encode
# checkVideoResolution handles : 1=480p 2=576p 3=720p 4=1080p 5=1440p 6=4KUHD 7=DCI4K 8=8KUHD 9=other
link("V6_BRES", 1, "V6_AAC", "e_br1"); link("V6_BRES", 2, "V6_AAC", "e_br2")
link("V6_BRES", 3, "V6_AAC", "e_br3")                 # 720p -> jamais ré-encodé
link("V6_BRES", 4, "V6_BR1080", "e_br4")              # 1080p -> check 20 Mbps
link("V6_BRES", 5, "V6_BR4K", "e_br5")                # 1440p -> seuil 4K (35 Mbps)
link("V6_BRES", 6, "V6_BR4K", "e_br6"); link("V6_BRES", 7, "V6_BR4K", "e_br7")
link("V6_BRES", 8, "V6_BR4K", "e_br8"); link("V6_BRES", 9, "V6_AAC", "e_br9")
# sortie 1 = sous le seuil (on garde -> audio compat) ; sortie 2 = au-dessus (on ré-encode)
link("V6_BR1080", 1, "V6_AAC", "e_b10a"); link("V6_BR1080", 2, "V6_HDR", "e_b10b")
link("V6_BR4K", 1, "V6_AAC", "e_b4a"); link("V6_BR4K", 2, "V6_HDR", "e_b4b")
# a une piste AAC ? -> oui : vérifier qu'il y a du stéréo ; non : ajouter la piste (Path B)
link("V6_AAC", 1, "V6_2CH", "e_ac1"); link("V6_AAC", 2, "V6_AUD", "e_ac2")
# a une piste 2 canaux ? -> oui : déjà compatible, on garde ; non : ajouter la piste (Path B)
link("V6_2CH", 1, "V6_COMPAT", "e_2c1"); link("V6_2CH", 2, "V6_AUD", "e_2c2")
# Path C : ré-encode -> split HDR/SDR
link("V6_HDR", 1, hdr, "e_hdr1"); link("V6_HDR", 2, sdr, "e_hdr2")
link("V6_AUD", 1, "V6_SIZE", "e_aud")
link("V6_SIZE", 1, "V6_REPL", "e_s1"); link("V6_SIZE", 2, "V6_ORIG", "e_s2")
link("V6_REPL", 1, "V6_CLR", "e_r1"); link("V6_REPL", 2, "V6_ORIG", "e_r2")
link("V6_ORIG", 1, "V6_KEEP", "e_orig")
link("V6_CLR", 1, "V6_OK", "e_cl1")

flow = {"_id": "OLED4K_norm_v6", "name": "OLED4K norm v6 - Compat-first HEVC/H264+AAC (NVENC+QSV) [CANONICAL]",
        "priority": 1, "flowPlugins": nodes, "flowEdges": edges}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(flow, f, indent=1)
print(f"OK {len(nodes)} nœuds, {len(edges)} arêtes -> {OUT}")
