import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { createWhatsAppCallHandler } from '../wa_call_handler.mjs';
import { PersistentInteractionState } from '../interaction_state.mjs';
import { detectLanguageEvidence } from '../language_detection.mjs';

function createHarness(overrides = {}) {
  const effects = [];
  const metrics = [];
  const logger = { info() {}, warn() {}, error() {} };
  const handler = createWhatsAppCallHandler({
    rejectCall: async (id, from) => effects.push(['reject', id, from]),
    sendMessage: async (jid, content) => effects.push(['send', jid, content]),
    getCallMessage: () => ({ text: 'No podemos responder ahora.', audio: 'call.mp3' }),
    readAudio: async () => Buffer.from('audio'),
    getLanguage: () => 'es',
    logger,
    onCallMetric: (metric) => metrics.push(metric),
    ...overrides,
  });
  return { handler, effects, metrics };
}

function offer(overrides = {}) {
  return {
    id: 'call-1',
    from: '573001234567@s.whatsapp.net',
    chatId: '573001234567@s.whatsapp.net',
    status: 'offer',
    offline: false,
    isVideo: false,
    ...overrides,
  };
}

test('procesa el arreglo de llamadas que entrega Baileys', async () => {
  const { handler, effects } = createHarness();

  const result = await handler([offer()]);

  assert.equal(result[0].status, 'handled');
  assert.deepEqual(effects[0], ['reject', 'call-1', '573001234567@s.whatsapp.net']);
  assert.equal(effects[1][0], 'send');
  assert.equal(effects[1][2].mimetype, 'audio/mpeg');
  assert.deepEqual(effects[1][2].audio, Buffer.from('audio'));
  assert.deepEqual(effects[2], [
    'send',
    '573001234567@s.whatsapp.net',
    { text: 'No podemos responder ahora.' },
  ]);
});

test('envia OGG/Opus como nota de voz cuando el lector lo proporciona', async () => {
  const { handler, effects } = createHarness({
    readAudio: async () => ({
      buffer: Buffer.from('opus'),
      mimetype: 'audio/ogg; codecs=opus',
      ptt: true,
    }),
  });

  await handler([offer()]);

  assert.deepEqual(effects[1][2], {
    audio: Buffer.from('opus'),
    mimetype: 'audio/ogg; codecs=opus',
    ptt: true,
  });
  assert.equal(effects[2][2].text, 'No podemos responder ahora.');
});

test('espera la confirmacion del audio antes de iniciar el texto', async () => {
  const deliveries = [];
  let releaseAudio;
  let markAudioStarted;
  const audioGate = new Promise(resolve => { releaseAudio = resolve; });
  const audioStarted = new Promise(resolve => { markAudioStarted = resolve; });
  const { handler } = createHarness({
    sendMessage: async (_jid, content) => {
      if (content.audio) {
        deliveries.push('audio');
        markAudioStarted();
        await audioGate;
      } else {
        deliveries.push('text');
      }
    },
  });

  const pending = handler([offer()]);
  await audioStarted;
  await Promise.resolve();
  assert.deepEqual(deliveries, ['audio']);

  releaseAudio();
  await pending;
  assert.deepEqual(deliveries, ['audio', 'text']);
});

test('procesa todas las ofertas del mismo lote de forma aislada', async () => {
  const { handler, effects } = createHarness();

  const result = await handler([
    offer(),
    offer({ id: 'call-2', from: '573009999999@s.whatsapp.net', chatId: '573009999999@s.whatsapp.net' }),
  ]);

  assert.equal(result.filter((item) => item.status === 'handled').length, 2);
  assert.equal(effects.filter(([kind]) => kind === 'reject').length, 2);
  assert.equal(effects.filter(([kind]) => kind === 'send').length, 4);
});

