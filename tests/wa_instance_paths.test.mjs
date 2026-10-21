import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { resolveWhatsAppInstancePaths } from '../wa_instance_paths.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pecados-paths-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const own = path.join(root, 'pecados');
  const other = path.join(root, 'fiesteras');
  fs.mkdirSync(own);
  fs.mkdirSync(other);
  return { own: fs.realpathSync(own), other: fs.realpathSync(other) };
}

test('defaults are rooted in the module checkout, independent of cwd', (t) => {
  const { own } = fixture(t);
  const paths = resolveWhatsAppInstancePaths(own, {});
  assert.equal(paths.baseDir, own);
  assert.equal(paths.authDir, path.join(own, 'data', 'wa_auth'));
  assert.equal(paths.officialImage, path.join(own, 'assets', 'pecadosvip-profile-logo.png'));
});

test('a BOT_DIR for another brand fails before any worker is started', (t) => {
  const { own, other } = fixture(t);
  assert.throws(() => resolveWhatsAppInstancePaths(own, { BOT_DIR: other }), /instance_root_mismatch/);
  assert.equal(resolveWhatsAppInstancePaths(own, { BOT_DIR: own }).baseDir, own);
});

test('all inherited data overrides reject another checkout and prefix siblings', (t) => {
  const { own, other } = fixture(t);
  for (const name of ['WA_AUTH_DIR', 'WA_QR_PATH', 'WA_HEALTH_FILE', 'WA_SAFETY_HEALTH_FILE',
    'WA_SAFETY_CONTROL_FILE', 'WA_INTERACTION_STATE_FILE', 'WA_VOICE_NOTE_CACHE_DIR',
    'WA_PROFILE_PICTURE_PATH', 'WA_PROFILE_PICTURE_STATE_FILE', 'WA_IDENTITY_FILE']) {
    assert.throws(() => resolveWhatsAppInstancePaths(own, { [name]: path.join(other, 'data', 'target') }), /instance_path_mismatch/, name);
    assert.throws(() => resolveWhatsAppInstancePaths(own, { [name]: `${own}-other/target` }), /instance_path_mismatch/, name);
  }
});

test('candidate paths and relative paths inside the instance remain valid', (t) => {
  const { own } = fixture(t);
  const paths = resolveWhatsAppInstancePaths(own, {
    WA_AUTH_DIR: 'data/wa_switch/candidate_auth',
    WA_QR_PATH: 'data/wa_switch/candidate_qr.png',
    WA_IDENTITY_FILE: 'data/wa_switch/identity.json',
  });
  assert.equal(paths.authDir, path.join(own, 'data', 'wa_switch', 'candidate_auth'));
  assert.equal(paths.qrPath, path.join(own, 'data', 'wa_switch', 'candidate_qr.png'));
});

test('junctions cannot redirect sessions or the entire data directory', (t) => {
  const { own, other } = fixture(t);
  fs.mkdirSync(path.join(own, 'data'));
  fs.symlinkSync(other, path.join(own, 'data', 'wa_auth'), 'junction');
  assert.throws(() => resolveWhatsAppInstancePaths(own, {}), /instance_path_mismatch/);
  fs.unlinkSync(path.join(own, 'data', 'wa_auth'));
  fs.rmdirSync(path.join(own, 'data'));
  fs.symlinkSync(other, path.join(own, 'data'), 'junction');
  assert.throws(() => resolveWhatsAppInstancePaths(own, {}), /instance_data_redirected/);
});

test('a shared audio directory also fails closed', (t) => {
  const { own, other } = fixture(t);
  fs.mkdirSync(path.join(own, 'data'));
  fs.symlinkSync(other, path.join(own, 'data', 'audios'), 'junction');
  assert.throws(() => resolveWhatsAppInstancePaths(own, {}), /instance_path_mismatch/);
});

test('nested credentials and photo requests cannot be redirected either', (t) => {
  const { own, other } = fixture(t);
  const auth = path.join(own, 'data', 'wa_auth');
  fs.mkdirSync(auth, { recursive: true });
  fs.symlinkSync(other, path.join(auth, 'nested'), 'junction');
  assert.throws(() => resolveWhatsAppInstancePaths(own, {}), /instance_path_mismatch/);
  fs.unlinkSync(path.join(auth, 'nested'));
  fs.symlinkSync(other, path.join(own, 'data', 'wa_profile_picture_request.json'), 'junction');
  assert.throws(() => resolveWhatsAppInstancePaths(own, {}), /instance_path_mismatch/);
});

test('a dangling data link is not mistaken for an uncreated local directory', (t) => {
  const { own, other } = fixture(t);
  fs.symlinkSync(path.join(other, 'missing'), path.join(own, 'data'), 'junction');
  assert.throws(() => resolveWhatsAppInstancePaths(own, {}), /instance_path_unavailable/);
});
