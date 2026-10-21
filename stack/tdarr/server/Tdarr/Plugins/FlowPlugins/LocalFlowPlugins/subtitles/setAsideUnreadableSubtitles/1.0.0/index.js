"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = exports.VARIABLE = void 0;

// Takes the subtitle tracks ffmpeg cannot read out of the ffmpegCommand being
// built, and records them so the flow can put them back with mkvmerge once
// ffmpeg is done.
//
// FFmpeg's Matroska demuxer knows WebVTT only under the WebM codec IDs
// (D_WEBVTT/*), not under S_TEXT/WEBVTT, the ID the Matroska specification
// and mkvmerge use (checked on release/7.1, release/8.0 and master). Such a
// track probes with no codec, and copying it makes the muxer refuse to write
// the header ("Subtitle codec 0 is not supported"), so the whole job fails.
// Dropping the track would lose the subtitles; mkvmerge reads it fine.
//
// The recorded value is the ffprobe stream index of each track, which is its
// mkvmerge track ID: Matroska tracks come first, in file order, in both tools
// (cover images and other attachments are listed after them by ffprobe).

const VARIABLE = 'unreadableSubtitleTracks';
exports.VARIABLE = VARIABLE;

const details = () => ({
  name: 'Set Aside Unreadable Subtitles',
  description: 'Removes subtitle tracks ffmpeg has no codec for (Matroska WebVTT) from the ffmpeg command '
    + `and lists their track IDs in the flow variable "${VARIABLE}" (empty when there are none), `
    + 'for an mkvmerge step to restore them from the original file.',
  style: { borderColor: '#fefe6e' },
  tags: 'subtitle',
  isStartPlugin: false,
  pType: '',
  requiresVersion: '2.11.01',
  sidebarPosition: -1,
  icon: '',
  inputs: [],
  outputs: [{ number: 1, tooltip: 'Continue to next plugin' }],
});
exports.details = details;

const isUnreadable = (stream) => stream.codec_type === 'subtitle'
  && (!stream.codec_name || stream.codec_name === 'unknown');

const plugin = (args) => {
  const lib = require('../../../../../methods/lib')();
  args.inputs = lib.loadDefaultValues(args.inputs, details);
  const command = args.variables.ffmpegCommand;
  if (!command || !command.init) {
    throw new Error('setAsideUnreadableSubtitles must run after "Begin Command" (ffmpegCommandStart)');
  }
  const setAside = command.streams.filter((s) => !s.removed && isUnreadable(s));
  setAside.forEach((stream) => {
    const lang = (stream.tags || {}).language || 'und';
    args.jobLog(`Setting aside subtitle track ${stream.index} (${lang}): ffmpeg has no codec for it, mkvmerge restores it`);
    stream.removed = true;
  });
  // Always written, so a value from an earlier step never leaks into this one.
  args.variables.user = Object.assign({}, args.variables.user, {
    [VARIABLE]: setAside.map((s) => s.index).join(','),
  });
  return { outputFileObj: args.inputFileObj, outputNumber: 1, variables: args.variables };
};
exports.plugin = plugin;
