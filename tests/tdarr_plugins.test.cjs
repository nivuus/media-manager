// Local Tdarr flow plugins, driven through their real plugin() entry point
// with ffprobe-shaped streams modelled on files seen in the library.
//
// Run: node --test tests/tdarr_plugins.test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const Module = require('node:module');
const path = require('node:path');

const PLUGINS = path.join(__dirname, '..', 'stack', 'tdarr', 'server', 'Tdarr', 'Plugins',
  'FlowPlugins', 'LocalFlowPlugins');

// Tdarr injects `methods/lib` at runtime; the plugins only use loadDefaultValues.
const realResolve = Module._resolveFilename;
Module._resolveFilename = function resolve(request, ...rest) {
  if (request.endsWith('/methods/lib')) return 'tdarr-methods-lib';
  return realResolve.call(this, request, ...rest);
};
require.cache['tdarr-methods-lib'] = {
  id: 'tdarr-methods-lib', filename: 'tdarr-methods-lib', loaded: true,
  exports: () => ({
    loadDefaultValues: (inputs, details) => Object.fromEntries(
      details().inputs.map((i) => [i.name, (inputs || {})[i.name] ?? i.defaultValue])),
  }),
};

const stereo = require(path.join(PLUGINS, 'audio/ensureStereoPerLanguage/1.0.0/index.js'));
const keepVideo = require(path.join(PLUGINS, 'video/keepPrimaryVideo/1.0.0/index.js'));

const video = (index, extra = {}) => ({ index, codec_type: 'video', codec_name: 'hevc', width: 3840, height: 2160, ...extra });
const audio = (index, lang, codec, channels, extra = {}) => ({
  index, codec_type: 'audio', codec_name: codec, channels, sample_rate: '48000',
  tags: lang ? { language: lang, ...(extra.tags || {}) } : (extra.tags || undefined),
  disposition: { default: 0, comment: 0, ...(extra.disposition || {}) },
});
const sub = (index, lang) => ({ index, codec_type: 'subtitle', codec_name: 'subrip', tags: { language: lang }, disposition: { default: 0 } });

// Mirrors ffmpegCommandStart: every probed stream becomes a mapped copy.
const run = (plugin, probed, shouldProcess = false) => {
  const logs = [];
  const args = {
    inputs: {},
    inputFileObj: { _id: '/media/x.mkv' },
    jobLog: (m) => logs.push(m),
    variables: {
      ffmpegCommand: {
        init: true,
        shouldProcess,
        streams: probed.map((s) => ({ ...s, removed: false, mapArgs: ['-map', `0:${s.index}`], inputArgs: [], outputArgs: [] })),
      },
    },
  };
  const result = plugin.plugin(args);
  return { result, streams: args.variables.ffmpegCommand.streams, command: args.variables.ffmpegCommand, logs };
};
const live = (streams) => streams.filter((s) => !s.removed);
const audioOut = (streams) => live(streams).filter((s) => s.codec_type === 'audio');
const arg = (stream, name) => stream.outputArgs[stream.outputArgs.indexOf(`${name}:{outputIndex}`) + 1];
const added = (streams) => streams.filter((s) => s.mapArgs[1] !== `0:${s.index}`);
const isDefaultAfter = (s) => {
  const d = arg(s, '-disposition');
  if (d === '+default') return true;
  if (d === '-default') return false;
  return Number((s.disposition || {}).default) === 1;
};

test('legacy file: 96 kHz track rebuilt, every language covered, one default', () => {
  // Casino Royale as the previous plugin left it.
  const { result, streams } = run(stereo, [
    audio(0, 'eng', 'aac', 2, { tags: { title: 'AAC 2.0 (normalisé)' }, disposition: { default: 1 } }),
    video(1),
    audio(3, 'eng', 'dts', 6, { disposition: { default: 1 } }),
    audio(5, 'fre', 'dts', 6),
    audio(12, 'eng', 'ac3', 2, { disposition: { comment: 1 } }),
    sub(13, 'eng'),
  ]);
  assert.equal(result.outputNumber, 1);
  assert.equal(streams.find((s) => s.index === 0).removed, true);
  const news = added(streams);
  assert.deepEqual(news.map((s) => [s.tags.language, s.mapArgs[1]]), [['eng', '0:3'], ['fre', '0:5']]);
  news.forEach((s) => {
    assert.equal(arg(s, '-c'), 'aac');
    assert.equal(arg(s, '-ar'), '48000');
    assert.equal(arg(s, '-metadata:s'), 'title=AAC 2.0 (normalized)');
    assert.match(arg(s, '-filter'), /^aformat=channel_layouts=stereo,loudnorm=.*,aresample=48000$/);
  });
  assert.deepEqual(audioOut(streams).filter(isDefaultAfter).map((s) => s.mapArgs[1]), ['0:3']);
  assert.equal(isDefaultAfter(news[0]), true);
  // Video stays first; new tracks lead the audio tracks.
  assert.deepEqual(live(streams).map((s) => s.codec_type).slice(0, 3), ['video', 'audio', 'audio']);
});

