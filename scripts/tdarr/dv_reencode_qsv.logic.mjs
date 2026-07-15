export function detectDvProfile(ffProbeData) {
  const streams = (ffProbeData && ffProbeData.streams) || [];
  for (const s of streams) {
    if (s.codec_type !== 'video') continue;
    const rec = (s.side_data_list || []).find(
      (d) => d.side_data_type === 'DOVI configuration record');
    if (rec) return { profile: rec.dv_profile, blCompat: rec.dv_bl_signal_compatibility_id };
  }
  return { profile: null, blCompat: null };
}

export function decideAction(dv) {
  if (dv.profile === 8) return 'reencode';
  if (dv.profile === 7) return 'convert-reencode';
  return 'copy'; // profil 5/4, ou pas de DV -> copy sûr
}