test('ignora estados que no son una oferta', async () => {
  const { handler, effects } = createHarness();

  const result = await handler([offer({ status: 'ringing' }), offer({ status: 'terminate' })]);

  assert.deepEqual(result.map((item) => item.status), ['ignored', 'ignored']);
  assert.equal(effects.length, 0);
});

test('responde una oferta offline sin intentar rechazar una llamada histórica', async () => {
  const { handler, effects } = createHarness();

  const result = await handler([offer({ offline: true })]);

  assert.equal(result[0].status, 'handled');
  assert.equal(result[0].reject, 'skipped_offline');
  assert.equal(effects.filter(([kind]) => kind === 'reject').length, 0);
  assert.equal(effects.filter(([kind]) => kind === 'send').length, 2);
});

test('ignora llamadas grupales para no responder dentro del grupo', async () => {
  const { handler, effects } = createHarness();

  const result = await handler([offer({ isGroup: true })]);

  assert.equal(result[0].status, 'ignored');
  assert.equal(result[0].reason, 'group_call');
  assert.equal(effects.length, 0);
});

test('deduplica la misma oferta en callbacks repetidos y concurrentes', async () => {
  const { handler, effects } = createHarness();

  await Promise.all([handler([offer()]), handler([offer()])]);
  await handler([offer()]);

  assert.equal(effects.filter(([kind]) => kind === 'reject').length, 1);
  assert.equal(effects.filter(([kind]) => kind === 'send').length, 2);
});

test('responde aunque falle el rechazo de la llamada', async () => {
  const { handler, effects } = createHarness({
    rejectCall: async () => {
      throw new Error('call already ended');
    },
  });

  const result = await handler([offer()]);

  assert.equal(result[0].reject, 'failed');
  assert.equal(result[0].text, 'sent');
  assert.equal(result[0].audio, 'sent');
  assert.equal(effects.filter(([kind]) => kind === 'send').length, 2);
});

test('un rechazo bloqueado vence y la respuesta continúa', async () => {
  const { handler, effects } = createHarness({
    rejectCall: () => new Promise(() => {}),
    rejectTimeoutMs: 5,
  });

  const result = await handler([offer()]);

  assert.equal(result[0].reject, 'failed');
  assert.equal(result[0].text, 'sent');
  assert.equal(result[0].audio, 'sent');
  assert.equal(effects.filter(([kind]) => kind === 'send').length, 2);
});

test('un fallo enviando texto ocurre despues de haber enviado el audio', async () => {
  const effects = [];
  const { handler } = createHarness({
    sendMessage: async (jid, content) => {
      effects.push(['send', jid, content]);
      if (content.text) throw new Error('text unavailable');
    },
  });

  const result = await handler([offer()]);

  assert.equal(result[0].text, 'uncertain');
  assert.equal(result[0].status, 'failed');
  assert.equal(result[0].delivery, 'uncertain');
  assert.equal(result[0].audio, 'sent');
  assert.equal(effects.length, 2);
  assert.ok(effects[0][2].audio);
  assert.equal(effects[1][2].text, 'No podemos responder ahora.');
});

test('un audio ausente no impide enviar el texto', async () => {
  const { handler, effects } = createHarness({ readAudio: async () => null });

  const result = await handler([offer()]);

  assert.equal(result[0].text, 'sent');
  assert.equal(result[0].audio, 'missing');
  assert.equal(effects.filter(([kind]) => kind === 'send').length, 1);
  assert.equal(effects[1][2].text, 'No podemos responder ahora.');
});

test('usa chatId para responder y from para rechazar', async () => {
  const { handler, effects } = createHarness();

  await handler([
    offer({
      from: '12345@lid',
      chatId: '573001234567@s.whatsapp.net',
    }),
  ]);

  assert.deepEqual(effects[0], ['reject', 'call-1', '12345@lid']);
  assert.equal(effects[1][1], '573001234567@s.whatsapp.net');
  assert.equal(effects[2][1], '573001234567@s.whatsapp.net');
});

