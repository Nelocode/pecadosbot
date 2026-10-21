import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { PersistentInteractionState } from '../interaction_state.mjs';
import { createWhatsAppMessageHandler, describeInteraction } from '../wa_message_handler.mjs';
import { KeyedSerialQueue } from '../keyed_serial_queue.mjs';
import { detectLanguageEvidence } from '../language_detection.mjs';

function incoming(id, message, overrides = {}) {
  return {
    key: {
      id,
      fromMe: false,
      remoteJid: '573001234567@s.whatsapp.net',
      ...overrides,
    },
    message,
  };
}

function createHarness() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-handler-'));
  const state = new PersistentInteractionState({
    filePath: path.join(directory, 'state.json'),
    logger: { error() {} },
  });
  const effects = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (jid, content) => effects.push([jid, content]),
    routeInteraction: details => state.register(details),
    getResponseMessage: (_lang, key) => ({ text: key, audio: `${key}.mp3` }),
    readAudio: async filename => Buffer.from(filename),
    detectLanguage: text => text === 'bonjour' ? 'fr' : 'es',
    logger: { info() {}, warn() {}, error() {} },
  });
  return { handler, effects, state };
}

test('texto, voz, imagen y archivo se reconocen como interacciones', () => {
  const messages = [
    { conversation: 'hola' },
    { audioMessage: { ptt: true } },
    { ptvMessage: {} },
    { imageMessage: { caption: '' } },
    { documentMessage: { fileName: 'x.pdf' } },
    { albumMessage: { expectedImageCount: 2 } },
    { associatedChildMessage: { message: { imageMessage: {} } } },
    { ephemeralMessage: { message: { videoMessage: { caption: 'hola' } } } },
  ];

  for (const message of messages) assert.ok(describeInteraction(message));
  assert.equal(describeInteraction({ protocolMessage: { type: 0 } }), null);
  assert.equal(describeInteraction({ reactionMessage: { text: '👍' } }), null);
  assert.equal(describeInteraction({
    editedMessage: { message: { conversation: 'texto editado' } },
  }), null);
});

test('primera multimedia usa Paso 1 y todas las posteriores Paso 2', async () => {
  const { handler, effects } = createHarness();

  const results = await handler({
    type: 'notify',
    messages: [
      incoming('1', { imageMessage: {} }),
      incoming('2', { audioMessage: { ptt: true } }),
      incoming('3', { documentMessage: { fileName: 'x.pdf' } }),
    ],
  });

  assert.deepEqual(results.map(result => result.response), ['step1', 'step2', 'step2']);
  assert.deepEqual(
    effects.filter(([, content]) => content.text).map(([, content]) => content.text),
    ['step1', 'step2', 'step2'],
  );
});

test('imagen con descripción cuenta una sola vez y fija el idioma', async () => {
  const { handler, effects } = createHarness();

  await handler({
    type: 'notify',
    messages: [incoming('1', { imageMessage: { caption: 'bonjour' } })],
  });

  assert.equal(effects.length, 2);
  assert.equal(effects[0][1].text, 'step1');
});

test('caption ambiguo usa prefijo provisional y texto claro posterior puede reemplazarlo', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-handler-'));
  const filePath = path.join(directory, 'state.json');
  const state = new PersistentInteractionState({ filePath });
  const languages = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async () => {},
    routeInteraction: details => state.register(details),
    getResponseMessage: (language, key) => {
      languages.push([language, key]);
      return { text: key, audio: '' };
    },
    readAudio: async () => null,
    detectLanguage: detectLanguageEvidence,
    logger: { info() {}, warn() {}, error() {} },
  });

  await handler({
    type: 'notify',
    messages: [incoming('image', { imageMessage: { caption: 'photo' } })],
  });
  await handler({
    type: 'notify',
    messages: [incoming('text', { conversation: 'Are you available now?' })],
  });

  assert.deepEqual(languages, [['es', 'step1'], ['en', 'step2']]);
  const persisted = JSON.parse(fs.readFileSync(filePath, 'utf8'));
  const contact = Object.values(persisted.contacts)[0];
  assert.equal(contact.language, 'en');
  assert.equal(contact.language_provisional, false);
});

