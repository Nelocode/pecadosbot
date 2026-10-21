/**
 * WhatsApp Bot — AutoReply Comercial
 * Usa Baileys (protocolo WhatsApp Web, sin API de Meta)
 * Misma lógica de messages.json, detección de idioma, y estado
 */
import makeWASocket, { useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import { fileURLToPath } from 'url';
import { Boom } from '@hapi/boom';
import QRCode from 'qrcode';
import { createWhatsAppCallHandler } from './wa_call_handler.mjs';
import { createWhatsAppCallHealth } from './wa_call_health.mjs';
import { PersistentInteractionState } from './interaction_state.mjs';
import { createWhatsAppMessageHandler } from './wa_message_handler.mjs';
import { KeyedSerialQueue } from './keyed_serial_queue.mjs';
import { classifyWhatsAppDisconnect } from './wa_disconnect_policy.mjs';
import { createWhatsAppVoiceNoteReader, createWhatsAppVideoReader } from './wa_audio_delivery.mjs';
import { pollWhatsAppProfilePicture, whatsappProfileAccountHash } from './wa_profile_picture.mjs';
import { createWhatsAppSafetyHealth } from './wa_safety_health.mjs';
import { createWhatsAppDeliveryTiming } from './wa_delivery_timing.mjs';
import {
  createWhatsAppDeliverySafety,
  readWhatsAppSafetyConfig,
  recordWhatsAppProviderSignal,
} from './wa_delivery_safety.mjs';
import { detectLanguageEvidence, reloadLanguageRules } from './language_detection.mjs';
import { resolveWhatsAppInstancePaths } from './wa_instance_paths.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const instancePaths = resolveWhatsAppInstancePaths(__dirname);
const BASE_DIR = instancePaths.baseDir;
const DATA_DIR = instancePaths.dataDir;
const AUDIO_DIR = instancePaths.audioDir;
const VIDEO_DIR = instancePaths.videoDir;
const MESSAGES_FILE = instancePaths.messagesFile;
const AUTH_DIR = instancePaths.authDir;
const QR_PATH = instancePaths.qrPath;
const CALL_HEALTH_FILE = instancePaths.callHealthFile;
const SAFETY_HEALTH_FILE = instancePaths.safetyHealthFile;
const SAFETY_CONTROL_FILE = instancePaths.safetyControlFile;
const INTERACTION_STATE_FILE = instancePaths.interactionStateFile;
const VOICE_NOTE_CACHE_DIR = instancePaths.voiceNoteCacheDir;
const OFFICIAL_PROFILE_PICTURE_PATH = instancePaths.officialImage;
const PROFILE_PICTURE_PATH = instancePaths.profileImage;
const PROFILE_PICTURE_STATE_FILE = instancePaths.profileStateFile;
const IDENTITY_FILE = instancePaths.identityFile;
const LINK_ONLY = process.env.WA_LINK_ONLY === '1';
const VOICE_NOTES_ENABLED = process.env.WA_VOICE_NOTES_ENABLED !== '0';
const PROFILE_PICTURE_ENABLED = process.env.WA_PROFILE_PICTURE_ENABLED !== '0';
const configuredDefaultLanguage = String(process.env.AUTOREPLY_DEFAULT_LANG || 'es').trim().toLowerCase();
const DEFAULT_LANGUAGE = /^[a-z]{2,5}$/.test(configuredDefaultLanguage)
  ? configuredDefaultLanguage
  : 'es';
const callHealth = createWhatsAppCallHealth({ filePath: CALL_HEALTH_FILE, logger: console });
const safetyConfig = readWhatsAppSafetyConfig();
const deliveryTiming = LINK_ONLY ? null : createWhatsAppDeliveryTiming({
  filePath: path.join(DATA_DIR, 'wa_delivery_timing.json'),
  logger: console,
});
const safetyHealth = LINK_ONLY ? null : createWhatsAppSafetyHealth({
  filePath: SAFETY_HEALTH_FILE,
  controlFilePath: SAFETY_CONTROL_FILE,
  logger: console,
});
const interactionState = LINK_ONLY ? null : new PersistentInteractionState({
  filePath: INTERACTION_STATE_FILE,
  defaultLanguage: DEFAULT_LANGUAGE,
  logger: console,
});
// El reclamo global conserva el orden exacto de llegada antes de cualquier
// resolución LID/PN. La entrega sigue aislada por contacto.
const interactionClaimQueue = new KeyedSerialQueue();
const deliveryQueue = new KeyedSerialQueue();
const CONNECTION_TIMEOUT_MS = 30_000;
const SAFETY_HEARTBEAT_MS = 30_000;
const SAFETY_HANDLER_SEND_TIMEOUT_MS = Math.max(
  30_000,
  safetyConfig.queueWaitTimeoutMs
    + safetyConfig.sendTimeoutMs
    + Math.max(safetyConfig.textDelayMaxMs, safetyConfig.audioDelayMaxMs)
    + safetyConfig.minimumSendIntervalMs
    + 5_000,
);
const parsedLinkTimeoutMs = Number.parseInt(process.env.WA_LINK_TIMEOUT_MS || '180000', 10);
const LINK_TIMEOUT_MS = Number.isFinite(parsedLinkTimeoutMs)
  ? Math.min(Math.max(parsedLinkTimeoutMs, 30_000), 600_000)
  : 180_000;

let activeSocket = null;
let reconnectTimer = null;
let reconnectAttempts = 0;
let reconnectStabilityTimer = null;
let shuttingDown = false;
let linkExpiryTimer = null;
let activeDeliverySafety = null;
let safetyHeartbeatTimer = null;
let activeProfilePicturePoller = null;

if (safetyHealth) {
  safetyHeartbeatTimer = setInterval(() => safetyHealth.touch(), SAFETY_HEARTBEAT_MS);
  if (typeof safetyHeartbeatTimer.unref === 'function') safetyHeartbeatTimer.unref();
}

if (LINK_ONLY) {
  linkExpiryTimer = setTimeout(() => shutdown('link-timeout'), LINK_TIMEOUT_MS);
}

function writeIdentity(user) {
  const rawId = typeof user?.id === 'string' ? user.id.split('@')[0].split(':')[0] : '';
  const digits = rawId.replace(/\D/g, '');
  const displayName = typeof user?.name === 'string' && user.name.trim()
    ? user.name.trim().slice(0, 120)
    : 'Cuenta de WhatsApp';
  const payload = {
    display_name: displayName,
    account_hash: whatsappProfileAccountHash(user?.id),
    phone_hint: digits.length >= 4 ? `••••${digits.slice(-4)}` : null,
    updated_at: Date.now(),
  };
  const temporaryPath = `${IDENTITY_FILE}.${process.pid}.tmp`;
  fs.mkdirSync(path.dirname(IDENTITY_FILE), { recursive: true });
  fs.writeFileSync(temporaryPath, JSON.stringify(payload), { encoding: 'utf8', mode: 0o600 });
  try {
    fs.renameSync(temporaryPath, IDENTITY_FILE);
  } catch (error) {
    // Windows no siempre permite reemplazar el destino con renameSync.
    if (error?.code !== 'EEXIST' && error?.code !== 'EPERM') throw error;
    fs.rmSync(IDENTITY_FILE, { force: true });
    fs.renameSync(temporaryPath, IDENTITY_FILE);
  }
}

function recordProviderRestriction(statusCode) {
  if (!safetyHealth) return;
  recordWhatsAppProviderSignal({
    statusCode,
    health: safetyHealth,
    config: safetyConfig,
    attempt: Math.max(1, reconnectAttempts + 1),
  });
}

function scheduleReconnect(statusCode = null) {
  if (shuttingDown || reconnectTimer) return;
  reconnectAttempts += 1;
  safetyHealth?.record('reconnect');
  const exponentialDelay = Math.min(
    safetyConfig.reconnectBaseMs * (2 ** Math.max(0, reconnectAttempts - 1)),
    safetyConfig.reconnectMaxMs,
  );
  const normalizedStatus = Number.parseInt(String(statusCode ?? ''), 10);
  const providerDelay = normalizedStatus === 429 ? safetyConfig.backoffBaseMs : 0;
  const activeBackoffDelay = Math.max(
    0,
    (safetyHealth?.refresh().backoff_until || 0) - Date.now(),
  );
  const reconnectDelay = Math.max(
    providerDelay,
    activeBackoffDelay,
    Math.round(exponentialDelay * (0.9 + Math.random() * 0.2)),
  );
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (shuttingDown) return;
    startBot().catch(() => {
      console.error('[WA] Reconnection setup failed');
      scheduleReconnect();
    });
  }, reconnectDelay);
}

