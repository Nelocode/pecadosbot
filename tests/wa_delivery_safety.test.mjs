import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  createWhatsAppDeliverySafety,
  readWhatsAppSafetyConfig,
  recordWhatsAppProviderSignal,
  WhatsAppDeliveryBlockedError,
} from '../wa_delivery_safety.mjs';
import { createWhatsAppSafetyHealth } from '../wa_safety_health.mjs';

function createHarness(t, overrides = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-delivery-safety-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const healthFile = path.join(directory, 'health.json');
  let currentTime = 1_700_000_000_000;
  const delays = [];
  const health = createWhatsAppSafetyHealth({
    filePath: healthFile,
    controlFilePath: path.join(directory, 'control.json'),
    now: () => currentTime,
    logger: { warn() {} },
  });
  const config = {
    ...readWhatsAppSafetyConfig({}),
    readDelayMinMs: 0,
    readDelayMaxMs: 0,
    textDelayMinMs: 0,
    textDelayMaxMs: 0,
    textMsPerCharacter: 0,
    audioDelayMinMs: 0,
    audioDelayMaxMs: 0,
    jitterRatio: 0,
    minimumSendIntervalMs: 0,
    auxiliaryTimeoutMs: 50,
    sendTimeoutMs: 100,
    ...overrides,
  };
  const sleep = async (ms, signal) => {
    if (signal?.aborted) throw signal.reason;
    delays.push(ms);
    currentTime += ms;
  };
  return {
    health,
    healthFile,
    config,
    delays,
    sleep,
    now: () => currentTime,
    monotonicNow: () => currentTime,
    advance(ms) { currentTime += ms; },
    logger: { warn() {} },
  };
}

test('configuration is bounded and min/max pairs stay coherent', () => {
  const config = readWhatsAppSafetyConfig({
    WA_TEXT_DELAY_MIN_MS: '8000',
    WA_TEXT_DELAY_MAX_MS: '10',
    WA_MAX_PENDING_SENDS: '99999',
  });
  assert.equal(config.textDelayMaxMs, 8_000);
  assert.equal(config.maxPendingSends, 500);
});

test('text and voice delivery publish bounded composing and recording UX', async (t) => {
  const harness = createHarness(t, {
    textDelayMinMs: 100,
    textDelayMaxMs: 500,
    textMsPerCharacter: 10,
    audioDelayMinMs: 300,
    audioDelayMaxMs: 300,
  });
  const effects = [];
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    random: () => 0.5,
    sendPresenceUpdate: async (presence) => effects.push(presence),
    sendMessage: async (_jid, content) => effects.push(content.text ? 'text' : 'audio'),
  });
  await safety.send('contact-a', { text: 'hola' });
  await safety.send('contact-a', { audio: Buffer.alloc(4_800), ptt: true });
  assert.deepEqual(effects, ['composing', 'text', 'paused', 'recording', 'audio', 'paused']);
  assert.equal(harness.delays.reduce((total, value) => total + value, 0), 440);
});

for (const videoBytes of [256_000, 1_048_576]) {
  test(`default video-first pacing saves 4000ms for a ${videoBytes}-byte video`, async (t) => {
    const harness = createHarness(t, readWhatsAppSafetyConfig({}));
    const startedAt = harness.now();
    const effects = [];
    const record = kind => effects.push([kind, harness.now() - startedAt]);
    const safety = createWhatsAppDeliverySafety({
      ...harness,
      random: () => 0.5,
      readMessages: async () => record('read'),
      sendPresenceUpdate: async presence => record(presence),
      sendMessage: async (_jid, content) => record(
        content.video ? 'video' : content.text ? 'text' : 'audio',
      ),
    });

    await safety.markRead({ id: 'synthetic-event' });
    await safety.send('contact-a', { video: Buffer.alloc(videoBytes) });
    await safety.send('contact-a', { text: 'x'.repeat(50) });
    await safety.send('contact-a', { audio: Buffer.alloc(102_400), ptt: true });

    assert.deepEqual(effects, [
      ['read', 575],
      ['composing', 575],
      ['video', 575],
      ['paused', 575],
      ['composing', 1_075],
      ['text', 2_325],
      ['paused', 2_325],
      ['recording', 2_825],
      ['audio', 6_825],
      ['paused', 6_825],
    ]);
    // Before this change the video inherited a 4000ms audio pacing delay.
    assert.equal(harness.now() - startedAt, 10_825 - 4_000);
    assert.equal(harness.health.recentEventTimes('outgoing', 60_000).length, 3);
  });
}