test('WhatsApp corrige provisional francés con Mándame información', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-handler-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  const languages = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async () => {},
    routeInteraction: details => state.register(details),
    getResponseMessage: (language, key) => {
      languages.push([language, key]);
      return { text: key, audio: '' };
    },
    readAudio: async () => null,
    detectLanguage: detectLanguageEvidence,
    logger: { info() {}, warn() {}, error() {} },
  });
  const frenchContact = { remoteJid: '33612345678@s.whatsapp.net' };

  await handler({
    type: 'notify',
    messages: [incoming('image-fr', { imageMessage: {} }, frenchContact)],
  });
  await handler({
    type: 'notify',
    messages: [incoming('text-es', { conversation: 'Mándame información' }, frenchContact)],
  });

  assert.deepEqual(languages, [['fr', 'step1'], ['es', 'step2']]);
  const contact = Object.values(state.contacts)[0];
  assert.equal(contact.language, 'es');
  assert.equal(contact.language_source, 'detected');
  assert.equal(contact.language_candidate, null);
});

test('texto inicial prima sobre el indicio del prefijo telefónico', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-handler-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  const languages = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async () => {},
    routeInteraction: details => state.register(details),
    getResponseMessage: (language, key) => {
      languages.push([language, key]);
      return { text: '', audio: '' };
    },
    readAudio: async () => null,
    detectLanguage: detectLanguageEvidence,
    logger: { info() {}, warn() {}, error() {} },
  });

  await handler({
    type: 'notify',
    messages: [incoming('text-first', { conversation: 'hello' })],
  });

  assert.deepEqual(languages, [['en', 'step1']]);
  assert.equal(Object.values(state.contacts)[0].language_provisional, false);
});

test('WhatsApp cambia de francés confirmado a español ante evidencia fuerte', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-language-override-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  state.register({
    contactId: '573001234567@s.whatsapp.net',
    eventId: 'seed:french',
    kind: 'content',
    languageEvidence: detectLanguageEvidence('bonjour'),
  });
  const languages = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async () => {},
    routeInteraction: details => state.register(details),
    getResponseMessage: (language, key) => {
      languages.push([language, key]);
      return { text: key, audio: '' };
    },
    readAudio: async () => null,
    detectLanguage: detectLanguageEvidence,
    logger: { info() {}, warn() {}, error() {} },
  });

  await handler({
    type: 'notify',
    messages: [incoming('spanish-strong', {
      conversation: 'que chicas están disponibles por Rubí',
    })],
  });

  assert.deepEqual(languages, [['es', 'step2']]);
  const contact = Object.values(state.contacts)[0];
  assert.equal(contact.language, 'es');
  assert.equal(contact.language_source, 'detected');
  assert.equal(contact.language_candidate, null);
});

test('envia OGG/Opus como nota de voz cuando el lector lo proporciona', async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-handler-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  const effects = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (jid, content) => effects.push([jid, content]),
    routeInteraction: details => state.register(details),
    getResponseMessage: () => ({ text: '', audio: 'step1.mp3' }),
    readAudio: async () => ({
      buffer: Buffer.from('opus'),
      mimetype: 'audio/ogg; codecs=opus',
      ptt: true,
    }),
    detectLanguage: () => 'es',
    logger: { info() {}, warn() {}, error() {} },
  });

  await handler({
    type: 'notify',
    messages: [incoming('voice-note-output', { conversation: 'hola' })],
  });

  assert.deepEqual(effects[0][1], {
    audio: Buffer.from('opus'),
    mimetype: 'audio/ogg; codecs=opus',
    ptt: true,
  });
});

test('el mismo id repetido no responde ni avanza dos veces', async () => {
  const { handler, effects } = createHarness();
  const message = incoming('same', { conversation: 'hola' });

  await handler({ type: 'notify', messages: [message] });
  const duplicate = await handler({ type: 'notify', messages: [message] });
  const next = await handler({ type: 'notify', messages: [incoming('next', { conversation: 'hola' })] });

  assert.equal(duplicate[0].reason, 'duplicate');
  assert.equal(next[0].response, 'step2');
  assert.equal(effects.length, 4);
});

