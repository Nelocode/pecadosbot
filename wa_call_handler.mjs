import { jidNormalizedUser } from '@whiskeysockets/baileys';
import { settleWithTimeout } from './keyed_serial_queue.mjs';
import { toWhatsAppAudioContent, toWhatsAppVideoContent } from './wa_audio_delivery.mjs';
import { provisionalLanguageFromWhatsAppIdentity } from './whatsapp_language_hint.mjs';

const DEFAULT_DEDUPE_TTL_MS = 60 * 60 * 1000;
const DEFAULT_REJECT_TIMEOUT_MS = 5_000;
const DEFAULT_CONTACT_RESOLUTION_TIMEOUT_MS = 5_000;
const DEFAULT_SEND_TIMEOUT_MS = 20_000;
const SAFE_EVENT_STATUSES = new Set([
  'offer', 'ringing', 'preaccept', 'transport', 'relaylatency', 'terminate',
  'timeout', 'reject', 'accept', 'other', 'missing',
]);

function normalizeCallBatch(payload) {
  if (Array.isArray(payload)) return payload;
  return payload && typeof payload === 'object' ? [payload] : [];
}

function jidKind(jid) {
  if (typeof jid !== 'string' || !jid) return 'missing';
  if (jid.endsWith('@s.whatsapp.net') || jid.endsWith('@hosted')) return 'pn';
  if (jid.endsWith('@lid') || jid.endsWith('@hosted.lid')) return 'lid';
  return 'other';
}

function normalizeJid(jid) {
  if (typeof jid !== 'string' || !jid) return '';
  try {
    return jidNormalizedUser(jid);
  } catch {
    return jid;
  }
}

function selectReplyTarget(call) {
  const candidates = [
    ['caller_pn', normalizeJid(call?.callerPn)],
    ['chat_id', normalizeJid(call?.chatId)],
    ['from', normalizeJid(call?.from)],
  ];
  for (const [source, jid] of candidates) {
    if (jidKind(jid) === 'pn') return { jid, source, kind: 'pn' };
  }
  for (const [source, jid] of candidates) {
    if (typeof jid === 'string' && jid) return { jid, source, kind: jidKind(jid) };
  }
  return { jid: '', source: 'missing', kind: 'missing' };
}

function pruneHandledCalls(handledCalls, now, ttlMs) {
  for (const [key, handledAt] of handledCalls) {
    if (now - handledAt >= ttlMs) handledCalls.delete(key);
  }
}

/**
 * Crea el handler del evento `call` de Baileys.
 *
 * Baileys entrega WACallEvent[], no un único objeto. Este handler también
 * tolera un objeto individual para facilitar pruebas y compatibilidad.
 */