test('pending video holds text behind the global gate while publishing video composing', async (t) => {
  const harness = createHarness(t, readWhatsAppSafetyConfig({}));
  const startedAt = harness.now();
  const effects = [];
  const record = kind => effects.push([kind, harness.now() - startedAt]);
  let releaseVideo;
  let markVideoStarted;
  const videoGate = new Promise(resolve => { releaseVideo = resolve; });
  const videoStarted = new Promise(resolve => { markVideoStarted = resolve; });
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    random: () => 0.5,
    sendPresenceUpdate: async presence => record(presence),
    sendMessage: async (_jid, content) => {
      if (content.video) {
        record('video:start');
        markVideoStarted();
        await videoGate;
        record('video:finish');
      } else {
        record(content.text ? 'text' : 'audio');
      }
    },
  });
  const pending = Promise.all([
    safety.send('contact-a', { video: Buffer.alloc(1_048_576) }),
    safety.send('contact-b', { text: 'x'.repeat(50) }),
    safety.send('contact-b', { audio: Buffer.alloc(102_400), ptt: true }),
  ]);
  t.after(async () => { releaseVideo(); await pending; });

  await videoStarted;
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(effects, [['composing', 0], ['video:start', 0]]);
  assert.equal(harness.health.recentEventTimes('outgoing', 60_000).length, 0);
  releaseVideo();
  await pending;
  assert.deepEqual(effects, [
    ['composing', 0],
    ['video:start', 0],
    ['video:finish', 0],
    ['paused', 0],
    ['composing', 500],
    ['text', 1_750],
    ['paused', 1_750],
    ['recording', 2_250],
    ['audio', 6_250],
    ['paused', 6_250],
  ]);
});

test('failed video releases the global gate and cleans presence without retry', async (t) => {
  const harness = createHarness(t, { maxPendingSends: 2 });
  const effects = [];
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendPresenceUpdate: async presence => effects.push(presence),
    sendMessage: async (_jid, content) => {
      effects.push(content.video ? 'video' : 'text');
      if (content.video) throw new Error('synthetic send failure');
    },
  });
  const outcomes = await Promise.allSettled([
    safety.send('contact-a', { video: Buffer.alloc(256_000) }),
    safety.send('contact-b', { text: 'next' }),
  ]);

  assert.equal(outcomes[0].status, 'rejected');
  assert.match(outcomes[0].reason.message, /synthetic send failure/);
  assert.equal(outcomes[1].status, 'fulfilled');
  assert.deepEqual(effects, ['composing', 'video', 'paused', 'composing', 'text', 'paused']);
  assert.equal(harness.health.recentEventTimes('delivery_failure', 60_000).length, 1);
  assert.equal(harness.health.recentEventTimes('outgoing', 60_000).length, 1);
});

test('slow video composing never delays sending and is cleared again after late settlement', async t => {
  const harness = createHarness(t);
  const effects = [];
  let releasePresence;
  const presenceGate = new Promise(resolve => { releasePresence = resolve; });
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendPresenceUpdate: async presence => {
      effects.push(presence);
      if (presence === 'composing') {
        await presenceGate;
        effects.push('late_composing');
      }
    },
    sendMessage: async () => effects.push('video'),
  });
  await safety.send('contact-a', { video: Buffer.alloc(256_000) });
  assert.deepEqual(effects, ['composing', 'video', 'paused']);
  assert.deepEqual(harness.delays, []);
  releasePresence();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(effects, ['composing', 'video', 'paused', 'late_composing', 'paused']);
});

test('video composing renews at most six times within thirty seconds and then stops', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const harness = createHarness(t, { sendTimeoutMs: 60_000 });
  const effects = [];
  let releaseVideo;
  const gate = new Promise(resolve => { releaseVideo = resolve; });
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendPresenceUpdate: async presence => effects.push(presence),
    sendMessage: async () => { effects.push('video'); await gate; },
  });
  const pending = safety.send('contact-a', { video: Buffer.alloc(256_000) });
  await new Promise(resolve => setImmediate(resolve));
  for (let index = 0; index < 6; index += 1) {
    t.mock.timers.tick(5_000);
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.equal(effects.filter(value => value === 'composing').length, 6);
  assert.equal(effects.filter(value => value === 'paused').length, 1);
  releaseVideo();
  await pending;
  const finished = [...effects];
  t.mock.timers.tick(60_000);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(effects, finished);
});

test('late video composing is cleared after a subsequent text phase has already finished', async t => {
  const harness = createHarness(t);
  const effects = [];
  let releasePresence;
  let firstComposing = true;
  const gate = new Promise(resolve => { releasePresence = resolve; });
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendPresenceUpdate: async presence => {
      effects.push(presence);
      if (presence === 'composing' && firstComposing) {
        firstComposing = false;
        await gate;
        effects.push('late_video_composing');
      }
    },
    sendMessage: async (_jid, content) => effects.push(content.video ? 'video' : 'text'),
  });
  await safety.send('contact-a', { video: Buffer.alloc(1) });
  await safety.send('contact-a', { text: 'next' });
  releasePresence();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(effects.slice(-4), ['text', 'paused', 'late_video_composing', 'paused']);
});