test('ignora salientes, grupos, estados y eventos técnicos', async () => {
  const { handler, effects } = createHarness();
  const results = await handler({
    type: 'notify',
    messages: [
      incoming('1', { conversation: 'hola' }, { fromMe: true }),
      incoming('2', { conversation: 'hola' }, { remoteJid: '123@g.us' }),
      incoming('3', { conversation: 'hola' }, { remoteJid: 'status@broadcast' }),
      incoming('4', { protocolMessage: { type: 0 } }),
    ],
  });

  assert.equal(results.every(result => result.status === 'ignored'), true);
  assert.equal(effects.length, 0);
});

test('resuelve un LID al número para compartir identidad y responder', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-handler-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  const effects = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (jid, content) => effects.push([jid, content]),
    routeInteraction: details => state.register(details),
    getResponseMessage: (_lang, key) => ({ text: key, audio: '' }),
    readAudio: async () => null,
    detectLanguage: () => 'es',
    resolvePnForLid: async () => '573001234567@s.whatsapp.net',
    logger: { info() {}, warn() {}, error() {} },
  });

  await handler({
    type: 'notify',
    messages: [incoming('1', { conversation: 'hola' }, { remoteJid: '123@lid' })],
  });

  assert.equal(effects[0][0], '573001234567@s.whatsapp.net');
});

test('normaliza el sufijo de dispositivo para conservar una sola identidad', async () => {
  const { handler, effects } = createHarness();

  await handler({
    type: 'notify',
    messages: [incoming('1', { conversation: 'hola' }, {
      remoteJid: '573001234567:4@s.whatsapp.net',
    })],
  });

  assert.equal(effects[0][0], '573001234567@s.whatsapp.net');
});

test('un padre de álbum y sus hijos cuentan como una sola interacción', async () => {
  const { handler, effects } = createHarness();
  const parent = incoming('album-1', { albumMessage: { expectedImageCount: 1 } });
  const child = incoming('child-1', {
    imageMessage: {},
    messageContextInfo: {
      messageAssociation: { parentMessageKey: { id: 'album-1' } },
    },
  });

  const results = await handler({ type: 'notify', messages: [parent, child] });

  assert.equal(results[0].reason, 'album_placeholder');
  assert.equal(results[1].response, 'step1');
  assert.equal(effects.length, 2);
});

test('un hijo de álbum envuelto conserva el id del padre', async () => {
  const { handler, effects } = createHarness();
  const parent = incoming('album-2', { albumMessage: { expectedImageCount: 1 } });
  const child = incoming('child-2', {
    associatedChildMessage: {
      message: {
        imageMessage: {},
        messageContextInfo: {
          messageAssociation: { parentMessageKey: { id: 'album-2' } },
        },
      },
    },
  });

  const results = await handler({ type: 'notify', messages: [parent, child] });

  assert.equal(results[0].reason, 'album_placeholder');
  assert.equal(results[1].response, 'step1');
  assert.equal(effects.length, 2);
});

test('padre vacío en callback previo no consume el caption posterior', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-album-callbacks-'));
  const filePath = path.join(directory, 'state.json');
  const state = new PersistentInteractionState({ filePath });
  const effects = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (_jid, content) => effects.push(content),
    routeInteraction: details => state.register(details),
    getResponseMessage: (_language, key) => ({ text: key, audio: '' }),
    readAudio: async () => null,
    detectLanguage: detectLanguageEvidence,
    logger: { info() {}, warn() {}, error() {} },
  });
  const parent = incoming('album-callbacks', {
    albumMessage: { expectedImageCount: 1 },
  }, {
    remoteJid: '123456789@lid',
  });
  const child = incoming('child-callbacks', {
    imageMessage: { caption: 'Are you available now?' },
    messageContextInfo: {
      messageAssociation: { parentMessageKey: { id: 'album-callbacks' } },
    },
  }, {
    remoteJid: '573001234567@s.whatsapp.net',
    remoteJidAlt: '123456789@lid',
  });

  const first = await handler({ type: 'notify', messages: [parent] });
  assert.equal(first[0].reason, 'album_placeholder');
  assert.deepEqual(effects, []);
  assert.equal(Object.keys(state.contacts).length, 0);
  assert.equal(fs.existsSync(filePath), false);

  const second = await handler({ type: 'notify', messages: [child] });

  assert.equal(second[0].response, 'step1');
  assert.deepEqual(effects, [{ text: 'step1' }]);
  const contact = Object.values(JSON.parse(fs.readFileSync(filePath, 'utf8')).contacts)[0];
  assert.equal(contact.language, 'en');
  assert.equal(contact.language_provisional, false);
});