test('prefiere callerPn para responder cuando chatId es un LID', async () => {
  const { handler, effects, metrics } = createHarness();

  await handler([
    offer({
      from: '12345@lid',
      chatId: '12345@lid',
      callerPn: '573001234567@s.whatsapp.net',
    }),
  ]);

  assert.deepEqual(effects[0], ['reject', 'call-1', '12345@lid']);
  assert.equal(effects[1][1], '573001234567@s.whatsapp.net');
  assert.equal(effects[2][1], '573001234567@s.whatsapp.net');
  const outcome = metrics.find((metric) => metric.type === 'outcome');
  assert.equal(outcome.target, 'caller_pn');
  assert.equal(outcome.targetKind, 'pn');
});

test('prefiere from con número antes que un chatId LID', async () => {
  const { handler, effects } = createHarness();

  await handler([offer({
    from: '573001234567@s.whatsapp.net',
    chatId: '12345@lid',
    callerPn: undefined,
  })]);

  assert.equal(effects[1][1], '573001234567@s.whatsapp.net');
  assert.equal(effects[2][1], '573001234567@s.whatsapp.net');
});

test('normaliza el sufijo de dispositivo al responder una llamada', async () => {
  const { handler, effects } = createHarness();

  await handler([offer({
    from: '573001234567:8@s.whatsapp.net',
    chatId: '573001234567:8@s.whatsapp.net',
  })]);

  assert.deepEqual(effects[0], ['reject', 'call-1', '573001234567:8@s.whatsapp.net']);
  assert.equal(effects[1][1], '573001234567@s.whatsapp.net');
  assert.equal(effects[2][1], '573001234567@s.whatsapp.net');
});

test('ignora callerPn malformado y cae a chatId', async () => {
  const { handler, effects, metrics } = createHarness();

  await handler([
    offer({
      from: '12345@lid',
      chatId: '573001234567@s.whatsapp.net',
      callerPn: 'not-a-phone@lid',
    }),
  ]);

  assert.equal(effects[1][1], '573001234567@s.whatsapp.net');
  assert.equal(effects[2][1], '573001234567@s.whatsapp.net');
  const outcome = metrics.find((metric) => metric.type === 'outcome');
  assert.equal(outcome.target, 'chat_id');
  assert.equal(outcome.targetKind, 'pn');
});

test('tolera payload vacío o un objeto individual', async () => {
  const { handler, effects } = createHarness();

  assert.deepEqual(await handler(null), []);
  const result = await handler(offer());

  assert.equal(result[0].status, 'handled');
  assert.equal(effects.filter(([kind]) => kind === 'reject').length, 1);
});

test('emite diagnostico anonimizado por lote y resultado', async () => {
  const { handler, metrics } = createHarness();

  await handler([offer()]);

  assert.equal(metrics[0].type, 'batch');
  assert.deepEqual(metrics[0], { type: 'batch', payload: 'array', size: 'one' });
  assert.equal(metrics[1].type, 'outcome');
  assert.equal(metrics[1].event, 'offer');
  assert.equal(metrics[1].outcome, 'handled');
  assert.equal(metrics[1].reason, 'completed');
  assert.equal(metrics[1].reject, 'sent');
  assert.equal(metrics[1].text, 'sent');
  assert.equal(metrics[1].audio, 'sent');

  const serialized = JSON.stringify(metrics);
  assert.doesNotMatch(serialized, /573001234567|@s\.whatsapp\.net|call-1|call\.mp3|No podemos/);
});

test('un observador de diagnostico defectuoso no altera la respuesta', async () => {
  const { handler, effects } = createHarness({
    onCallMetric: () => {
      throw new Error('diagnostic unavailable');
    },
  });

  const result = await handler([offer()]);

  assert.equal(result[0].status, 'handled');
  assert.equal(effects.filter(([kind]) => kind === 'send').length, 2);
});

