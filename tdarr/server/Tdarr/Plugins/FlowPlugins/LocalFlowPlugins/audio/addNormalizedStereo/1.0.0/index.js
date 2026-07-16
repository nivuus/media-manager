"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
const cliUtils = require("../../../../FlowHelpers/1.0.0/cliUtils");
const fileUtils = require("../../../../FlowHelpers/1.0.0/fileUtils");

// Ajoute une piste AAC stéréo normalisée (loudnorm, single-pass) en a:0 et
// COPIE toutes les pistes originales (Atmos/DTS-HD préservés). Passe standalone
// (contrôle total de la commande, aucun conflit avec le builder ffmpegCommand).
// Commande validée manuellement avant intégration. Fallback : si l'ffmpeg
// échoue, on renvoie le fichier d'entrée inchangé (invariant "sans erreur").

const details = () => ({
  name: 'Add Normalized Stereo (AAC)',
  description: 'Ajoute une piste AAC stéréo normalisée (loudnorm) en gardant toutes les pistes originales.',
  style: { borderColor: '#6efefc' },
  tags: 'video',
  isStartPlugin: false,
  pType: '',
  requiresVersion: '2.11.01',
  sidebarPosition: -1,
  icon: '',
  inputs: [
    { label: 'Integrated Loudness (LUFS)', name: 'i', type: 'string', defaultValue: '-16.0', inputUI: { type: 'text' }, tooltip: 'Cible loudness intégrée' },
    { label: 'Loudness Range (LU)', name: 'lra', type: 'string', defaultValue: '11.0', inputUI: { type: 'text' }, tooltip: 'Plage de loudness' },
    { label: 'True Peak (dBTP)', name: 'tp', type: 'string', defaultValue: '-1.5', inputUI: { type: 'text' }, tooltip: 'True peak max' },
    { label: 'Bitrate', name: 'bitrate', type: 'string', defaultValue: '256k', inputUI: { type: 'text' }, tooltip: 'Bitrate de la piste AAC stéréo' },
  ],
  outputs: [
    { number: 1, tooltip: 'Continue' },
  ],
});
exports.details = details;

const plugin = async (args) => {
  const lib = require('../../../../../methods/lib')();
  args.inputs = lib.loadDefaultValues(args.inputs, details);
  const jobLog = args.jobLog || (() => {});
  const keep = () => ({ outputFileObj: args.inputFileObj, outputNumber: 1, variables: args.variables });

  try {
    const { i, lra, tp, bitrate } = args.inputs;
    const container = fileUtils.getContainer(args.inputFileObj._id);
    const outputFilePath = `${fileUtils.getPluginWorkDir(args)}/${fileUtils.getFileName(args.inputFileObj._id)}.${container}`;
    const spawnArgs = [
      '-y', '-i', args.inputFileObj._id,
      '-map', '0:a:0', '-map', '0',
      '-c', 'copy',
      '-c:a:0', 'aac', '-ac:0', '2', '-b:a:0', String(bitrate),
      '-filter:a:0', `loudnorm=I=${i}:TP=${tp}:LRA=${lra}`,
      '-disposition:a:0', 'default',
      '-max_muxing_queue_size', '9999',
      outputFilePath,
    ];
    const cli = new cliUtils.CLI({
      cli: args.ffmpegPath,
      spawnArgs,
      spawnOpts: {},
      jobLog,
      outputFilePath,
      inputFileObj: args.inputFileObj,
      logFullCliOutput: args.logFullCliOutput,
      updateWorker: args.updateWorker,
      args,
    });
    const res = await cli.runCli();
    if (res.cliExitCode !== 0) {
      jobLog('addNormalizedStereo: ffmpeg a échoué -> fichier conservé inchangé');
      return keep();
    }
    return { outputFileObj: { _id: outputFilePath }, outputNumber: 1, variables: args.variables };
  } catch (err) {
    jobLog(`addNormalizedStereo: exception -> fichier conservé (${err && err.message ? err.message : err})`);
    return keep();
  }
};
exports.plugin = plugin;