test('caption del hijo se procesa antes que el padre vacío del mismo álbum', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-album-language-'));
  const filePath = path.join(directory, 'state.json');
  const state = new PersistentInteractionState({ filePath });
  const effects = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (_jid, content) => effects.push(content),
    routeInteraction: details => state.register(details),
    getResponseMessage: (_language, key) => ({ text: key, audio: '' }),
    readAudio: async () => null,
    detectLanguage: detectLanguageEvidence,
    logger: { info() {}, warn() {}, error() {} },
  });
  const parent = incoming('album-language', {
    albumMessage: { expectedImageCount: 1 },
  }, {
    remoteJid: '123456789@lid',
  });
  const child = incoming('child-language', {
    imageMessage: { caption: 'Are you available now?' },
    messageContextInfo: {
      messageAssociation: { parentMessageKey: { id: 'album-language' } },
    },
  }, {
    remoteJid: '573001234567@s.whatsapp.net',
    remoteJidAlt: '123456789@lid',
  });

  const results = await handler({ type: 'notify', messages: [parent, child] });

  assert.equal(results[0].reason, 'album_placeholder');
  assert.equal(results[1].response, 'step1');
  assert.deepEqual(effects, [{ text: 'step1' }]);
  const contact = Object.values(JSON.parse(fs.readFileSync(filePath, 'utf8')).contacts)[0];
  assert.equal(contact.language, 'en');
  assert.equal(contact.language_provisional, false);
});

test('solapamiento transitivo LID a PN agrupa todo el álbum', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-album-transitive-'));
  const filePath = path.join(directory, 'state.json');
  const state = new PersistentInteractionState({ filePath });
  const effects = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (_jid, content) => effects.push(content),
    routeInteraction: details => state.register(details),
    getResponseMessage: (_language, key) => ({ text: key, audio: '' }),
    readAudio: async () => null,
    detectLanguage: detectLanguageEvidence,
    logger: { info() {}, warn() {}, error() {} },
  });
  const parent = incoming('album-transitive', {
    albumMessage: { expectedImageCount: 2 },
  }, {
    remoteJid: '123456789@lid',
  });
  const bridge = incoming('album-bridge', {
    imageMessage: {},
    messageContextInfo: {
      messageAssociation: { parentMessageKey: { id: 'album-transitive' } },
    },
  }, {
    remoteJid: '573001234567@s.whatsapp.net',
    remoteJidAlt: '123456789@lid',
  });
  const caption = incoming('album-caption', {
    imageMessage: { caption: 'Are you available now?' },
    messageContextInfo: {
      messageAssociation: { parentMessageKey: { id: 'album-transitive' } },
    },
  }, {
    remoteJid: '573001234567@s.whatsapp.net',
  });

  const results = await handler({
    type: 'notify',
    messages: [parent, bridge, caption],
  });

  assert.deepEqual(results.map(result => result.reason || result.response), [
    'album_placeholder',
    'duplicate',
    'step1',
  ]);
  assert.deepEqual(effects, [{ text: 'step1' }]);
  const contact = Object.values(JSON.parse(fs.readFileSync(filePath, 'utf8')).contacts)[0];
  assert.equal(contact.language, 'en');
  assert.equal(contact.language_provisional, false);
});

test('ids iguales de contactos distintos no se fusionan dentro del lote', async () => {
  const { handler, effects } = createHarness();
  const results = await handler({
    type: 'notify',
    messages: [
      incoming('shared-id', { conversation: 'hola' }),
      incoming('shared-id', { conversation: 'hola' }, {
        remoteJid: '573009999999@s.whatsapp.net',
      }),
    ],
  });

  assert.deepEqual(results.map(result => result.response), ['step1', 'step1']);
  assert.equal(effects.length, 4);
});

