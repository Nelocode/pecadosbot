import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { applyWhatsAppProfilePicture, pollWhatsAppProfilePicture, whatsappProfileAccountHash } from '../wa_profile_picture.mjs';

const JID = '573001234567:4@s.whatsapp.net';
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-profile-picture-'));
  const imagePath = path.join(directory, 'logo.jpg');
  const statePath = path.join(directory, 'state.json');
  const requestPath = path.join(directory, 'wa_profile_picture_request.json');
  const image = Buffer.from('test-logo');
  fs.writeFileSync(imagePath, image);
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = {
    jid: JID,
    imagePath,
    statePath,
    requestPath,
    expectedImageHash: hash(image),
    updateProfilePicture: async () => {},
    logger: {},
    now: () => 1_000,
  };
  const queue = (jid = JID, imageHash = options.expectedImageHash, requestedAt = 1_000, extra = {}) => {
    const request = {
      brand: 'pecadosvip', status: 'pending', requestId: crypto.randomUUID().replaceAll('-', ''),
      accountHash: whatsappProfileAccountHash(jid), imageHash,
      requestedAt, expiresAt: requestedAt + 600_000, ...extra,
    };
    fs.writeFileSync(requestPath, JSON.stringify(request));
    return request;
  };
  queue();
  const read = (jid = JID) => JSON.parse(fs.readFileSync(statePath, 'utf8')).accounts[whatsappProfileAccountHash(jid)];
  return { directory, imagePath, statePath, requestPath, image, options, read, queue };
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test('ordinary startup and reconnect without a panel request never change a profile', async t => {
  const files = fixture(t);
  fs.rmSync(files.requestPath);
  let calls = 0;
  const options = { ...files.options, updateProfilePicture: async () => { calls += 1; } };
  for (let restart = 0; restart < 3; restart += 1) {
    assert.equal((await applyWhatsAppProfilePicture(options)).status, 'not_requested');
  }
  assert.equal(calls, 0);
  assert.equal(fs.existsSync(files.statePath), false);
});

test('a request authorizes only its exact account, PecadosVIP brand, image and lifetime', async t => {
  const files = fixture(t);
  let calls = 0;
  const options = { ...files.options, updateProfilePicture: async () => { calls += 1; } };
  files.queue('573000000000@s.whatsapp.net');
  assert.equal((await applyWhatsAppProfilePicture(options)).status, 'not_requested');
  files.queue(JID, options.expectedImageHash, 1_000, { brand: 'las-fiesteras' });
  assert.equal((await applyWhatsAppProfilePicture(options)).errorCode, 'request_invalid');
  files.queue(JID, options.expectedImageHash, -600_000);
  assert.equal((await applyWhatsAppProfilePicture(options)).errorCode, 'request_expired');
  files.queue(JID, hash('other-logo'));
  assert.equal((await applyWhatsAppProfilePicture(options)).errorCode, 'request_image_changed');
  assert.equal(calls, 0);
});

test('a definite rejected request does not retry itself after its cooldown', async t => {
  const files = fixture(t);
  let calls = 0;
  const options = { ...files.options, updateProfilePicture: async () => {
    calls += 1;
    throw Object.assign(new Error('rejected'), { data: 403 });
  } };
  await applyWhatsAppProfilePicture(options);
  assert.equal((await applyWhatsAppProfilePicture({ ...options, now: () => 3_602_000 })).errorCode, 'request_expired');
  assert.equal(calls, 1);
});

test('poller picks up a post-link request and stops on disconnect', async t => {
  const files = fixture(t);
  fs.rmSync(files.requestPath);
  let active = true;
  let reads = 0;
  let calls = 0;
  const poller = pollWhatsAppProfilePicture({
    isActive: () => active,
    getOptions: () => { reads += 1; return { ...files.options, updateProfilePicture: async () => { calls += 1; } }; },
    intervalMs: 5, durationMs: 100,
  });
  t.after(() => poller.stop());
  assert.equal(calls, 0);
  files.queue();
  await new Promise(resolve => setTimeout(resolve, 25));
  assert.equal(calls, 1);
  active = false;
  await new Promise(resolve => setTimeout(resolve, 10));
  const stoppedReads = reads;
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(reads, stoppedReads);
});