function terminateInvalidSession() {
  // `fs.watchFile` otherwise keeps Node alive after Baileys has declared the
  // credentials unusable. Preserve the health snapshot so the panel can
  // explain that a new QR is required.
  shuttingDown = true;
  activeProfilePicturePoller?.stop();
  activeProfilePicturePoller = null;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (reconnectStabilityTimer) clearTimeout(reconnectStabilityTimer);
  reconnectStabilityTimer = null;
  if (linkExpiryTimer) clearTimeout(linkExpiryTimer);
  linkExpiryTimer = null;
  if (safetyHeartbeatTimer) clearInterval(safetyHeartbeatTimer);
  safetyHeartbeatTimer = null;
  activeDeliverySafety?.cancelAll('invalid_session');
  activeDeliverySafety = null;
  fs.unwatchFile(MESSAGES_FILE);
  setImmediate(() => process.exit(0));
}

// ── Cargar mensajes ──
function loadMessages() {
  const raw = fs.readFileSync(MESSAGES_FILE, 'utf-8');
  const data = JSON.parse(raw);
  const result = {};
  for (const [lang, langData] of Object.entries(data)) {
    if (!Array.isArray(langData.steps) || langData.steps.length < 2) {
      throw new Error(`[WA] El idioma ${lang} necesita Paso 1 y Paso 2`);
    }
    const callRaw = langData.call || {};
    result[lang] = {
      steps: langData.steps.map(s => ({
        text: s.text,
        audio: s.audio,
        video: s.video || '',
        loop: s.loop || false,
      })),
      call: {
        text: callRaw.text ?? '📞 Llamada recibida',
        audio: callRaw.audio ?? '',
        video: callRaw.video ?? '',
      },
    };
  }
  return result;
}

