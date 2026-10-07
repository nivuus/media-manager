"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.planStereoTracks = exports.details = void 0;

// Guarantees one loudness-normalised AAC stereo track per audio language, as
// an ffmpegCommand builder: it only edits args.variables.ffmpegCommand, so on
// the encode path the audio work rides in the same ffmpeg pass as the video,
// and on the compatibility path a file that is already right is not touched.
//
// Languages are never named here. Tracks are grouped by whatever language tag
// they carry (untagged = "und"), so a file in any language gets the same
// treatment. Accessory tracks (commentary, audio description) are recognised
// by their disposition flags, never by their title.

const OWN_TITLE = 'AAC 2.0 (normalized)';
// Tracks written by the previous plugin (addNormalizedStereo). They are ours
// but non-compliant: loudnorm resamples to 192 kHz and nothing brought them
// back, so every one of them is AAC at 96 kHz. They are rebuilt.
const LEGACY_OWN_TITLES = ['AAC 2.0 (normalisé)'];
const SAMPLE_RATE = 48000;

const details = () => ({
  name: 'Ensure Stereo Per Language',
  description: 'Adds a loudness-normalised AAC stereo track for every audio language that lacks one, '
    + 'rebuilds non-compliant tracks from earlier runs, and leaves exactly one default audio track.',
  style: { borderColor: '#6efefc' },
  tags: 'audio',
  isStartPlugin: false,
  pType: '',
  requiresVersion: '2.11.01',
  sidebarPosition: -1,
  icon: '',
  inputs: [
    { label: 'Integrated Loudness (LUFS)', name: 'i', type: 'string', defaultValue: '-16.0', inputUI: { type: 'text' }, tooltip: 'Integrated loudness target' },
    { label: 'Loudness Range (LU)', name: 'lra', type: 'string', defaultValue: '11.0', inputUI: { type: 'text' }, tooltip: 'Loudness range target' },
    { label: 'True Peak (dBTP)', name: 'tp', type: 'string', defaultValue: '-1.5', inputUI: { type: 'text' }, tooltip: 'Maximum true peak' },
    { label: 'Bitrate', name: 'bitrate', type: 'string', defaultValue: '256k', inputUI: { type: 'text' }, tooltip: 'Bitrate of each added AAC track' },
  ],
  outputs: [
    { number: 1, tooltip: 'The ffmpeg command has work to do' },
    { number: 2, tooltip: 'Nothing to change: the file is already as required' },
  ],
});
exports.details = details;

const flag = (stream, name) => Number((stream.disposition || {})[name]) === 1;
const title = (stream) => ((stream.tags || {}).title || '');
const langKey = (stream) => String((stream.tags || {}).language || 'und').toLowerCase();
const isAccessory = (s) => flag(s, 'comment') || flag(s, 'visual_impaired') || flag(s, 'descriptions');
const isOwn = (s) => title(s) === OWN_TITLE || LEGACY_OWN_TITLES.includes(title(s));
const isCompliantOwn = (s) => title(s) === OWN_TITLE && s.codec_name === 'aac'
  && Number(s.channels) <= 2 && Number(s.sample_rate) === SAMPLE_RATE;
const isStereoAac = (s) => s.codec_name === 'aac' && Number(s.channels) <= 2 && !isAccessory(s);

// Best source for a language: the default track, then the most channels,
// then the earliest. Downmixing from the richest mix gives the best stereo.
const bestSource = (candidates) => candidates.slice().sort((a, b) => (
  (Number(flag(b, 'default')) - Number(flag(a, 'default')))
  || (Number(b.channels) - Number(a.channels))
  || (a.order - b.order)))[0];

// Pure planning step, exported for tests. `streams` are ffprobe-shaped stream
// objects (ffmpegCommand streams); removed ones are ignored. Returns which
// streams to drop, which tracks to add (in output order) and the stream that
// must end up as the single default audio track.
const planStereoTracks = (streams) => {
  const audio = streams
    .map((s, order) => Object.assign({}, s, { order }))
    .filter((s) => s.codec_type === 'audio' && !s.removed);
  const remove = [];
  const keptOwnByLang = {};
  audio.filter(isOwn).forEach((s) => {
    if (!isCompliantOwn(s) || keptOwnByLang[langKey(s)]) {
      remove.push(s.index);
    } else {
      keptOwnByLang[langKey(s)] = s;
    }
  });
  const kept = audio.filter((s) => !remove.includes(s.index));
  const sources = kept.filter((s) => !isOwn(s) && !isAccessory(s));
  const covered = new Set(kept.filter(isStereoAac).map(langKey));

  const primarySource = sources.find((s) => flag(s, 'default')) || sources[0];
  const primary = primarySource ? langKey(primarySource) : null;
  const languages = [];
  sources.forEach((s) => { if (!languages.includes(langKey(s))) languages.push(langKey(s)); });
  if (primary) languages.sort((a, b) => Number(b === primary) - Number(a === primary));

  const add = languages
    .filter((lang) => !covered.has(lang))
    .map((lang) => {
      const source = bestSource(sources.filter((s) => langKey(s) === lang));
      return { lang, sourceIndex: source.index, channels: Math.min(2, Number(source.channels) || 2) };
    });

  // The stereo track of the primary language is the default, as the previous
  // plugin intended; without one, the first existing default is kept. Either
  // way exactly one audio track ends up flagged default.
  let defaultTarget = null;
  const primaryAdd = add.find((a) => a.lang === primary);
  if (primaryAdd) {
    defaultTarget = { added: primaryAdd.lang };
  } else if (primary && keptOwnByLang[primary]) {
    defaultTarget = { index: keptOwnByLang[primary].index };
  } else {
    const firstDefault = kept.find((s) => flag(s, 'default'));
    if (firstDefault) defaultTarget = { index: firstDefault.index };
  }
  return { remove, add, defaultTarget };
};
exports.planStereoTracks = planStereoTracks;