test('poller has a finite lifetime and explicit shutdown cancels its timer', async t => {
  const files = fixture(t);
  fs.rmSync(files.requestPath);
  let reads = 0;
  const poller = pollWhatsAppProfilePicture({
    isActive: () => true,
    getOptions: () => { reads += 1; return files.options; },
    intervalMs: 5, durationMs: 10,
  });
  t.after(() => poller.stop());
  await new Promise(resolve => setTimeout(resolve, 30));
  const boundedReads = reads;
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(reads, boundedReads);
  poller.stop();
});

test('provider void ACK is recorded once per normalized account without the phone number', async t => {
  const files = fixture(t);
  const calls = [];
  const options = {
    ...files.options,
    updateProfilePicture: async (...args) => {
      assert.equal(files.read().status, 'pending');
      calls.push(args);
      // Baileys resolves void when the provider acknowledges the update.
    },
  };

  assert.deepEqual(await applyWhatsAppProfilePicture(options), {
    status: 'applied', verification: 'provider_acknowledged',
  });
  assert.deepEqual(await applyWhatsAppProfilePicture({
    ...options, jid: '573001234567:9@s.whatsapp.net',
  }), { status: 'unchanged', verification: 'provider_acknowledged' });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], JID);
  assert.deepEqual(calls[0][1], files.image);
  assert.deepEqual(calls[0][2], { width: 640, height: 640 });
  assert.equal(files.read().verification, 'provider_acknowledged');
  assert.equal(files.read().appliedAt, 1_000);
  assert.equal(files.read().imageHash, files.options.expectedImageHash);
  assert.equal(fs.readFileSync(files.statePath, 'utf8').includes('573001234567'), false);
  assert.equal(whatsappProfileAccountHash(' 573001234567:8@S.WHATSAPP.NET '), whatsappProfileAccountHash(JID));
  assert.equal(whatsappProfileAccountHash(''), null);
});

test('a newly approved official asset is applied again', async t => {
  const files = fixture(t);
  let calls = 0;
  const options = { ...files.options, updateProfilePicture: async () => { calls += 1; } };
  await applyWhatsAppProfilePicture(options);
  const replacement = Buffer.from('new-approved-official-logo');
  fs.writeFileSync(files.imagePath, replacement);
  files.queue(JID, hash(replacement));
  assert.equal((await applyWhatsAppProfilePicture({
    ...options, expectedImageHash: hash(replacement),
  })).status, 'applied');
  assert.equal(calls, 2);
});

test('a different override is blocked even when it was applied by old code', async t => {
  const files = fixture(t);
  const oldImage = Buffer.from('legacy-other-brand');
  fs.writeFileSync(files.imagePath, oldImage);
  fs.writeFileSync(files.statePath, JSON.stringify({
    version: 1, accounts: {
      [whatsappProfileAccountHash(JID)]: { status: 'applied', imageHash: hash(oldImage), appliedAt: 500 },
    },
  }));
  let calls = 0;
  assert.deepEqual(await applyWhatsAppProfilePicture({
    ...files.options, updateProfilePicture: async () => { calls += 1; },
  }), { status: 'failed', errorCode: 'unexpected_image' });
  assert.equal(calls, 0);
  assert.equal(files.read().imageHash, hash(oldImage));
  assert.equal(files.read().expectedImageHash, files.options.expectedImageHash);
  assert.equal(files.read().verification, undefined);
});

test('an override containing the exact official bytes is accepted', async t => {
  const files = fixture(t);
  const copyPath = path.join(files.directory, 'override.png');
  fs.copyFileSync(files.imagePath, copyPath);
  assert.equal((await applyWhatsAppProfilePicture({
    ...files.options, imagePath: copyPath,
  })).status, 'applied');
});

test('missing configured or official image cannot call WhatsApp', async t => {
  const files = fixture(t);
  let calls = 0;
  const options = { ...files.options, updateProfilePicture: async () => { calls += 1; } };
  assert.deepEqual(await applyWhatsAppProfilePicture({
    ...options, expectedImageHash: undefined,
  }), { status: 'missing', errorCode: 'official_image_missing' });
  assert.deepEqual(await applyWhatsAppProfilePicture({
    ...options, expectedImageHash: null,
  }), { status: 'missing', errorCode: 'official_image_missing' });
  fs.rmSync(files.imagePath);
  assert.deepEqual(await applyWhatsAppProfilePicture(options), { status: 'missing', errorCode: 'image_missing' });
  assert.equal(calls, 0);
  assert.equal(files.read().status, 'missing');
});