test('cada rama ignorada emite una razon de cardinalidad cerrada', async () => {
  const { handler, metrics } = createHarness();

  await handler([
    offer({ status: 'future_status_with_private_data_573001234567' }),
    offer({ id: '' }),
    offer({ id: 'call-2', isGroup: true }),
  ]);

  const outcomes = metrics.filter((metric) => metric.type === 'outcome');
  assert.deepEqual(outcomes.map((metric) => metric.reason), [
    'non_offer',
    'missing_identity',
    'group_call',
  ]);
  assert.doesNotMatch(JSON.stringify(outcomes), /573001234567|@s\.whatsapp\.net|call-/);
});

test('cada intento de llamada usa CALL aunque existan llamadas anteriores', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-state-'));
  const state = new PersistentInteractionState({
    filePath: path.join(directory, 'state.json'),
    logger: { error() {} },
  });
  const effects = [];
  const handler = createWhatsAppCallHandler({
    rejectCall: async (id, from) => effects.push(['reject', id, from]),
    sendMessage: async (jid, content) => effects.push(['send', jid, content]),
    getResponseMessage: (_lang, key) => ({ text: key, audio: `${key}.mp3` }),
    routeInteraction: details => state.register(details),
    readAudio: async filename => Buffer.from(filename),
    logger: { info() {}, warn() {}, error() {} },
  });

  const first = await handler([offer({ id: 'call-1' })]);
  const second = await handler([offer({ id: 'call-2' })]);

  assert.equal(first[0].response, 'call');
  assert.equal(second[0].response, 'call');
  assert.deepEqual(
    effects.filter(([kind, , content]) => kind === 'send' && content.text)
      .map(([, , content]) => content.text),
    ['call', 'call'],
  );
});

test('primera llamada usa provisional del prefijo y texto detectado puede reemplazarlo', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-state-'));
  const filePath = path.join(directory, 'state.json');
  const state = new PersistentInteractionState({ filePath });
  const languages = [];
  const handler = createWhatsAppCallHandler({
    rejectCall: async () => {},
    sendMessage: async () => {},
    getResponseMessage: (language, key) => {
      languages.push([language, key]);
      return { text: '', audio: '' };
    },
    routeInteraction: details => state.register(details),
    resolveContactId: async () => '573001234567@s.whatsapp.net',
    readAudio: async () => null,
    logger: { info() {}, warn() {}, error() {} },
  });

  await handler([offer({
    from: '123456789@lid',
    chatId: '123456789@lid',
    callerPn: undefined,
  })]);
  const followingText = state.register({
    contactId: '573001234567@s.whatsapp.net',
    eventId: 'message:english',
    kind: 'content',
    detectedLanguage: 'en',
    languageEvidence: detectLanguageEvidence('Are you available now?'),
  });

  assert.deepEqual(languages, [['es', 'call']]);
  assert.equal(followingText.language, 'en');
  const contact = Object.values(JSON.parse(fs.readFileSync(filePath, 'utf8')).contacts)[0];
  assert.equal(contact.language_provisional, false);
});

test('una llamada posterior a un mensaje también envía CALL', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-state-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  state.register({
    contactId: '573001234567@s.whatsapp.net',
    eventId: 'message:first',
    kind: 'content',
  });
  const effects = [];
  const handler = createWhatsAppCallHandler({
    rejectCall: async () => {},
    sendMessage: async (jid, content) => effects.push(['send', jid, content]),
    getResponseMessage: (_lang, key) => ({ text: key, audio: '' }),
    routeInteraction: details => state.register(details),
    readAudio: async () => null,
    logger: { info() {}, warn() {}, error() {} },
  });

  const result = await handler([offer({ id: 'call-after-message' })]);

  assert.equal(result[0].response, 'call');
  assert.equal(effects[0][2].text, 'call');
});