// El proceso temporal que genera el QR no entrega respuestas. Mantenerlo
// independiente de messages.json evita que una configuración ausente o en
// pleno guardado impida volver a vincular WhatsApp.
let MESSAGES = LINK_ONLY ? {} : loadMessages();

// ── Watch messages.json para recargar en caliente (cuando admin panel guarda) ──
if (!LINK_ONLY) {
  fs.watchFile(MESSAGES_FILE, () => {
    try {
      MESSAGES = loadMessages();
      reloadLanguageRules();
      console.log(`[WA] messages.json recargado — ${Object.keys(MESSAGES).length} idiomas`);
    } catch (e) {
      console.error('[WA] Error recargando messages.json:', e.message);
    }
  });
}

function detectLang(text) {
  return detectLanguageEvidence(text);
}

// ── Estado por usuario ──
// ── Obtener mensaje para un paso ──
function getMessage(lang, step) {
  const data = MESSAGES[lang] || MESSAGES['en'];
  const idx = Math.min(step, data.steps.length - 1);
  return data.steps[idx];
}

function getCallMessage(lang) {
  const data = MESSAGES[lang] || MESSAGES['en'];
  return data.call || { text: '📞 Llamada recibida', audio: '' };
}

function getResponseMessage(lang, responseKey) {
  if (responseKey === 'call') return getCallMessage(lang);
  return getMessage(lang, responseKey === 'step1' ? 0 : 1);
}