export function createWhatsAppCallHandler({
  rejectCall,
  sendMessage,
  getCallMessage,
  getResponseMessage = null,
  routeInteraction = null,
  resolveContactId = async (jid) => jid,
  deliveryAllowed = () => true,
  serializeClaim = async operation => operation(),
  serializeInteraction = async (_contactId, operation) => operation(),
  readAudio,
  readVideo = async () => null,
  getLanguage = () => 'en',
  logger = console,
  now = () => Date.now(),
  dedupeTtlMs = DEFAULT_DEDUPE_TTL_MS,
  rejectTimeoutMs = DEFAULT_REJECT_TIMEOUT_MS,
  contactResolutionTimeoutMs = DEFAULT_CONTACT_RESOLUTION_TIMEOUT_MS,
  sendTimeoutMs = DEFAULT_SEND_TIMEOUT_MS,
  handledCalls = new Map(),
  onCallMetric = () => {},
}) {
  if (typeof rejectCall !== 'function') throw new TypeError('rejectCall es requerido');
  if (typeof sendMessage !== 'function') throw new TypeError('sendMessage es requerido');
  if (typeof getCallMessage !== 'function' && typeof getResponseMessage !== 'function') {
    throw new TypeError('getCallMessage o getResponseMessage es requerido');
  }
  if (routeInteraction !== null && typeof routeInteraction !== 'function') {
    throw new TypeError('routeInteraction debe ser una función');
  }
  if (routeInteraction && typeof getResponseMessage !== 'function') {
    throw new TypeError('getResponseMessage es requerido con routeInteraction');
  }
  if (typeof resolveContactId !== 'function') {
    throw new TypeError('resolveContactId debe ser una función');
  }
  if (typeof deliveryAllowed !== 'function') {
    throw new TypeError('deliveryAllowed must be a function');
  }
  if (typeof serializeClaim !== 'function') {
    throw new TypeError('serializeClaim debe ser una función');
  }
  if (typeof serializeInteraction !== 'function') {
    throw new TypeError('serializeInteraction debe ser una función');
  }
  if (typeof readAudio !== 'function') throw new TypeError('readAudio es requerido');
  if (typeof readVideo !== 'function') throw new TypeError('readVideo debe ser una función');
  if (!Number.isFinite(contactResolutionTimeoutMs) || contactResolutionTimeoutMs <= 0) {
    throw new TypeError('contactResolutionTimeoutMs debe ser positivo');
  }
  if (!Number.isFinite(sendTimeoutMs) || sendTimeoutMs <= 0) {
    throw new TypeError('sendTimeoutMs debe ser positivo');
  }

  function emitMetric(metric) {
    try {
      const pending = onCallMetric(metric);
      if (pending && typeof pending.catch === 'function') pending.catch(() => {});
    } catch {
      // Diagnostics are best-effort and must never alter call handling.
    }
  }

  function eventMetricContext(call) {
    const rawStatus = typeof call?.status === 'string' && call.status ? call.status : 'missing';
    return {
      event: SAFE_EVENT_STATUSES.has(rawStatus) ? rawStatus : 'other',
      offline: typeof call?.offline === 'boolean' ? call.offline : null,
      video: typeof call?.isVideo === 'boolean' ? call.isVideo : null,
      group: typeof call?.isGroup === 'boolean' ? call.isGroup : null,
    };
  }

  function finish(call, result, reason, delivery = {}) {
    emitMetric({
      type: 'outcome',
      ...eventMetricContext(call),
      outcome: result.status,
      reason,
      reject: delivery.reject || 'not_applicable',
      text: delivery.text || 'not_applicable',
      audio: delivery.audio || 'not_applicable',
      videoDelivery: delivery.video || 'not_applicable',
      delivery: delivery.delivery || 'not_applicable',
      target: delivery.targetSource || 'not_applicable',
      targetKind: delivery.targetKind || 'not_applicable',
    });
    return result;
  }

  async function rejectIncomingCall(call) {
    if (call.offline) {
      logger.info?.('[WA CALL] Offline offer; rejection skipped');
      return 'skipped_offline';
    }
    try {
      await settleWithTimeout(
        Promise.resolve(rejectCall(call.id, call.from)),
        rejectTimeoutMs,
        'El rechazo de la llamada',
      );
      return 'sent';
    } catch {
      logger.error?.('[WA CALL] Call rejection failed');
      return 'failed';
    }
  }

  async function handleOneCall(call) {
    if (!call || typeof call !== 'object') {
      return finish(call, { status: 'ignored', reason: 'invalid_event' }, 'invalid_event');
    }
    const metricContext = eventMetricContext(call);
    logger.info?.(
      `[WA CALL] Event status=${metricContext.event} ` +
      `offline=${Boolean(call.offline)} video=${Boolean(call.isVideo)}`,
    );
    if (call.status !== 'offer') {
      return finish(
        call,
        { status: 'ignored', reason: `status_${call.status || 'missing'}` },
        'non_offer',
      );
    }
    if (!call.id || !call.from) {
      logger.warn?.('[WA CALL] Oferta ignorada: faltan id o from');
      return finish(call, { status: 'ignored', reason: 'missing_identity' }, 'missing_identity');
    }
    if (call.isGroup) {
      logger.info?.('[WA CALL] Group call ignored');
      return finish(call, { status: 'ignored', reason: 'group_call' }, 'group_call');
    }

    const currentTime = now();
    pruneHandledCalls(handledCalls, currentTime, dedupeTtlMs);
    const dedupeKey = `${call.from}:${call.id}`;
    if (handledCalls.has(dedupeKey)) {
      logger.info?.('[WA CALL] Duplicate offer ignored');
      return finish(call, { status: 'ignored', reason: 'duplicate' }, 'duplicate');
    }

    // Reclamar el evento antes del primer await evita respuestas simultáneas.
    handledCalls.set(dedupeKey, currentTime);
    const rejectionPromise = rejectIncomingCall(call);

    // Prefer the phone-number JID. Sending to a LID can resolve without the
    // recipient receiving or synchronizing the message on all devices.
    const replyTarget = selectReplyTarget(call);
    const replyJid = replyTarget.jid;
    const contactAliases = [
      replyJid,
      normalizeJid(call.callerPn),
      normalizeJid(call.chatId),
      normalizeJid(call.from),
    ].filter(Boolean);

    let claimed;
    try {
      claimed = await serializeClaim(async () => {
        let contactId = replyJid;
        try {
          contactId = await settleWithTimeout(
            Promise.resolve(resolveContactId(replyJid, call)),
            contactResolutionTimeoutMs,
            'La resolución de identidad de la llamada',
          ) || replyJid;
        } catch {
          logger.warn?.('[WA CALL] Contact mapping failed; using reply target');
        }

        try {
          if (!await deliveryAllowed()) {
            return { contactId, interactionDecision: null, deliveryBlocked: true };
          }
        } catch {
          return { contactId, interactionDecision: null, deliveryBlocked: true };
        }

        let interactionDecision = null;
        if (routeInteraction) {
          interactionDecision = await routeInteraction({
            contactId,
            contactAliases,
            eventId: `call:${call.id}`,
            kind: 'call',
            detectedLanguage: null,
            provisionalLanguage: provisionalLanguageFromWhatsAppIdentity(
              contactId,
              contactAliases,
            ),
          });
        }
        return { contactId, interactionDecision };
      });
    } catch {
      logger.error?.('[WA CALL] Interaction state failed');
      const failedResult = {
        status: 'failed',
        reason: 'interaction_state_failed',
        reject: await rejectionPromise,
      };
      return finish(call, failedResult, 'interaction_state_failed', {
        ...failedResult,
        targetSource: replyTarget.source,
        targetKind: replyTarget.kind,
      });
    }

    if (claimed.deliveryBlocked) {
      const blockedResult = {
        status: 'ignored',
        reason: 'delivery_blocked',
        reject: await rejectionPromise,
      };
      return finish(call, blockedResult, 'delivery_blocked', {
        ...blockedResult,
        targetSource: replyTarget.source,
        targetKind: replyTarget.kind,
      });
    }

    const interactionDecision = claimed.interactionDecision;
    if (interactionDecision?.duplicate) {
      logger.info?.('[WA CALL] Persisted duplicate offer ignored');
      const duplicateResult = {
        status: 'ignored',
        reason: 'duplicate',
        reject: await rejectionPromise,
      };
      return finish(call, duplicateResult, 'duplicate', {
        ...duplicateResult,
        targetSource: replyTarget.source,
        targetKind: replyTarget.kind,
      });
    }

    const deliveryKey = interactionDecision?.contactKey || claimed.contactId;
    const delivery = await serializeInteraction(deliveryKey, async () => {
      const result = {
        status: 'handled',
        callId: call.id,
        replyJid,
        reject: 'pending',
        text: 'skipped',
        audio: 'skipped',
        video: 'skipped',
        targetSource: replyTarget.source,
        targetKind: replyTarget.kind,
        // Every distinct call attempt receives the dedicated call response.
        // routeInteraction still records the event and advances the contact
        // phase; this boundary also protects against a custom router key.
        response: 'call',
      };

      logger.info?.(`[WA CALL] Offer received${call.isVideo ? ' (video)' : ''}`);
      let language = 'en';
      let callMessage = {};
      let preparationFailed = false;
      try {
        if (interactionDecision) {
          language = interactionDecision.language || 'es';
          callMessage = getResponseMessage(language, 'call') || {};
        } else {
          language = getLanguage(call, replyJid) || 'en';
          callMessage = getCallMessage(language) || {};
        }
      } catch {
        preparationFailed = true;
        logger.error?.('[WA CALL] Response preparation failed');
      }

      const text = typeof callMessage.text === 'string' ? callMessage.text.trim() : '';
      const prepare = {
        text: async () => ({ text }),
        audio: async () => toWhatsAppAudioContent(await readAudio(callMessage.audio)),
        video: async () => toWhatsAppVideoContent(await readVideo(callMessage.video)),
      };
      const configured = { text: Boolean(text), audio: Boolean(callMessage.audio), video: Boolean(callMessage.video) };
      // Video leads configured responses. Calls without video retain audio/text.
      const order = configured.video ? ['video', 'text', 'audio'] : ['audio', 'text'];
      for (const component of order) {
        if (!configured[component]) continue;
        let content;
        try {
          content = await prepare[component]();
        } catch {
          result[component] = 'failed';
          logger.error?.(`[WA CALL] ${component} preparation failed`);
          continue;
        }
        if (!content) {
          result[component] = 'missing';
          continue;
        }
        try {
          await settleWithTimeout(
            Promise.resolve(sendMessage(replyJid, content)),
            sendTimeoutMs,
            `El envío de ${component} de la llamada`,
          );
          result[component] = 'sent';
        } catch {
          // Sending may have succeeded without an ACK. Keep deduplication and
          // the delivery-safety gate intact; never retry this component here.
          result[component] = 'uncertain';
          logger.error?.(`[WA CALL] ${component} delivery unconfirmed`);
        }
      }
      const outcomes = [result.text, result.audio, result.video];
      const incomplete = preparationFailed
        || outcomes.some(outcome => ['failed', 'missing', 'uncertain'].includes(outcome));
      result.delivery = outcomes.includes('uncertain') ? 'uncertain'
        : incomplete ? (outcomes.includes('sent') ? 'partial' : 'failed')
          : outcomes.includes('sent') ? 'complete' : 'skipped';
      const reason = incomplete
        ? (result.delivery === 'uncertain' ? 'delivery_uncertain' : 'delivery_incomplete')
        : 'completed';
      if (incomplete) {
        result.status = 'failed';
        result.reason = reason;
      }
      return { reason, result, language };
    });

    delivery.result.reject = await rejectionPromise;
    if (delivery.reason === 'completed') {
      logger.info?.(
        `[WA CALL] ${delivery.language.toUpperCase()} response processed ` +
        `type=${delivery.result.response} delivery=${delivery.result.delivery}`,
      );
    }
    return finish(call, delivery.result, delivery.reason, {
      ...delivery.result,
      targetSource: replyTarget.source,
      targetKind: replyTarget.kind,
    });
  }

  return async function handleCallBatch(payload) {
    const calls = normalizeCallBatch(payload);
    emitMetric({
      type: 'batch',
      payload: Array.isArray(payload)
        ? 'array'
        : (payload && typeof payload === 'object' ? 'object' : 'invalid'),
      size: calls.length === 0 ? 'empty' : (calls.length === 1 ? 'one' : 'multiple'),
    });
    if (calls.length === 0) {
      logger.warn?.('[WA CALL] Payload vacío o inválido');
      return [];
    }

    return Promise.all(calls.map(async (call) => {
      try {
        return await handleOneCall(call);
      } catch (error) {
        logger.error?.('[WA CALL] Unexpected handler failure');
        emitMetric({
          type: 'outcome',
          ...eventMetricContext(call),
          outcome: 'failed',
          reason: 'unexpected_error',
          reject: 'not_applicable',
          text: 'not_applicable',
          audio: 'not_applicable',
          target: 'not_applicable',
          targetKind: 'not_applicable',
        });
        return { status: 'failed', reason: 'unexpected_error' };
      }
    }));
  };
}