test('la deduplicación persistente evita repetir una llamada tras reiniciar', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-state-'));
  const filePath = path.join(directory, 'state.json');
  const firstState = new PersistentInteractionState({ filePath });
  const firstHarness = createWhatsAppCallHandler({
    rejectCall: async () => {},
    sendMessage: async () => {},
    getResponseMessage: (_lang, key) => ({ text: key, audio: '' }),
    routeInteraction: details => firstState.register(details),
    readAudio: async () => null,
    logger: { info() {}, warn() {}, error() {} },
  });
  await firstHarness([offer()]);

  const effects = [];
  const reloadedState = new PersistentInteractionState({ filePath });
  const secondHarness = createWhatsAppCallHandler({
    rejectCall: async () => effects.push('reject'),
    sendMessage: async () => effects.push('send'),
    getResponseMessage: (_lang, key) => ({ text: key, audio: '' }),
    routeInteraction: details => reloadedState.register(details),
    readAudio: async () => null,
    logger: { info() {}, warn() {}, error() {} },
  });

  const result = await secondHarness([offer()]);

  assert.equal(result[0].reason, 'duplicate');
  assert.deepEqual(effects, ['reject']);
});

test('si falla el mapeo LID todavía rechaza y responde la llamada', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-call-state-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  const effects = [];
  const handler = createWhatsAppCallHandler({
    rejectCall: async () => effects.push('reject'),
    sendMessage: async () => effects.push('send'),
    getResponseMessage: (_lang, key) => ({ text: key, audio: '' }),
    routeInteraction: details => state.register(details),
    resolveContactId: async () => { throw new Error('mapping unavailable'); },
    readAudio: async () => null,
    logger: { info() {}, warn() {}, error() {} },
  });

  const result = await handler([offer({ from: '123@lid', chatId: '123@lid' })]);

  assert.equal(result[0].status, 'handled');
  assert.deepEqual(effects, ['reject', 'send']);
});

test('si falla el estado la llamada se rechaza de todas formas', async () => {
  const effects = [];
  const handler = createWhatsAppCallHandler({
    rejectCall: async () => effects.push('reject'),
    sendMessage: async () => effects.push('send'),
    getResponseMessage: () => ({ text: 'x', audio: '' }),
    routeInteraction: async () => { throw new Error('disk unavailable'); },
    readAudio: async () => null,
    logger: { info() {}, warn() {}, error() {} },
  });

  const result = await handler([offer()]);

  assert.equal(result[0].reason, 'interaction_state_failed');
  assert.deepEqual(effects, ['reject']);
});

test('operational pause rejects the call without consuming interaction state', async () => {
  let allowed = false;
  let routes = 0;
  const effects = [];
  const handler = createWhatsAppCallHandler({
    rejectCall: async () => effects.push('reject'),
    sendMessage: async () => effects.push('send'),
    deliveryAllowed: () => allowed,
    getResponseMessage: () => ({ text: 'call', audio: '' }),
    routeInteraction: async () => {
      routes += 1;
      return { language: 'es', responseKey: 'call', contactKey: 'contact' };
    },
    readAudio: async () => null,
    logger: { info() {}, warn() {}, error() {} },
  });

  const blocked = await handler([offer({ id: 'paused-call' })]);
  allowed = true;
  const resumed = await handler([offer({ id: 'resumed-call' })]);

  assert.equal(blocked[0].reason, 'delivery_blocked');
  assert.equal(resumed[0].status, 'handled');
  assert.equal(routes, 1);
  assert.deepEqual(effects, ['reject', 'reject', 'send']);
});