test('socket cancellation stops video composing and late transport cannot restart it', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const harness = createHarness(t, { sendTimeoutMs: 10 });
  const effects = [];
  let releaseVideo;
  const gate = new Promise(resolve => { releaseVideo = resolve; });
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendPresenceUpdate: async presence => effects.push(presence),
    sendMessage: async () => { effects.push('video'); await gate; },
  });
  const outcome = safety.send('contact-a', { video: Buffer.alloc(256_000) })
    .then(() => null, error => error);
  await new Promise(resolve => setImmediate(resolve));
  safety.cancelAll('connection_closed');
  t.mock.timers.tick(11);
  assert.match((await outcome).message, /send_timeout/);
  assert.deepEqual(effects, ['composing', 'video', 'paused']);
  releaseVideo();
  t.mock.timers.tick(60_000);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(effects, ['composing', 'video', 'paused']);
});

for (const statusCode of [403, 429]) {
  test(`concurrent video presence ${statusCode} still blocks subsequent delivery`, async t => {
    const harness = createHarness(t);
    const effects = [];
    const safety = createWhatsAppDeliverySafety({
      ...harness,
      sendPresenceUpdate: async presence => {
        effects.push(presence);
        throw Object.assign(new Error('Synthetic provider restriction'), { statusCode });
      },
      sendMessage: async () => effects.push('video'),
    });
    await safety.send('contact-a', { video: Buffer.alloc(256_000) });
    await new Promise(resolve => setImmediate(resolve));
    const before = [...effects];
    await assert.rejects(safety.send('contact-a', { text: 'held' }), WhatsAppDeliveryBlockedError);
    assert.equal(safety.canDeliver(), false);
    assert.deepEqual(effects, before);
    assert.equal(effects.filter(value => value === 'composing').length, 1);
  });
}

test('timing separates queue, pacing and SDK elapsed time without including payload identifiers', async t => {
  const harness = createHarness(t, { presenceEnabled: false });
  const samples = [];
  let releaseVideo;
  const gate = new Promise(resolve => { releaseVideo = resolve; });
  const context = { trace_id: '123e4567-e89b-42d3-a456-426614174000', response: 'step1' };
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    timing: { record: value => samples.push(value) },
    sendMessage: async (_jid, content) => {
      if (content.video) { await gate; harness.advance(25); }
      else harness.advance(5);
    },
  });
  const pending = Promise.all([
    safety.send('private-contact', { video: Buffer.from('private-video') }, context),
    safety.send('private-contact', { text: 'private-text' }, context),
  ]);
  await new Promise(resolve => setImmediate(resolve));
  releaseVideo();
  await pending;
  assert.deepEqual(samples.map(({ queue_ms, pacing_ms, sdk_ms, total_ms, outcome }) =>
    [queue_ms, pacing_ms, sdk_ms, total_ms, outcome]), [
    [0, 0, 25, 25, 'completed'],
    [25, 0, 5, 30, 'completed'],
  ]);
  assert.ok(samples.every(value => value.trace_id === context.trace_id && value.response === 'step1'));
  assert.equal(JSON.stringify(samples).includes('private-'), false);
});

test('SDK rejection timing remains uncertain and a broken timing recorder cannot alter delivery', async t => {
  const harness = createHarness(t, { presenceEnabled: false });
  const samples = [];
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    timing: { record: value => { samples.push(value); throw new Error('Synthetic telemetry failure'); } },
    sendMessage: async () => { throw new Error('Synthetic unknown send'); },
  });
  await assert.rejects(safety.send('contact-a', { video: Buffer.alloc(1) }), /Synthetic unknown send/);
  assert.equal(samples.length, 1);
  assert.equal(samples[0].outcome, 'uncertain');
});

test('bounded pacing remains when presence publication is disabled', async (t) => {
  const harness = createHarness(t, {
    presenceEnabled: false,
    textDelayMinMs: 300,
    textDelayMaxMs: 300,
  });
  const safety = createWhatsAppDeliverySafety({ ...harness, sendMessage: async () => {} });
  await safety.send('contact-a', { text: 'hola' });
  assert.equal(harness.delays.reduce((total, value) => total + value, 0), 300);
});

