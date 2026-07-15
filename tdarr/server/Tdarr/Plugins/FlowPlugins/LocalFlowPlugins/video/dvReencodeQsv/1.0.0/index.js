"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
const path = require('path');
const { CLI } = require('../../../../FlowHelpers/1.0.0/cliUtils');

const DOVI = '/app/bin/dovi_tool';
const MKVMERGE = '/app/bin/mkvmerge';
const LIBENV = Object.assign({}, process.env, { LD_LIBRARY_PATH: '/app/bin/lib' });

// --- logique pure : COPIE À L'IDENTIQUE de scripts/tdarr/dv_reencode_qsv.logic.mjs (testée) ---
function detectDvProfile(ffProbeData) {
  const streams = (ffProbeData && ffProbeData.streams) || [];
  for (const s of streams) {
    if (s.codec_type !== 'video') continue;
    const rec = (s.side_data_list || []).find(
      (d) => d.side_data_type === 'DOVI configuration record');
    if (rec) return { profile: rec.dv_profile, blCompat: rec.dv_bl_signal_compatibility_id };
  }
  return { profile: null, blCompat: null };
}
function decideAction(dv) {
  if (dv.profile === 8) return 'reencode';
  if (dv.profile === 7) return 'convert-reencode';
  return 'copy';
}
// -----------------------------------------------------------------------------------------

const details = () => ({
  name: 'DV Re-encode QSV (RPU preserve)',
  description: 'Ré-encode Dolby Vision en QSV avec réinjection RPU. Fallback copy si échec.',
  style: { borderColor: 'purple' },
  tags: 'video',
  isStartPlugin: false,
  pType: '',
  requiresVersion: '2.11.01',
  sidebarPosition: -1,
  icon: 'faFilm',
  inputs: [],
  outputs: [
    { number: 1, tooltip: 'Ré-encode DV réussi (fichier remplacé)' },
    { number: 2, tooltip: 'Fallback -> router vers branche HDR (re-encode HDR10)' },
  ],
});
exports.details = details;

const plugin = async (args) => {
  const jobLog = args.jobLog || (() => {});
  const fallback = (msg) => {
    jobLog(`DV fallback -> HDR re-encode: ${msg}`);
    return { outputFileObj: args.inputFileObj, outputNumber: 2, variables: args.variables };
  };

  // Tout le corps (init comprise) est dans try/catch : aucune exception ne
  // s'échappe sans retomber sur le fallback (invariant "tout passe sans erreur").
  try {
    const lib = require('../../../../../methods/lib')();
    args.inputs = lib.loadDefaultValues(args.inputs, details);
    const src = args.inputFileObj._id;
    const work = args.workDir;
    const quality = String((args.variables && args.variables.user && args.variables.user.quality) || 19);

    const run = async (cliPath, spawnArgs, outFile) => {
      const cli = new CLI({
        cli: cliPath, spawnArgs, spawnOpts: { env: LIBENV },
        jobLog, outputFilePath: outFile, inputFileObj: args.inputFileObj,
        logFullCliOutput: true, updateWorker: args.updateWorker,
      });
      const res = await cli.runCli();
      if (res.cliExitCode !== 0) throw new Error(`${cliPath} exit ${res.cliExitCode}`);
      return res;
    };

    const dv = detectDvProfile(args.inputFileObj.ffProbeData);
    const action = decideAction(dv);
    jobLog(`DV profile=${dv.profile} action=${action}`);
    if (action === 'copy') return fallback(`profil ${dv.profile} non ré-encodable ici`);

    const inHevc = path.join(work, 'in.hevc');
    const rpu = path.join(work, 'RPU.bin');
    const baseHevc = path.join(work, 'base.hevc');
    const dvHevc = path.join(work, 'base_dv.hevc');
    const aacTmp = path.join(work, 'aac.mka');
    const outMkv = path.join(work, `${path.parse(src).name}.dv.mkv`);

    // 1. flux HEVC brut + extraction RPU (7 -> 8.1 via -m 2)
    await run('ffmpeg', ['-y', '-i', src, '-map', '0:v:0', '-c', 'copy',
      '-bsf:v', 'hevc_mp4toannexb', '-f', 'hevc', inHevc], inHevc);
    const mode = action === 'convert-reencode' ? ['-m', '2'] : [];
    await run(DOVI, [...mode, 'extract-rpu', inHevc, '-o', rpu], rpu);

    // 2. encode base QSV HEVC Main10, framerate passthrough
    await run('ffmpeg', ['-y', '-i', src, '-map', '0:v:0', '-an', '-sn',
      '-c:v', 'hevc_qsv', '-profile:v', 'main10', '-global_quality', quality,
      '-color_primaries', 'bt2020', '-color_trc', 'smpte2084', '-colorspace', 'bt2020nc',
      '-fps_mode', 'passthrough', '-f', 'hevc', baseHevc], baseHevc);

    // 3. réinjection RPU
    await run(DOVI, ['inject-rpu', '-i', baseHevc, '--rpu-in', rpu, '-o', dvHevc], dvHevc);

    // 4. piste AAC 2.0 normalisée + remux MKV (vidéo DV + toutes pistes originales + AAC)
    await run('ffmpeg', ['-y', '-i', src, '-map', '0:a:0', '-c:a', 'aac', '-ac', '2',
      '-b:a', '256k', '-filter:a', 'loudnorm=I=-16:TP=-1.5:LRA=11', aacTmp], aacTmp);
    await run(MKVMERGE, ['-o', outMkv, dvHevc,
      '--no-video', '--no-attachments', src, aacTmp], outMkv);

    // 5. validation : RPU présent + taille cohérente
    const info = await new CLI({
      cli: DOVI, spawnArgs: ['info', '-i', outMkv, '-f', '0'], spawnOpts: { env: LIBENV },
      jobLog, outputFilePath: outMkv, inputFileObj: args.inputFileObj, logFullCliOutput: true,
    }).runCli();
    if (info.cliExitCode !== 0) return fallback('dovi_tool info échec (RPU absent ?)');

    const fs = args.deps.fsextra;
    const outSize = (await fs.stat(outMkv)).size;
    const inSize = (await fs.stat(src)).size;
    if (outSize >= inSize) return fallback(`sortie ${outSize} >= source ${inSize}`);
    if (outSize < inSize * 0.05) return fallback('sortie < 5% source (tronquée ?)');

    args.inputFileObj._id = outMkv;
    args.inputFileObj.file = outMkv;
    return { outputFileObj: args.inputFileObj, outputNumber: 1, variables: args.variables };
  } catch (err) {
    return fallback(err && err.message ? err.message : String(err));
  }
};
exports.plugin = plugin;