test('429 cooldown lasts a day and survives a changed or missing asset', async t => {
  const files = fixture(t);
  let calls = 0;
  let clock = 1_000;
  const options = {
    ...files.options,
    now: () => clock,
    updateProfilePicture: async () => {
      calls += 1;
      throw Object.assign(new Error('private token=secret'), { data: 429, output: { statusCode: 500 } });
    },
  };
  assert.deepEqual(await applyWhatsAppProfilePicture(options), { status: 'failed', errorCode: 'rate_limited' });
  assert.equal(files.read().retryAfter, 1_000 + 86_400_000);
  fs.rmSync(files.imagePath);
  assert.deepEqual(await applyWhatsAppProfilePicture(options), { status: 'cooldown', errorCode: 'rate_limited' });
  fs.writeFileSync(files.imagePath, Buffer.from('changed'));
  const changedOptions = { ...options, expectedImageHash: hash(Buffer.from('changed')) };
  assert.equal((await applyWhatsAppProfilePicture(changedOptions)).status, 'cooldown');
  assert.equal(calls, 1);
  clock += 86_400_001;
  files.queue(JID, hash(Buffer.from('changed')), clock);
  assert.equal((await applyWhatsAppProfilePicture(changedOptions)).status, 'failed');
  assert.equal(calls, 2);
  assert.equal(fs.readFileSync(files.statePath, 'utf8').includes('secret'), false);
});

test('explicit rejection is sanitized and has the existing one-hour cooldown', async t => {
  const files = fixture(t);
  const logs = [];
  let calls = 0;
  const options = {
    ...files.options,
    logger: { warn: value => logs.push(value) },
    updateProfilePicture: async () => {
      calls += 1;
      throw Object.assign(new Error('sensitive token 573001234567'), { data: 403, output: { statusCode: 500 } });
    },
  };
  assert.deepEqual(await applyWhatsAppProfilePicture(options), { status: 'failed', errorCode: 'rejected' });
  assert.equal(files.read().retryAfter, 3_601_000);
  assert.equal((await applyWhatsAppProfilePicture(options)).status, 'cooldown');
  assert.equal(calls, 1);
  assert.equal(JSON.stringify(logs).includes('sensitive'), false);
  assert.equal(fs.readFileSync(files.statePath, 'utf8').includes('573001234567'), false);
});

test('transport error has uncertain outcome and does not retry after an image change', async t => {
  const files = fixture(t);
  let calls = 0;
  const options = {
    ...files.options,
    updateProfilePicture: async () => { calls += 1; throw new Error('connection lost after send'); },
  };
  assert.deepEqual(await applyWhatsAppProfilePicture(options), { status: 'uncertain', errorCode: 'update_unconfirmed' });
  fs.writeFileSync(files.imagePath, Buffer.from('changed'));
  files.queue(JID, hash(Buffer.from('changed')), 99_000_000);
  assert.deepEqual(await applyWhatsAppProfilePicture({
    ...options, expectedImageHash: hash(Buffer.from('changed')), now: () => 99_000_000,
  }), { status: 'uncertain', errorCode: 'update_unconfirmed' });
  assert.equal(calls, 1);
  assert.equal(files.read().retryAfter, undefined);
});

test('Baileys disconnection codes do not imply a confirmed rejection or permit retries', async t => {
  const files = fixture(t);
  for (const statusCode of [401, 403, 408, 411, 428, 440, 500, 503, 515]) {
    let calls = 0;
    const options = {
      ...files.options,
      jid: `${statusCode}@s.whatsapp.net`,
      updateProfilePicture: async () => {
        calls += 1;
        throw Object.assign(new Error('socket closed'), { output: { statusCode } });
      },
    };
    files.queue(options.jid);
    assert.deepEqual(await applyWhatsAppProfilePicture(options), {
      status: 'uncertain', errorCode: 'update_unconfirmed',
    });
    assert.equal(files.read(options.jid).retryAfter, undefined);
    assert.equal((await applyWhatsAppProfilePicture(options)).status, 'uncertain');
    assert.equal(calls, 1);
  }
});