test('la resolución LID lenta no invierte Paso 1 y Paso 2', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-order-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  const claimQueue = new KeyedSerialQueue();
  const deliveryQueue = new KeyedSerialQueue();
  const effects = [];
  const phoneJid = '573001234567@s.whatsapp.net';
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (_jid, content) => effects.push(content.text),
    routeInteraction: details => state.register(details),
    getResponseMessage: (_lang, key) => ({ text: key, audio: '' }),
    readAudio: async () => null,
    detectLanguage: () => 'es',
    resolvePnForLid: async () => {
      await new Promise(resolve => setTimeout(resolve, 20));
      return phoneJid;
    },
    serializeClaim: operation => claimQueue.run('all-inbound', operation),
    serializeInteraction: (contactId, operation) => deliveryQueue.run(contactId, operation),
    logger: { info() {}, warn() {}, error() {} },
  });
  const first = incoming('first', { conversation: 'hola' }, { remoteJid: '123@lid' });
  const second = incoming('second', { conversation: 'hola' }, { remoteJid: phoneJid });

  const results = await handler({ type: 'notify', messages: [first, second] });

  assert.deepEqual(results.map(result => result.response), ['step1', 'step2']);
  assert.deepEqual(effects, ['step1', 'step2']);
});

test('procesa append reciente pero ignora historial antiguo', async () => {
  const { handler, effects } = createHarness();
  const currentSeconds = Math.floor(Date.now() / 1000);

  const recent = incoming('recent', { conversation: 'hola' });
  recent.messageTimestamp = currentSeconds;
  const old = incoming('old', { conversation: 'hola' });
  old.messageTimestamp = currentSeconds - 3600;
  const recentResult = await handler({ type: 'append', messages: [recent] });
  const oldResult = await handler({ type: 'append', messages: [old] });

  assert.equal(recentResult[0].response, 'step1');
  assert.equal(oldResult[0].reason, 'historical_append');
  assert.equal(effects.length, 2);
});

test('un contacto bloqueado no detiene otro del mismo lote', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-handler-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  const queue = new KeyedSerialQueue();
  const completed = [];
  let releaseFirst;
  const firstGate = new Promise(resolve => { releaseFirst = resolve; });
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (jid) => {
      if (jid.startsWith('573001')) await firstGate;
      completed.push(jid);
    },
    routeInteraction: details => state.register(details),
    getResponseMessage: () => ({ text: 'x', audio: '' }),
    readAudio: async () => null,
    detectLanguage: () => 'es',
    serializeInteraction: (contactId, operation) => queue.run(contactId, operation),
    logger: { info() {}, warn() {}, error() {} },
  });
  const first = incoming('1', { conversation: 'hola' });
  const second = incoming('2', { conversation: 'hola' }, {
    remoteJid: '573009999999@s.whatsapp.net',
  });

  const pending = handler({ type: 'notify', messages: [first, second] });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(completed, ['573009999999@s.whatsapp.net']);
  releaseFirst();
  await pending;
});

test('un envío sin respuesta vence y no bloquea al contacto para siempre', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-timeout-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  const queue = new KeyedSerialQueue();
  let sendCount = 0;
  const handler = createWhatsAppMessageHandler({
    sendMessage: async () => {
      sendCount += 1;
      if (sendCount === 1) return new Promise(() => {});
      return undefined;
    },
    routeInteraction: details => state.register(details),
    getResponseMessage: (_lang, key) => ({ text: key, audio: '' }),
    readAudio: async () => null,
    detectLanguage: () => 'es',
    serializeInteraction: (contactId, operation) => queue.run(contactId, operation),
    sendTimeoutMs: 15,
    logger: { info() {}, warn() {}, error() {} },
  });

  const first = handler({
    type: 'notify',
    messages: [incoming('timeout-1', { conversation: 'hola' })],
  });
  const second = handler({
    type: 'notify',
    messages: [incoming('timeout-2', { conversation: 'hola' })],
  });
  const [firstResult, secondResult] = await Promise.all([first, second]);

  assert.equal(firstResult[0].text, 'uncertain');
  assert.equal(firstResult[0].status, 'failed');
  assert.equal(firstResult[0].delivery, 'uncertain');
  assert.equal(secondResult[0].text, 'sent');
  assert.equal(secondResult[0].response, 'step2');
});