// New tracks lead the audio tracks but never precede the video: the previous
// plugin wrote its track at stream 0, ahead of the video, and that track may
// be the one being dropped.
const insertPosition = (streams) => {
  const liveAt = (type) => streams
    .map((s, pos) => ((s.codec_type === type && !s.removed) ? pos : -1))
    .filter((pos) => pos !== -1);
  const videos = liveAt('video');
  const audios = liveAt('audio');
  if (videos.length === 0) return audios.length > 0 ? audios[0] : streams.length;
  const audioAfterVideo = audios.find((pos) => pos > videos[0]);
  return audioAfterVideo !== undefined ? audioAfterVideo : videos[videos.length - 1] + 1;
};

const plugin = (args) => {
  const lib = require('../../../../../methods/lib')();
  args.inputs = lib.loadDefaultValues(args.inputs, details);
  const command = args.variables.ffmpegCommand;
  if (!command || !command.init) {
    throw new Error('ensureStereoPerLanguage must run after "Begin Command" (ffmpegCommandStart)');
  }
  const { i, lra, tp, bitrate } = args.inputs;
  const streams = command.streams;
  const plan = planStereoTracks(streams);

  plan.remove.forEach((index) => {
    const stream = streams.find((s) => s.index === index);
    stream.removed = true;
    args.jobLog(`Dropping non-compliant normalised track (stream ${index}, "${title(stream)}", ${stream.sample_rate} Hz)`);
  });

  let nextIndex = Math.max(...streams.map((s) => Number(s.index))) + 1;
  const added = plan.add.map((a) => {
    const source = streams.find((s) => s.index === a.sourceIndex);
    const layout = a.channels === 1 ? 'mono' : 'stereo';
    const isDefault = Boolean(plan.defaultTarget && plan.defaultTarget.added === a.lang);
    args.jobLog(`Adding ${layout} AAC for language "${a.lang}" from stream ${a.sourceIndex} (${source.codec_name}, ${source.channels} ch)`);
    const stream = JSON.parse(JSON.stringify(source));
    Object.assign(stream, {
      index: nextIndex,
      removed: false,
      codec_name: 'aac',
      channels: a.channels,
      sample_rate: String(SAMPLE_RATE),
      tags: Object.assign({}, source.tags, { title: OWN_TITLE }),
      disposition: Object.assign({}, source.disposition, { default: isDefault ? 1 : 0 }),
      mapArgs: ['-map', `0:${a.sourceIndex}`],
      inputArgs: [],
      outputArgs: [
        '-c:{outputIndex}', 'aac',
        '-b:{outputIndex}', String(bitrate),
        '-ac:{outputIndex}', String(a.channels),
        '-ar:{outputIndex}', String(SAMPLE_RATE),
        // Downmix first, then measure: loudness is normalised on what is heard.
        '-filter:{outputIndex}', `aformat=channel_layouts=${layout},loudnorm=I=${i}:TP=${tp}:LRA=${lra},aresample=${SAMPLE_RATE}`,
        '-disposition:{outputIndex}', isDefault ? '+default' : '-default',
        '-metadata:s:{outputIndex}', `title=${OWN_TITLE}`,
      ],
    });
    nextIndex += 1;
    return stream;
  });
  if (added.length > 0) {
    streams.splice(insertPosition(streams), 0, ...added);
  }

  let changed = plan.remove.length > 0 || added.length > 0;
  // With no target (no track flagged default anywhere), defaults are left alone.
  const target = plan.defaultTarget;
  streams
    .filter((s) => s.codec_type === 'audio' && !s.removed && !added.includes(s))
    .forEach((s) => {
      const want = target ? target.index === s.index : flag(s, 'default');
      if (flag(s, 'default') === want) return;
      s.outputArgs.push('-disposition:{outputIndex}', want ? '+default' : '-default');
      changed = true;
    });

  if (changed) command.shouldProcess = true;
  return {
    outputFileObj: args.inputFileObj,
    outputNumber: command.shouldProcess ? 1 : 2,
    variables: args.variables,
  };
};
exports.plugin = plugin;