test('already compliant file is left untouched', () => {
  const { result, command } = run(stereo, [
    video(0),
    audio(1, 'fre', 'aac', 2, { tags: { title: 'AAC 2.0 (normalized)' }, disposition: { default: 1 } }),
    audio(2, 'fre', 'opus', 8),
  ]);
  assert.equal(result.outputNumber, 2);
  assert.equal(command.shouldProcess, false);
});

test('two default tracks are reduced to one without re-adding audio', () => {
  const { result, streams } = run(stereo, [
    video(0),
    audio(1, 'fre', 'aac', 2, { tags: { title: 'AAC 2.0 (normalized)' }, disposition: { default: 1 } }),
    audio(2, 'fre', 'opus', 8, { disposition: { default: 1 } }),
  ]);
  assert.equal(result.outputNumber, 1);
  assert.equal(added(streams).length, 0);
  assert.deepEqual(audioOut(streams).filter(isDefaultAfter).map((s) => s.index), [1]);
});

test('an original AAC stereo track covers its language; only the others get one', () => {
  const { streams } = run(stereo, [
    video(0),
    audio(1, 'fre', 'aac', 2, { disposition: { default: 1 } }),
    audio(2, 'eng', 'eac3', 6),
  ]);
  const news = added(streams);
  assert.deepEqual(news.map((s) => s.tags.language), ['eng']);
  assert.equal(isDefaultAfter(news[0]), false);
  assert.deepEqual(audioOut(streams).filter(isDefaultAfter).map((s) => s.index), [1]);
});

test('every language tag is handled the same way, untagged included', () => {
  const { streams } = run(stereo, [
    video(0),
    audio(1, 'jpn', 'flac', 6, { disposition: { default: 1 } }),
    audio(2, 'ara', 'eac3', 6),
    audio(3, 'kor', 'truehd', 8),
    audio(4, undefined, 'ac3', 6),
  ]);
  assert.deepEqual(added(streams).map((s) => (s.tags || {}).language || 'und'), ['jpn', 'ara', 'kor', 'und']);
});

test('duplicate normalized tracks of one language: the extra one is dropped', () => {
  const { streams } = run(stereo, [
    video(0),
    audio(1, 'fre', 'aac', 2, { tags: { title: 'AAC 2.0 (normalized)' }, disposition: { default: 1 } }),
    audio(2, 'fre', 'aac', 2, { tags: { title: 'AAC 2.0 (normalized)' } }),
    audio(3, 'fre', 'eac3', 6),
  ]);
  assert.deepEqual(streams.filter((s) => s.removed).map((s) => s.index), [2]);
  assert.equal(added(streams).length, 0);
});

test('a language present only as commentary gets no track', () => {
  const { streams } = run(stereo, [
    video(0),
    audio(1, 'fre', 'eac3', 6, { disposition: { default: 1 } }),
    audio(2, 'eng', 'ac3', 2, { disposition: { comment: 1 } }),
  ]);
  assert.deepEqual(added(streams).map((s) => s.tags.language), ['fre']);
});

test('a mono source gives a mono AAC track, not an upmix', () => {
  const { streams } = run(stereo, [video(0), audio(1, 'ita', 'mp2', 1, { disposition: { default: 1 } })]);
  const [only] = added(streams);
  assert.equal(arg(only, '-ac'), '1');
  assert.match(arg(only, '-filter'), /^aformat=channel_layouts=mono,/);
});

test('the richest default track of a language is the downmix source', () => {
  const { streams } = run(stereo, [
    video(0),
    audio(1, 'eng', 'ac3', 2),
    audio(2, 'eng', 'truehd', 8, { disposition: { default: 1 } }),
    audio(3, 'eng', 'dts', 6),
  ]);
  assert.equal(added(streams)[0].mapArgs[1], '0:2');
});

test('on the encode path the command already runs: output 1 either way', () => {
  const { result } = run(stereo, [
    video(0),
    audio(1, 'fre', 'aac', 2, { tags: { title: 'AAC 2.0 (normalized)' }, disposition: { default: 1 } }),
  ], true);
  assert.equal(result.outputNumber, 1);
});

test('keepPrimaryVideo drops a second video track but keeps cover images', () => {
  const { streams } = run(keepVideo, [
    video(0),
    video(1, { width: 1920, height: 1080 }),
    audio(2, 'eng', 'dts', 6),
    video(3, { codec_name: 'mjpeg', width: 600, height: 882 }),
  ]);
  assert.deepEqual(streams.filter((s) => s.removed).map((s) => s.index), [1]);
});