test('entrega video, texto y audio en respuesta a llamada cuando hay video configurado', async () => {
  const effects = [];
  const handler = createWhatsAppCallHandler({
    rejectCall: async (id, from) => effects.push(['reject', id, from]),
    sendMessage: async (jid, content) => effects.push(['send', jid, content]),
    getCallMessage: () => ({ text: 'Te llamamos pronto', audio: 'call.mp3', video: 'call.mp4' }),
    readAudio: async () => Buffer.from('call-audio'),
    readVideo: async () => ({ buffer: Buffer.from('call-video'), mimetype: 'video/mp4' }),
    getLanguage: () => 'es',
    logger: { info() {}, warn() {}, error() {} },
  });

  const result = await handler([offer({ id: 'call-video-1' })]);

  assert.equal(result[0].status, 'handled');
  assert.equal(result[0].audio, 'sent');
  assert.equal(result[0].video, 'sent');
  assert.equal(result[0].text, 'sent');
  assert.equal(result[0].delivery, 'complete');
  const sentContents = effects.filter(([kind]) => kind === 'send').map(([, , content]) => content);
  assert.equal(sentContents.length, 3);
  assert.deepEqual(sentContents[0], {
    video: Buffer.from('call-video'),
    mimetype: 'video/mp4',
  });
  assert.deepEqual(sentContents[1], { text: 'Te llamamos pronto' });
  assert.ok(sentContents[2].audio);
});

function videoFirstCallHarness(t, overrides = {}) {
  const temporaryRoot = fs.realpathSync(os.tmpdir());
  const directory = fs.mkdtempSync(path.join(temporaryRoot, 'wa-call-video-first-'));
  t.after(() => {
    const target = fs.realpathSync(directory);
    assert.equal(path.dirname(target), temporaryRoot);
    fs.rmSync(target, { recursive: true, force: true });
  });
  const statePath = path.join(directory, 'state.json');
  const effects = [];
  const metrics = [];
  const create = () => {
    const state = new PersistentInteractionState({ filePath: statePath });
    return createWhatsAppCallHandler({
      rejectCall: async () => {},
      sendMessage: async (_jid, content) => effects.push(content),
      routeInteraction: details => state.register(details),
      getResponseMessage: () => ({ text: 'call', audio: 'audio.mp3', video: 'video.mp4' }),
      readAudio: async () => Buffer.from('audio'),
      readVideo: async () => ({ buffer: Buffer.from('video'), mimetype: 'video/mp4' }),
      onCallMetric: metric => metrics.push(metric),
      sendTimeoutMs: 100,
      logger: { info() {}, warn() {}, error() {} },
      ...overrides,
    });
  };
  return { handler: create(), create, effects, metrics };
}

test('call waits for video acknowledgement before text and audio', async t => {
  const effects = [];
  let releaseVideo;
  let signalStarted;
  const started = new Promise(resolve => { signalStarted = resolve; });
  const gate = new Promise(resolve => { releaseVideo = resolve; });
  const files = videoFirstCallHarness(t, {
    sendMessage: async (_jid, content) => {
      effects.push(content.video ? 'video' : content.text ? 'text' : 'audio');
      if (content.video) {
        signalStarted();
        await gate;
      }
    },
  });
  const pending = files.handler([offer({ id: 'video-call-order' })]);
  await started;
  assert.deepEqual(effects, ['video']);
  releaseVideo();
  const [result] = await pending;
  assert.equal(result.response, 'call');
  assert.equal(result.delivery, 'complete');
  assert.deepEqual(effects, ['video', 'text', 'audio']);
});

test('call video with empty audio skips its reader and keeps video then text', async t => {
  const files = videoFirstCallHarness(t, {
    getResponseMessage: () => ({ text: 'call', audio: '', video: 'video.mp4' }),
    readAudio: async () => { throw new Error('empty audio must not be read'); },
  });
  const [result] = await files.handler([offer({ id: 'video-no-audio' })]);
  assert.equal(result.audio, 'skipped');
  assert.equal(result.delivery, 'complete');
  assert.deepEqual(files.effects.map(content => content.video ? 'video' : 'text'), ['video', 'text']);
});