// ── Leer archivo de audio/video como buffer ──
const readAudio = createWhatsAppVoiceNoteReader({
  audioDir: AUDIO_DIR,
  cacheDir: VOICE_NOTE_CACHE_DIR,
  ffmpegPath: process.env.FFMPEG_PATH || 'ffmpeg',
  enabled: VOICE_NOTES_ENABLED,
  logger: console,
});
const readVideo = createWhatsAppVideoReader({
  videoDir: VIDEO_DIR,
  logger: console,
});

// ── Iniciar conexión WhatsApp ──
async function startBot() {
  // Crear directorio de autenticación si no existe
  if (!fs.existsSync(AUTH_DIR)) {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
  }

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  callHealth.record({ type: 'connection', state: 'connecting' });

  const sock = makeWASocket({
    auth: state,
    syncFullHistory: false,
    markOnlineOnConnect: true,
    browser: ['AutoReply Bot', 'Chrome', '120.0'],
  });
  activeSocket = sock;
  activeProfilePicturePoller?.stop();
  activeProfilePicturePoller = null;
  if (reconnectStabilityTimer) clearTimeout(reconnectStabilityTimer);
  reconnectStabilityTimer = null;
  activeDeliverySafety?.cancelAll('socket_replaced');
  const deliverySafety = LINK_ONLY ? null : createWhatsAppDeliverySafety({
    sendMessage: (jid, content) => sock.sendMessage(jid, content),
    sendPresenceUpdate: (presence, jid) => sock.sendPresenceUpdate(presence, jid),
    readMessages: keys => sock.readMessages(keys),
    health: safetyHealth,
    timing: deliveryTiming,
    config: safetyConfig,
    logger: console,
  });
  activeDeliverySafety = deliverySafety;
  const connectionWatchdog = state.creds?.registered
    ? setTimeout(() => {
        if (activeSocket !== sock || shuttingDown) return;
        console.warn('[WA] Connection timeout; retrying with the saved session');
        activeSocket = null;
        deliverySafety?.cancelAll('connection_timeout');
        if (activeDeliverySafety === deliverySafety) activeDeliverySafety = null;
        callHealth.record({ type: 'connection', state: 'closed', reason: 'timeout' });
        try {
          sock.end(new Error('connection-timeout'));
        } catch {
          // The reconnect scheduler below remains authoritative.
        }
        scheduleReconnect();
      }, CONNECTION_TIMEOUT_MS)
    : null;

  if (!LINK_ONLY) {
    // Observe only that a raw call stanza arrived. Never persist its payload.
    if (typeof sock.ws?.on === 'function') {
      sock.ws.on('CB:call', () => callHealth.record({ type: 'raw_call' }));
      callHealth.record({ type: 'raw_listener', state: 'registered' });
    } else {
      callHealth.record({ type: 'raw_listener', state: 'unavailable' });
    }

    // Register immediately: call events are not buffered by Baileys.
    const handleCallBatch = createWhatsAppCallHandler({
      rejectCall: (callId, callFrom) => sock.rejectCall(callId, callFrom),
      sendMessage: (jid, content, metadata) => deliverySafety.send(jid, content, metadata),
      deliveryAllowed: () => deliverySafety.canDeliver(),
      getCallMessage,
      getResponseMessage,
      routeInteraction: details => interactionState.register(details),
      resolveContactId: async (jid) => {
        if (!jid.endsWith('@lid') && !jid.endsWith('@hosted.lid')) return jid;
        return sock.signalRepository?.lidMapping?.getPNForLID
          ? (await sock.signalRepository.lidMapping.getPNForLID(jid)) || jid
          : jid;
      },
      serializeClaim: operation => interactionClaimQueue.run('all-inbound', operation),
      serializeInteraction: (contactId, operation) => deliveryQueue.run(contactId, operation),
      readAudio,
      readVideo,
      logger: console,
      onCallMetric: callHealth.record,
      sendTimeoutMs: SAFETY_HANDLER_SEND_TIMEOUT_MS,
    });
    sock.ev.on('call', handleCallBatch);
    callHealth.record({ type: 'listener_registered' });
  }

  // ── Guardar credenciales cuando se actualicen ──
  sock.ev.on('creds.update', saveCreds);

  // ── Manejar conexión / reconexión ──
  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Guardar QR como imagen PNG para el panel web
      fs.mkdirSync(path.dirname(QR_PATH), { recursive: true });
      QRCode.toFile(QR_PATH, qr, { type: 'png', width: 400, margin: 2 }, (err) => {
        if (err) console.error('[WA] Error guardando QR:', err.message);
        else console.log(`[WA] QR guardado en ${QR_PATH}`);
      });
      console.log('\n╔══════════════════════════════════════════════╗');
      console.log('║      ESCANEA EL QR EN EL PANEL ADMIN      ║');
      console.log('║   http://localhost:5000                    ║');
      console.log('╚══════════════════════════════════════════════╝\n');
    }

    if (connection === 'close') {
      if (activeSocket !== sock) return;
      activeProfilePicturePoller?.stop();
      activeProfilePicturePoller = null;
      if (connectionWatchdog) clearTimeout(connectionWatchdog);
      if (reconnectStabilityTimer) clearTimeout(reconnectStabilityTimer);
      reconnectStabilityTimer = null;
      activeSocket = null;
      deliverySafety?.cancelAll('connection_closed');
      if (activeDeliverySafety === deliverySafety) activeDeliverySafety = null;
      const disconnectStatus = lastDisconnect?.error instanceof Boom
        ? lastDisconnect.error.output?.statusCode
        : (
          lastDisconnect?.error?.output?.statusCode
          ?? lastDisconnect?.error?.data?.statusCode
          ?? lastDisconnect?.error?.response?.status
          ?? lastDisconnect?.error?.statusCode
          ?? lastDisconnect?.error?.status
        );
      recordProviderRestriction(disconnectStatus);
      const disconnect = classifyWhatsAppDisconnect(disconnectStatus);
      callHealth.record({
        type: 'connection',
        state: 'closed',
        reason: disconnect.reason,
        reauthRequired: disconnect.reauthRequired,
      });

      console.log(`[WA] Conexión cerrada. Estado: ${disconnectStatus ?? 'unknown'}. Reconnect: ${disconnect.shouldReconnect}`);

      if (disconnect.terminateWorker) {
        console.log('[WA] Sesión inválida. Vuelve a vincularla desde el panel.');
        terminateInvalidSession();
      } else {
        scheduleReconnect(disconnectStatus);
      }
    }

    if (connection === 'open') {
      if (activeSocket !== sock) return;
      if (connectionWatchdog) clearTimeout(connectionWatchdog);
      if (reconnectStabilityTimer) clearTimeout(reconnectStabilityTimer);
      reconnectStabilityTimer = setTimeout(() => {
        if (activeSocket === sock && !shuttingDown) reconnectAttempts = 0;
        reconnectStabilityTimer = null;
      }, safetyConfig.reconnectStableMs);
      if (typeof reconnectStabilityTimer.unref === 'function') reconnectStabilityTimer.unref();
      callHealth.record({ type: 'connection', state: 'open' });
      try {
        fs.rmSync(QR_PATH, { force: true });
        writeIdentity(sock.user);
      } catch {
        console.warn('[WA] No fue posible actualizar la identidad mostrable');
      }
      if (!LINK_ONLY && PROFILE_PICTURE_ENABLED) {
        activeProfilePicturePoller?.stop();
        activeProfilePicturePoller = pollWhatsAppProfilePicture({
          isActive: () => activeSocket === sock && !shuttingDown,
          getOptions: () => {
            let expectedImageHash = null;
            try {
              expectedImageHash = crypto.createHash('sha256')
                .update(fs.readFileSync(OFFICIAL_PROFILE_PICTURE_PATH)).digest('hex');
            } catch { /* The helper records a missing image without sending. */ }
            return {
              jid: sock.user?.id,
              updateProfilePicture: (jid, image, dimensions) => {
                if (activeSocket !== sock || shuttingDown) throw new Error('socket_inactive');
                return sock.updateProfilePicture(jid, image, dimensions);
              },
              imagePath: PROFILE_PICTURE_PATH,
              expectedImageHash,
              statePath: PROFILE_PICTURE_STATE_FILE,
              requestPath: instancePaths.profileRequestFile,
              logger: console,
            };
          },
        });
      }
      console.log('[WA] Connected');
    }
  });

  if (!LINK_ONLY) {
    // Un único manejador cuenta texto y cualquier multimedia en el mismo estado
    // que las llamadas. Los eventos de sincronización y control se descartan.
    const handleMessageBatch = createWhatsAppMessageHandler({
      sendMessage: (jid, content, metadata) => deliverySafety.send(jid, content, metadata),
      markRead: messageKey => deliverySafety.markRead(messageKey),
      deliveryAllowed: () => deliverySafety.canDeliver(),
      routeInteraction: details => interactionState.register(details),
      getResponseMessage,
      readAudio,
      readVideo,
      detectLanguage: detectLang,
      resolvePnForLid: async (lid) => (
        sock.signalRepository?.lidMapping?.getPNForLID
          ? sock.signalRepository.lidMapping.getPNForLID(lid)
          : null
      ),
      serializeClaim: operation => interactionClaimQueue.run('all-inbound', operation),
      serializeInteraction: (contactId, operation) => deliveryQueue.run(contactId, operation),
      logger: console,
      sendTimeoutMs: SAFETY_HANDLER_SEND_TIMEOUT_MS,
    });
    sock.ev.on('messages.upsert', handleMessageBatch);
  }

}

