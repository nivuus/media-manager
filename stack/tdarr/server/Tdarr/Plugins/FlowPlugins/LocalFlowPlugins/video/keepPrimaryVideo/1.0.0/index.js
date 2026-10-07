"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;

// Keeps only the first real video stream in the ffmpegCommand being built.
//
// The video encoder plugin encodes EVERY video stream. Some releases carry a
// second one: the Dolby Vision profile 7 enhancement layer of a dual-track
// remux, or a whole alternate 1080p version bundled with the 4K one. Encoded
// on its own, the enhancement layer is a meaningless picture, and the
// alternate version is a duplicate the library never plays. Cover images are
// not video for this purpose: ffmpegCommandStart already turns attached
// pictures into attachments, and image codecs are left in place.

const IMAGE_CODECS = ['mjpeg', 'png', 'bmp', 'gif', 'webp'];

const details = () => ({
  name: 'Keep Primary Video',
  description: 'Drops every video stream but the first one (cover images excepted) from the ffmpeg command.',
  style: { borderColor: '#6efe6e' },
  tags: 'video',
  isStartPlugin: false,
  pType: '',
  requiresVersion: '2.11.01',
  sidebarPosition: -1,
  icon: '',
  inputs: [],
  outputs: [{ number: 1, tooltip: 'Continue to next plugin' }],
});
exports.details = details;

const plugin = (args) => {
  const lib = require('../../../../../methods/lib')();
  args.inputs = lib.loadDefaultValues(args.inputs, details);
  const command = args.variables.ffmpegCommand;
  if (!command || !command.init) {
    throw new Error('keepPrimaryVideo must run after "Begin Command" (ffmpegCommandStart)');
  }
  let seenPrimary = false;
  command.streams.forEach((stream) => {
    if (stream.removed || stream.codec_type !== 'video' || IMAGE_CODECS.includes(stream.codec_name)) return;
    if (!seenPrimary) {
      seenPrimary = true;
      return;
    }
    args.jobLog(`Dropping secondary video stream ${stream.index} (${stream.codec_name} ${stream.width}x${stream.height})`);
    stream.removed = true;
  });
  return { outputFileObj: args.inputFileObj, outputNumber: 1, variables: args.variables };
};
exports.plugin = plugin;
