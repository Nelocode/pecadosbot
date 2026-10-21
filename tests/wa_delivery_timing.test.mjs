import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { createWhatsAppDeliveryTiming } from '../wa_delivery_timing.mjs';

const TRACE_ID = 'a670ac52-10ef-4e10-9ba9-c1b991b8fb72';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function event(overrides = {}) {
  return {
    trace_id: TRACE_ID,
    response: 'step1',
    component: 'video',
    outcome: 'completed',
    queue_ms: 10,
    pacing_ms: 20,
    sdk_ms: 30,
    total_ms: 60,
    ...overrides,
  };
}

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-delivery-timing-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { directory, filePath: path.join(directory, 'timing.json') };
}

test('records only the schema fields and exposes an independent snapshot', () => {
  const timing = createWhatsAppDeliveryTiming({ now: () => 1234 });
  const input = event();
  timing.record(input);
  input.total_ms = 999;
  const expected = {
    schema_version: 1,
    updated_at: 1234,
    samples: [{
      trace_id: TRACE_ID,
      response: 'step1',
      observed_at: 1234,
      elapsed_ms: 60,
      outcome: 'completed',
      components: { video: { outcome: 'completed', queue_ms: 10, pacing_ms: 20, sdk_ms: 30, total_ms: 60 } },
    }],
  };
  assert.deepEqual(timing.snapshot(), expected);
  const copy = timing.snapshot();
  copy.samples[0].components.video.total_ms = 999;
  copy.samples[0].response = 'call';
  copy.samples.push({});
  assert.deepEqual(timing.snapshot(), expected);
});

test('keeps only the latest 32 records without merging matching trace IDs', () => {
  let clock = 100;
  const timing = createWhatsAppDeliveryTiming({ now: () => clock });
  for (let index = 0; index < 40; index += 1) {
    clock = 100 + index;
    timing.record(event({ total_ms: index }));
  }
  const snapshot = timing.snapshot();
  assert.equal(snapshot.samples.length, 32);
  assert.deepEqual(snapshot.samples.map(sample => sample.elapsed_ms), Array.from({ length: 32 }, (_, index) => index + 8));
  assert.equal(snapshot.samples[0].observed_at, 108);
  assert.equal(snapshot.updated_at, 139);
});

test('rounds finite bounded durations and stores invalid values as null', () => {
  const timing = createWhatsAppDeliveryTiming({ now: () => 1 });
  for (const value of [0, -0, 0.49, 0.5, 12.75, 3_599_999.9, 3_600_000,
    -1, 3_600_000.1, NaN, Infinity, -Infinity, '50', true, null, undefined, {}, Symbol('duration'), 1n]) {
    timing.record(event({ queue_ms: value, pacing_ms: value, sdk_ms: value, total_ms: value }));
  }
  const expected = [0, 0, 0, 1, 13, 3_600_000, 3_600_000, ...Array(12).fill(null)];
  const samples = timing.snapshot().samples;
  assert.deepEqual(samples.map(sample => sample.elapsed_ms), expected);
  for (const [index, sample] of samples.entries()) {
    for (const key of ['queue_ms', 'pacing_ms', 'sdk_ms', 'total_ms']) {
      assert.equal(sample.components.video[key], expected[index]);
    }
  }
});

test('accepts allowed values and normalizes untrusted labels without retaining raw content', () => {
  const timing = createWhatsAppDeliveryTiming({ now: () => 1 });
  const outcomes = ['completed', 'failed', 'uncertain', 'blocked', 'cancelled'];
  const components = ['text', 'audio', 'video', 'other'];
  for (const [index, outcome] of outcomes.entries()) {
    timing.record(event({ outcome, component: components[index % components.length], response: [null, 'step1', 'step2', 'call', null][index] }));
  }
  for (const sample of timing.snapshot().samples) {
    assert.ok(outcomes.includes(sample.outcome));
    assert.ok(components.includes(Object.keys(sample.components)[0]));
  }
  const raw = 'private-contact@example.invalid';
  timing.record(event({ trace_id: raw, response: raw, component: '__proto__', outcome: raw,
    jid: raw, path: raw, error: new Error(raw), text: raw, components: { text: raw }, metadata: { raw } }));
  const sample = timing.snapshot().samples.at(-1);
  assert.match(sample.trace_id, UUID);
  assert.equal(sample.response, null);
  assert.equal(sample.outcome, 'uncertain');
  assert.deepEqual(Object.keys(sample.components), ['other']);
  assert.equal(JSON.stringify(timing.snapshot()).includes(raw), false);
});

test('keeps a lowercase UUIDv4 and replaces all other identifiers with fresh UUIDv4s', () => {
  const timing = createWhatsAppDeliveryTiming({ now: () => 1 });
  const valid = randomUUID();
  timing.record(event({ trace_id: valid }));
  const invalid = [undefined, null, '', 123, '573001234567@s.whatsapp.net', `${TRACE_ID}-extra`,
    TRACE_ID.toUpperCase(), TRACE_ID.replace('-9ba9-', '-cba9-'),
    ...[0, 1, 2, 3, 5, 6, 7, 8].map(version => TRACE_ID.replace('-4e10-', `-${version}e10-`))];
  for (const trace_id of invalid) {
    timing.record(event({ trace_id }));
  }
  const samples = timing.snapshot().samples;
  assert.equal(samples[0].trace_id, valid);
  for (const sample of samples.slice(1)) assert.match(sample.trace_id, UUID);
  assert.equal(samples.slice(1).some(sample => invalid.includes(sample.trace_id)), false);
  assert.equal(new Set(samples.map(sample => sample.trace_id)).size, samples.length);
});