test('timeout remains uncertain after late provider ACK and cannot be retried', async t => {
  const files = fixture(t);
  const provider = deferred();
  let calls = 0;
  const options = {
    ...files.options, timeoutMs: 10,
    updateProfilePicture: () => { calls += 1; return provider.promise; },
  };
  assert.deepEqual(await applyWhatsAppProfilePicture(options), { status: 'uncertain', errorCode: 'update_timeout' });
  provider.resolve();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(files.read().status, 'uncertain');
  assert.equal(files.read().verification, undefined);
  assert.equal((await applyWhatsAppProfilePicture(options)).status, 'uncertain');
  assert.equal(calls, 1);
});

test('pending from an interrupted process blocks a new image and becomes uncertain', async t => {
  const files = fixture(t);
  fs.writeFileSync(files.statePath, JSON.stringify({
    version: 1, accounts: {
      [whatsappProfileAccountHash(JID)]: { status: 'pending', imageHash: hash('previous-image'), attemptedAt: 100 },
    },
  }));
  let calls = 0;
  assert.deepEqual(await applyWhatsAppProfilePicture({
    ...files.options, updateProfilePicture: async () => { calls += 1; },
  }), { status: 'uncertain', errorCode: 'interrupted' });
  assert.equal(files.read().status, 'uncertain');
  assert.equal(files.read().imageHash, hash('previous-image'));
  assert.equal(calls, 0);
});

test('concurrent accounts retain both results and duplicate same-account calls are suppressed', async t => {
  const files = fixture(t);
  const first = deferred();
  const second = deferred();
  const secondJid = '573009876543@s.whatsapp.net';
  let calls = 0;
  const updateProfilePicture = jid => {
    calls += 1;
    return jid === JID ? first.promise : second.promise;
  };
  const options = { ...files.options, updateProfilePicture };
  const firstResult = applyWhatsAppProfilePicture(options);
  // First RPC has already been scheduled; another account receives its own request.
  await new Promise(resolve => setImmediate(resolve));
  files.queue(secondJid);
  const secondResult = applyWhatsAppProfilePicture({ ...options, jid: secondJid });
  assert.deepEqual(await applyWhatsAppProfilePicture(options), { status: 'pending' });
  second.resolve();
  assert.equal((await secondResult).status, 'applied');
  first.resolve();
  assert.equal((await firstResult).status, 'applied');
  assert.equal(files.read().status, 'applied');
  assert.equal(files.read(secondJid).status, 'applied');
  assert.equal(calls, 2);
});

test('unreadable state prevents provider calls instead of discarding prior evidence', async t => {
  const files = fixture(t);
  fs.writeFileSync(files.statePath, 'corrupt state');
  let calls = 0;
  assert.deepEqual(await applyWhatsAppProfilePicture({
    ...files.options, updateProfilePicture: async () => { calls += 1; },
  }), { status: 'uncertain', errorCode: 'state_unavailable' });
  assert.equal(calls, 0);
  assert.equal(fs.readFileSync(files.statePath, 'utf8'), 'corrupt state');
});

test('failure to save provider ACK preserves pending evidence without retry', async t => {
  const files = fixture(t);
  const originalRename = fs.renameSync;
  let writes = 0;
  const renameMock = t.mock.method(fs, 'renameSync', (source, target) => {
    if (target === files.statePath && ++writes === 2) throw Object.assign(new Error('disk unavailable'), { code: 'EPERM' });
    return originalRename(source, target);
  });
  let calls = 0;
  const options = { ...files.options, updateProfilePicture: async () => { calls += 1; } };
  assert.deepEqual(await applyWhatsAppProfilePicture(options), { status: 'uncertain', errorCode: 'state_unavailable' });
  assert.equal(files.read().status, 'pending');
  renameMock.mock.restore();
  assert.deepEqual(await applyWhatsAppProfilePicture(options), { status: 'uncertain', errorCode: 'interrupted' });
  assert.equal(calls, 1);
});
