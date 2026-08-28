import assert from 'node:assert';
import test from 'node:test';
import { detectDvProfile, decideAction } from './dv_reencode_qsv.logic.mjs';

const withProfile = (dv_profile, extra = {}) => ({
  streams: [{ codec_type: 'video',
    side_data_list: [{ side_data_type: 'DOVI configuration record', dv_profile, ...extra }] }],
});

test('profil 8 -> reencode', () => {
  assert.equal(decideAction(detectDvProfile(withProfile(8, { dv_bl_signal_compatibility_id: 1 }))), 'reencode');
});
test('profil 7 -> convert-reencode', () => {
  assert.equal(decideAction(detectDvProfile(withProfile(7))), 'convert-reencode');
});
test('profil 5 -> copy', () => {
  assert.equal(decideAction(detectDvProfile(withProfile(5))), 'copy');
});
test('pas de DV -> copy', () => {
  assert.equal(decideAction(detectDvProfile({ streams: [{ codec_type: 'video' }] })), 'copy');
});