test('malformed input and accessors cannot throw or leak caller data', () => {
  const timing = createWhatsAppDeliveryTiming({ now: () => 1 });
  const accessor = Object.create({ response: 'step1' });
  let accessorCalls = 0;
  Object.defineProperty(accessor, 'total_ms', { get() { accessorCalls += 1; throw new Error('private'); } });
  const proxy = new Proxy({}, { getOwnPropertyDescriptor() { throw new Error('private'); } });
  for (const input of [undefined, null, true, 'private', 1, 1n, Symbol('private'), [], accessor, proxy]) {
    assert.doesNotThrow(() => timing.record(input));
  }
  assert.equal(accessorCalls, 0);
  assert.equal(timing.snapshot().samples.length, 10);
  for (const sample of timing.snapshot().samples) {
    assert.equal(sample.response, null);
    assert.equal(sample.elapsed_ms, null);
    assert.equal(sample.outcome, 'uncertain');
    assert.deepEqual(Object.keys(sample.components), ['other']);
  }
  assert.equal(JSON.stringify(timing.snapshot()).includes('private'), false);
});

test('invalid or throwing clock falls back to a finite epoch timestamp', () => {
  for (const now of [() => NaN, () => -1, () => Infinity, () => 'private', () => { throw new Error('private'); }, null]) {
    const before = Date.now();
    const timing = createWhatsAppDeliveryTiming({ now });
    assert.doesNotThrow(() => timing.record(event()));
    const snapshot = timing.snapshot();
    assert.ok(Number.isSafeInteger(snapshot.updated_at));
    assert.ok(snapshot.updated_at >= before && snapshot.updated_at <= Date.now());
    assert.equal(snapshot.samples[0].observed_at, snapshot.updated_at);
  }
});

test('persistence atomically replaces the old file and never loads its history', t => {
  const { directory, filePath } = fixture(t);
  const oldContents = '{"samples":[{"private":"old history"}]}';
  fs.writeFileSync(filePath, oldContents);
  const timing = createWhatsAppDeliveryTiming({ filePath, now: () => 1234 });
  assert.deepEqual(timing.snapshot().samples, []);
  const originalRename = fs.renameSync;
  const observations = [];
  t.mock.method(fs, 'renameSync', (source, destination) => {
    observations.push({
      sameDirectory: path.dirname(source) === path.dirname(destination),
      differentFile: source !== destination,
      oldContents: fs.readFileSync(destination, 'utf8'),
      next: JSON.parse(fs.readFileSync(source, 'utf8')),
    });
    return originalRename(source, destination);
  });
  timing.record(event());
  assert.equal(observations.length, 1);
  assert.equal(observations[0].sameDirectory, true);
  assert.equal(observations[0].differentFile, true);
  assert.equal(observations[0].oldContents, oldContents);
  assert.deepEqual(observations[0].next, timing.snapshot());
  assert.deepEqual(JSON.parse(fs.readFileSync(filePath, 'utf8')), timing.snapshot());
  assert.deepEqual(fs.readdirSync(directory), ['timing.json']);
});

test('a failed rename preserves the previous report, cleans temporary files and logs no PII', t => {
  const { directory, filePath } = fixture(t);
  fs.writeFileSync(filePath, 'previous report');
  const logs = [];
  const privateValue = 'private-contact@example.invalid';
  t.mock.method(fs, 'renameSync', () => { throw new Error(privateValue); });
  const timing = createWhatsAppDeliveryTiming({ filePath, now: () => 1, logger: { warn: (...args) => logs.push(args) } });
  assert.doesNotThrow(() => timing.record(event({ jid: privateValue, error: privateValue })));
  assert.equal(timing.snapshot().samples.length, 1);
  assert.equal(fs.readFileSync(filePath, 'utf8'), 'previous report');
  assert.deepEqual(fs.readdirSync(directory), ['timing.json']);
  assert.deepEqual(logs, [['WhatsApp delivery timing persistence failed']]);
  assert.equal(JSON.stringify(logs).includes(privateValue), false);
});

test('failed paths and throwing loggers do not interrupt recording', t => {
  const { directory } = fixture(t);
  const notDirectory = path.join(directory, 'file');
  fs.writeFileSync(notDirectory, 'not a directory');
  const logger = {};
  Object.defineProperty(logger, 'warn', { get() { throw new Error('private logger failure'); } });
  const timing = createWhatsAppDeliveryTiming({ filePath: path.join(notDirectory, 'timing.json'), now: () => 1, logger });
  assert.doesNotThrow(() => timing.record(event()));
  assert.equal(timing.snapshot().samples.length, 1);
  assert.equal(fs.readFileSync(notDirectory, 'utf8'), 'not a directory');
});

test('asynchronous logger failures are handled without an unhandled rejection', async t => {
  const { directory } = fixture(t);
  const notDirectory = path.join(directory, 'file');
  fs.writeFileSync(notDirectory, 'not a directory');
  let loggerCalls = 0;
  const timing = createWhatsAppDeliveryTiming({
    filePath: path.join(notDirectory, 'timing.json'),
    now: () => 1,
    logger: { warn: async () => { loggerCalls += 1; throw new Error('private logger failure'); } },
  });
  assert.doesNotThrow(() => timing.record(event()));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(loggerCalls, 1);
  assert.equal(timing.snapshot().samples.length, 1);
});

test('persistence is optional and an omitted destination performs no filesystem writes', t => {
  const writes = [];
  t.mock.method(fs, 'openSync', (...args) => { writes.push(args); throw new Error('unexpected write'); });
  for (const filePath of [undefined, null, '', {}, 123]) {
    const timing = createWhatsAppDeliveryTiming({ filePath, now: () => 1 });
    assert.doesNotThrow(() => timing.record(event()));
    assert.equal(timing.snapshot().samples.length, 1);
  }
  assert.deepEqual(writes, []);
});