test('call without video retains audio then text and never reads video', async t => {
  const files = videoFirstCallHarness(t, {
    getResponseMessage: () => ({ text: 'call', audio: 'audio.mp3', video: '' }),
    readVideo: async () => { throw new Error('unconfigured video must not be read'); },
  });
  const [result] = await files.handler([offer({ id: 'without-video' })]);
  assert.equal(result.video, 'skipped');
  assert.equal(result.delivery, 'complete');
  assert.deepEqual(files.effects.map(content => content.audio ? 'audio' : 'text'), ['audio', 'text']);
});

test('call with only a missing video cannot report a completed delivery', async t => {
  const files = videoFirstCallHarness(t, {
    getResponseMessage: () => ({ text: '', audio: '', video: 'video.mp4' }),
    readVideo: async () => null,
  });
  const [result] = await files.handler([offer({ id: 'video-only-missing' })]);
  assert.equal(result.status, 'failed');
  assert.equal(result.delivery, 'failed');
  assert.equal(result.reason, 'delivery_incomplete');
  assert.deepEqual([result.text, result.audio, result.video], ['skipped', 'skipped', 'missing']);
  assert.deepEqual(files.effects, []);
});

test('call response preparation failure is reported without claiming delivery success', async t => {
  const files = videoFirstCallHarness(t, {
    getResponseMessage: () => { throw new Error('synthetic configuration failure'); },
  });
  const [result] = await files.handler([offer({ id: 'preparation-error' })]);
  assert.equal(result.status, 'failed');
  assert.equal(result.delivery, 'failed');
  assert.equal(result.reason, 'delivery_incomplete');
  assert.deepEqual(files.effects, []);
});

for (const failure of ['missing', 'read_error', 'send_error', 'timeout']) {
  test(`call video ${failure} preserves fallback order, truthful metrics and persistent deduplication`, async t => {
    const effects = [];
    let releaseLateVideo;
    const lateVideo = new Promise(resolve => { releaseLateVideo = resolve; });
    const files = videoFirstCallHarness(t, {
      sendTimeoutMs: 10,
      readVideo: async () => {
        effects.push('read_video');
        if (failure === 'missing') return null;
        if (failure === 'read_error') throw new Error('synthetic read failure');
        return Buffer.from('video');
      },
      sendMessage: async (_jid, content) => {
        const component = content.video ? 'video' : content.text ? 'text' : 'audio';
        effects.push(component);
        if (component === 'video' && failure === 'send_error') throw new Error('lost acknowledgement');
        if (component === 'video' && failure === 'timeout') await lateVideo;
      },
    });
    const event = offer({ id: `video-${failure}` });
    const [result] = await files.handler([event]);
    const uncertain = ['send_error', 'timeout'].includes(failure);
    assert.equal(result.video, uncertain ? 'uncertain' : failure === 'missing' ? 'missing' : 'failed');
    assert.equal(result.status, 'failed');
    assert.equal(result.delivery, uncertain ? 'uncertain' : 'partial');
    assert.equal(result.text, 'sent');
    assert.equal(result.audio, 'sent');
    assert.deepEqual(effects, uncertain ? ['read_video', 'video', 'text', 'audio'] : ['read_video', 'text', 'audio']);
    const outcome = files.metrics.find(metric => metric.type === 'outcome');
    assert.equal(outcome.outcome, 'failed');
    assert.equal(outcome.reason, uncertain ? 'delivery_uncertain' : 'delivery_incomplete');
    assert.equal(outcome.delivery, result.delivery);
    assert.equal(outcome.videoDelivery, result.video);
    assert.equal(outcome.video, false);
    releaseLateVideo();
    await new Promise(resolve => setImmediate(resolve));
    const beforeDuplicate = [...effects];
    const [duplicate] = await files.create()([event]);
    assert.equal(duplicate.reason, 'duplicate');
    assert.deepEqual(effects, beforeDuplicate);
    assert.equal(result.delivery, uncertain ? 'uncertain' : 'partial');
  });
}
