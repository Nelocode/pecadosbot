import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { createWhatsAppCallHealth } from '../wa_call_health.mjs';

function idFactory() {
  let value = 0;
  return () => `00000000-0000-4000-8000-${String(++value).padStart(12, '0')}`;
}

test('persiste solo un snapshot anonimizado y acotado', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-health-'));
  const filePath = path.join(directory, 'health.json');
  const health = createWhatsAppCallHealth({
    filePath,
    idFactory: idFactory(),
    logger: { warn() {} },
  });

  health.record({ type: 'connection', state: 'open', phone: '573001234567' });
  health.record({ type: 'listener_registered', jid: '573001234567@s.whatsapp.net' });
  health.record({ type: 'raw_listener', state: 'registered' });
  health.record({ type: 'raw_call', id: 'call-1' });
  health.record({ type: 'batch', payload: 'array', size: 'one', from: '573001234567@s.whatsapp.net' });
  health.record({
    type: 'outcome',
    event: 'offer',
    outcome: 'handled',
    reason: 'completed',
    offline: false,
    video: false,
    group: false,
    reject: 'sent',
    text: 'sent',
    audio: 'sent',
    target: 'caller_pn',
    targetKind: 'pn',
    callId: 'call-1',
    replyJid: '573001234567@s.whatsapp.net',
    message: 'No podemos responder ahora',
    audioPath: 'call.mp3',
  });

  const persisted = fs.readFileSync(filePath, 'utf8');
  const parsed = JSON.parse(persisted);
  assert.equal(parsed.connection, 'open');
  assert.equal(parsed.listener, 'registered');
  assert.equal(parsed.raw_listener, 'registered');
  assert.equal(parsed.pipeline.event, 'offer');
  assert.equal(parsed.last_event, 'offer');
  assert.equal(parsed.pipeline.outcome, 'handled');
  assert.equal(parsed.pipeline.target, 'caller_pn');
  assert.equal(parsed.pipeline.target_kind, 'pn');
  assert.equal(parsed.pipeline.video_delivery, 'not_applicable');
  assert.equal(parsed.pipeline.delivery, 'not_applicable');
  assert.doesNotMatch(
    persisted,
    /573001234567|@s\.whatsapp\.net|call-1|No podemos|call\.mp3|phone|jid|callId|message|audioPath/,
  );
  assert.ok(persisted.length < 2_000);

  fs.rmSync(directory, { recursive: true, force: true });
});

test('conserva fallos parciales e incertidumbre sin confundir la videollamada entrante', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-health-'));
  try {
    const filePath = path.join(directory, 'health.json');
    const health = createWhatsAppCallHealth({ filePath, idFactory: idFactory(), logger: { warn() {} } });
    for (const [videoDelivery, delivery, reason] of [
      ['missing', 'partial', 'delivery_incomplete'],
      ['failed', 'failed', 'delivery_incomplete'],
      ['uncertain', 'uncertain', 'delivery_uncertain'],
      ['sent', 'complete', 'completed'],
    ]) {
      health.record({
        type: 'outcome', event: 'offer', outcome: delivery === 'complete' ? 'handled' : 'failed',
        reason, video: false, videoDelivery, delivery, text: 'sent', audio: 'skipped',
        privatePath: 'private-fixture.mp4',
      });
      const saved = JSON.parse(fs.readFileSync(filePath, 'utf8'));
      assert.equal(saved.pipeline.video, false);
      assert.equal(saved.pipeline.video_delivery, videoDelivery);
      assert.equal(saved.pipeline.delivery, delivery);
      assert.equal(saved.pipeline.reason, reason);
      assert.doesNotMatch(JSON.stringify(saved), /private-fixture|privatePath/);
    }
    health.record({
      type: 'outcome', event: 'offer', video: 'sent', videoDelivery: 'private-video',
      delivery: 'private-delivery', text: 'private-text', reason: 'private-reason',
    });
    const safe = health.snapshot().pipeline;
    assert.equal(safe.video, null);
    assert.equal(safe.video_delivery, 'not_applicable');
    assert.equal(safe.delivery, 'not_applicable');
    assert.equal(safe.reason, 'unexpected_error');
    assert.doesNotMatch(JSON.stringify(safe), /private-/);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('normaliza valores desconocidos a enums seguros', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-health-'));
  const filePath = path.join(directory, 'health.json');
  const health = createWhatsAppCallHealth({
    filePath,
    idFactory: idFactory(),
    logger: { warn() {} },
  });

  health.record({
    type: 'outcome',
    event: 'private-573001234567',
    outcome: 'private',
    reason: 'private',
    reject: 'private',
    text: 'private',
    audio: 'private',
  });

  const snapshot = health.snapshot();
  assert.equal(snapshot.last_event, 'other');
  assert.equal(snapshot.pipeline.event, 'never');
  assert.equal(snapshot.pipeline.outcome, 'never');
  assert.equal(snapshot.pipeline.reason, 'never');
  assert.equal(snapshot.pipeline.reject, 'never');
  assert.doesNotMatch(JSON.stringify(snapshot), /573001234567|private/);

  fs.rmSync(directory, { recursive: true, force: true });
});

test('distingue cierre transitorio de una sesión que exige nuevo QR', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-health-'));
  const filePath = path.join(directory, 'health.json');
  const health = createWhatsAppCallHealth({
    filePath,
    idFactory: idFactory(),
    logger: { warn() {} },
  });

  health.record({
    type: 'connection',
    state: 'closed',
    reason: 'logged_out',
    reauthRequired: true,
  });

  assert.equal(health.snapshot().disconnect_reason, 'logged_out');
  assert.equal(health.snapshot().reauth_required, true);
  health.record({ type: 'connection', state: 'open' });
  assert.equal(health.snapshot().disconnect_reason, 'never');
  assert.equal(health.snapshot().reauth_required, false);
  health.record({
    type: 'connection',
    state: 'closed',
    reason: 'session_invalid',
    reauthRequired: true,
  });
  assert.equal(health.snapshot().disconnect_reason, 'session_invalid');
  assert.equal(health.snapshot().reauth_required, true);

  fs.rmSync(directory, { recursive: true, force: true });
});

test('conserva el resultado de la oferta aunque llegue terminate despues', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-health-'));
  const filePath = path.join(directory, 'health.json');
  const health = createWhatsAppCallHealth({
    filePath,
    idFactory: idFactory(),
    logger: { warn() {} },
  });

  health.record({
    type: 'outcome',
    event: 'offer',
    outcome: 'handled',
    reason: 'completed',
    reject: 'sent',
    text: 'sent',
    audio: 'sent',
  });
  const offerRevision = health.snapshot().pipeline_revision;
  health.record({
    type: 'outcome',
    event: 'terminate',
    outcome: 'ignored',
    reason: 'non_offer',
  });

  const snapshot = health.snapshot();
  assert.equal(snapshot.last_event, 'terminate');
  assert.equal(snapshot.pipeline_revision, offerRevision);
  assert.equal(snapshot.pipeline.event, 'offer');
  assert.equal(snapshot.pipeline.outcome, 'handled');
  assert.equal(snapshot.pipeline.text, 'sent');

  fs.rmSync(directory, { recursive: true, force: true });
});