test('an operational pause does not consume a step or mark the message read', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-paused-'));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  let allowed = false;
  let reads = 0;
  const effects = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (_jid, content) => effects.push(content.text),
    markRead: async () => { reads += 1; },
    deliveryAllowed: () => allowed,
    routeInteraction: details => state.register(details),
    getResponseMessage: (_lang, key) => ({ text: key, audio: '' }),
    readAudio: async () => null,
    detectLanguage: () => 'es',
    logger: { info() {}, warn() {}, error() {} },
  });

  const blocked = await handler({
    type: 'notify',
    messages: [incoming('paused', { conversation: 'hola' })],
  });
  allowed = true;
  const resumed = await handler({
    type: 'notify',
    messages: [incoming('resumed', { conversation: 'hola' })],
  });

  assert.equal(blocked[0].reason, 'delivery_blocked');
  assert.equal(resumed[0].response, 'step1');
  assert.deepEqual(effects, ['step1']);
  assert.equal(reads, 1);
});

test('entrega video, texto y audio en un paso cuando el video está configurado', async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-message-video-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const state = new PersistentInteractionState({ filePath: path.join(directory, 'state.json') });
  const effects = [];
  const handler = createWhatsAppMessageHandler({
    sendMessage: async (jid, content) => effects.push([jid, content]),
    routeInteraction: details => state.register(details),
    getResponseMessage: () => ({ text: 'Hola', audio: 'step1.mp3', video: 'step1.mp4' }),
    readAudio: async () => Buffer.from('audio-bytes'),
    readVideo: async () => ({ buffer: Buffer.from('video-bytes'), mimetype: 'video/mp4' }),
    detectLanguage: () => 'es',
    logger: { info() {}, warn() {}, error() {} },
  });

  const results = await handler({
    type: 'notify',
    messages: [incoming('video-msg-1', { conversation: 'hola' })],
  });

  assert.equal(results[0].text, 'sent');
  assert.equal(results[0].audio, 'sent');
  assert.equal(results[0].video, 'sent');
  assert.equal(results[0].delivery, 'complete');
  assert.equal(effects.length, 3);
  assert.deepEqual(effects[0][1], {
    video: Buffer.from('video-bytes'),
    mimetype: 'video/mp4',
  });
  assert.deepEqual(effects[1][1], { text: 'Hola' });
  assert.ok(effects[2][1].audio);
});

function videoFirstHarness(t, overrides = {}) {
  const temporaryRoot = fs.realpathSync(os.tmpdir());
  const directory = fs.mkdtempSync(path.join(temporaryRoot, 'wa-message-video-first-'));
  t.after(() => {
    const target = fs.realpathSync(directory);
    assert.equal(path.dirname(target), temporaryRoot);
    fs.rmSync(target, { recursive: true, force: true });
  });
  const statePath = path.join(directory, 'state.json');
  const effects = [];
  const create = () => {
    const state = new PersistentInteractionState({ filePath: statePath });
    return createWhatsAppMessageHandler({
      sendMessage: async (_jid, content) => effects.push(content),
      routeInteraction: details => state.register(details),
      getResponseMessage: (_language, responseKey) => ({ text: responseKey, audio: 'audio.mp3', video: 'video.mp4' }),
      readAudio: async () => Buffer.from('audio'),
      readVideo: async () => ({ buffer: Buffer.from('video'), mimetype: 'video/mp4' }),
      detectLanguage: () => 'es',
      sendTimeoutMs: 100,
      logger: { info() {}, warn() {}, error() {} },
      ...overrides,
    });
  };
  const batch = id => ({ type: 'notify', messages: [incoming(id, { conversation: 'hola' })] });
  return { handler: create(), create, effects, batch };
}