// ── Main ──
console.log(`🚀 WhatsApp Bot AutoReply iniciando${LINK_ONLY ? ' (sólo vinculación)' : ''}...`);
console.log(`📁 Directorio: ${BASE_DIR}`);
console.log(`📁 Auth: ${AUTH_DIR}`);
console.log(`🔑 Escanea el QR con tu WhatsApp`);
console.log('────────────────────────────────────────\n');

function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  activeProfilePicturePoller?.stop();
  activeProfilePicturePoller = null;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (reconnectStabilityTimer) clearTimeout(reconnectStabilityTimer);
  reconnectStabilityTimer = null;
  if (linkExpiryTimer) clearTimeout(linkExpiryTimer);
  linkExpiryTimer = null;
  if (safetyHeartbeatTimer) clearInterval(safetyHeartbeatTimer);
  safetyHeartbeatTimer = null;
  callHealth.record({ type: 'connection', state: 'closed', reason: 'shutdown' });
  activeDeliverySafety?.cancelAll('worker_shutdown');
  activeDeliverySafety = null;
  console.log(`[WA] Shutting down (${signal})`);
  try {
    activeSocket?.end(new Error(signal));
  } catch {
    // Process shutdown must continue even if the socket is already closed.
  }
  process.exit(0);
}

process.on('unhandledRejection', (reason) => {
  console.error('[WA] Unhandled Rejection:', reason);
});

process.on('uncaughtException', (error) => {
  console.error('[WA] Uncaught Exception:', error);
});

process.once('SIGINT', () => shutdown('SIGINT'));
process.once('SIGTERM', () => shutdown('SIGTERM'));

startBot().catch(() => {
  console.error('[WA] Initial connection setup failed');
  scheduleReconnect();
});