test('403 pauses future delivery and persists no private payload', async (t) => {
  const harness = createHarness(t);
  const restricted = new Error('private provider details');
  restricted.output = { statusCode: 403 };
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendMessage: async () => { throw restricted; },
  });
  await assert.rejects(safety.send('573001234567@s.whatsapp.net', {
    video: Buffer.alloc(256_000), caption: 'secret text',
  }));
  assert.equal(harness.health.snapshot().operator_paused, true);
  await assert.rejects(
    safety.send('contact-b', { text: 'next' }),
    error => error instanceof WhatsAppDeliveryBlockedError && error.code === 'operator_paused',
  );
  const persisted = fs.readFileSync(harness.healthFile, 'utf8');
  for (const value of ['573001234567', 'secret text', 'private provider details']) {
    assert.equal(persisted.includes(value), false);
  }
});

test('429 in read receipt activates backoff before content is sent', async (t) => {
  const harness = createHarness(t);
  const limited = new Error('limited');
  limited.output = { statusCode: 429 };
  let sent = false;
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    readMessages: async () => { throw limited; },
    sendMessage: async () => { sent = true; },
  });
  assert.equal((await safety.markRead({ id: 'one' })).status, 'skipped');
  await assert.rejects(safety.send('contact-a', { video: Buffer.alloc(256_000) }));
  assert.equal(sent, false);
  assert.ok(harness.health.snapshot().backoff_until);
});

test('connection 403 and 429 use the common provider signal mapping', (t) => {
  const forbidden = createHarness(t);
  assert.equal(recordWhatsAppProviderSignal({
    statusCode: 403, health: forbidden.health, config: forbidden.config, now: forbidden.now,
  }), 'forbidden');
  assert.equal(forbidden.health.snapshot().operator_paused, true);

  const limited = createHarness(t);
  assert.equal(recordWhatsAppProviderSignal({
    statusCode: 429, health: limited.health, config: limited.config, now: limited.now,
  }), 'rate_limited');
  assert.ok(limited.health.snapshot().backoff_until);
});

test('global send reservation is atomic and pending queue is bounded', async (t) => {
  const harness = createHarness(t, {
    presenceEnabled: false,
    maxSendsPerMinute: 1,
    maxPendingSends: 2,
  });
  const effects = [];
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendMessage: async jid => effects.push(jid),
  });
  const results = await Promise.allSettled([
    safety.send('contact-a', { video: Buffer.alloc(256_000) }),
    safety.send('contact-b', { video: Buffer.alloc(256_000) }),
    safety.send('contact-c', { text: 'three' }),
  ]);
  assert.deepEqual(effects, ['contact-a']);
  assert.equal(results[0].status, 'fulfilled');
  assert.equal(results[1].reason.code, 'local_rate_limit');
  assert.equal(results[2].reason.code, 'queue_full');
});

test('a timed-out video keeps the global gate until its final outcome is known', async (t) => {
  const harness = createHarness(t, {
    presenceEnabled: false,
    sendTimeoutMs: 15,
    circuitOpenMs: 20,
  });
  const effects = [];
  let releaseTransport;
  let markStarted;
  const transportGate = new Promise(resolve => { releaseTransport = resolve; });
  const started = new Promise(resolve => { markStarted = resolve; });
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendMessage: async jid => {
      effects.push(`start:${jid}`);
      markStarted();
      await transportGate;
      effects.push(`finish:${jid}`);
    },
  });

  const first = safety.send('contact-a', { video: Buffer.alloc(256_000) });
  await started;
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.ok(harness.health.snapshot().circuit_open_until);

  harness.advance(21);
  let secondSettled = false;
  const second = safety.send('contact-b', { text: 'two' }).then(() => {
    secondSettled = true;
  });
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(secondSettled, false);
  assert.deepEqual(effects, ['start:contact-a']);

  releaseTransport();
  await assert.rejects(first, /send_timeout/);
  await second;
  assert.deepEqual(effects, [
    'start:contact-a',
    'finish:contact-a',
    'start:contact-b',
    'finish:contact-b',
  ]);
});

test('operator pause cannot consume the later socket-close release of an unknown transport', async (t) => {
  const harness = createHarness(t, {
    presenceEnabled: false,
    sendTimeoutMs: 15,
  });
  let markStarted;
  const started = new Promise(resolve => { markStarted = resolve; });
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendMessage: async () => {
      markStarted();
      await new Promise(() => {});
    },
  });
  let settled = false;
  const outcome = safety.send('contact-a', { video: Buffer.alloc(256_000) }).then(
    value => ({ value }),
    error => ({ error }),
  ).finally(() => { settled = true; });

  await started;
  await new Promise(resolve => setTimeout(resolve, 30));
  harness.health.setOperatorPaused(true);
  safety.refreshControls();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(settled, false);

  safety.cancelAll('connection_closed');
  const result = await outcome;
  assert.match(result.error.message, /send_timeout/);
});