test('both steps wait for video acknowledgement before text and audio', async t => {
  const effects = [];
  let releaseVideo;
  let signalStarted;
  const started = new Promise(resolve => { signalStarted = resolve; });
  const gate = new Promise(resolve => { releaseVideo = resolve; });
  const files = videoFirstHarness(t, {
    sendMessage: async (_jid, content) => {
      effects.push(content.video ? 'video' : content.text ? 'text' : 'audio');
      if (content.video) {
        signalStarted();
        await gate;
      }
    },
  });
  const first = files.handler(files.batch('video-step1'));
  await started;
  assert.deepEqual(effects, ['video']);
  releaseVideo();
  assert.equal((await first)[0].response, 'step1');
  assert.deepEqual(effects, ['video', 'text', 'audio']);
  const second = await files.handler(files.batch('video-step2'));
  assert.equal(second[0].response, 'step2');
  assert.equal(second[0].delivery, 'complete');
  assert.deepEqual(effects, ['video', 'text', 'audio', 'video', 'text', 'audio']);
});

test('video with explicitly empty audio skips its reader and sends video then text', async t => {
  const files = videoFirstHarness(t, {
    getResponseMessage: () => ({ text: 'text', audio: '', video: 'video.mp4' }),
    readAudio: async () => { throw new Error('empty audio must not be read'); },
  });
  const [result] = await files.handler(files.batch('video-without-audio'));
  assert.equal(result.audio, 'skipped');
  assert.equal(result.delivery, 'complete');
  assert.deepEqual(files.effects.map(content => content.video ? 'video' : 'text'), ['video', 'text']);
});

test('steps without video retain text then audio and never read video', async t => {
  const files = videoFirstHarness(t, {
    getResponseMessage: () => ({ text: 'text', audio: 'audio.mp3', video: '' }),
    readVideo: async () => { throw new Error('unconfigured video must not be read'); },
  });
  const [result] = await files.handler(files.batch('without-video'));
  assert.equal(result.video, 'skipped');
  assert.equal(result.delivery, 'complete');
  assert.deepEqual(files.effects.map(content => content.text ? 'text' : 'audio'), ['text', 'audio']);
});

test('missing video with no other configured components reports failed delivery', async t => {
  const files = videoFirstHarness(t, {
    getResponseMessage: () => ({ text: '', audio: '', video: 'video.mp4' }),
    readVideo: async () => null,
  });
  const [result] = await files.handler(files.batch('video-only-missing'));
  assert.equal(result.status, 'failed');
  assert.equal(result.delivery, 'failed');
  assert.equal(result.reason, 'delivery_incomplete');
  assert.deepEqual([result.text, result.audio, result.video], ['skipped', 'skipped', 'missing']);
  assert.deepEqual(files.effects, []);
});

for (const failure of ['missing', 'read_error', 'send_error', 'timeout']) {
  test(`message video ${failure} preserves fallback order, truthful status and persistent deduplication`, async t => {
    const effects = [];
    let releaseLateVideo;
    const lateVideo = new Promise(resolve => { releaseLateVideo = resolve; });
    const files = videoFirstHarness(t, {
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
    const [result] = await files.handler(files.batch(`video-${failure}`));
    const uncertain = ['send_error', 'timeout'].includes(failure);
    assert.equal(result.video, uncertain ? 'uncertain' : failure === 'missing' ? 'missing' : 'failed');
    assert.equal(result.status, 'failed');
    assert.equal(result.delivery, uncertain ? 'uncertain' : 'partial');
    assert.equal(result.text, 'sent');
    assert.equal(result.audio, 'sent');
    assert.deepEqual(effects, uncertain ? ['read_video', 'video', 'text', 'audio'] : ['read_video', 'text', 'audio']);
    releaseLateVideo();
    await new Promise(resolve => setImmediate(resolve));
    const beforeDuplicate = [...effects];
    const [duplicate] = await files.create()(files.batch(`video-${failure}`));
    assert.equal(duplicate.reason, 'duplicate');
    assert.deepEqual(effects, beforeDuplicate);
    assert.equal(result.delivery, uncertain ? 'uncertain' : 'partial');
  });
}
