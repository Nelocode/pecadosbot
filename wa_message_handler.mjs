import {
  getContentType,
  jidNormalizedUser,
  normalizeMessageContent,
} from '@whiskeysockets/baileys';
import { randomUUID } from 'node:crypto';
import { settleWithTimeout } from './keyed_serial_queue.mjs';
import { toWhatsAppAudioContent, toWhatsAppVideoContent } from './wa_audio_delivery.mjs';
import { provisionalLanguageFromWhatsAppIdentity } from './whatsapp_language_hint.mjs';

const DEFAULT_CONTACT_RESOLUTION_TIMEOUT_MS = 5_000;
const DEFAULT_SEND_TIMEOUT_MS = 20_000;

const INTERACTION_TYPES = new Set([
  'conversation',
  'extendedTextMessage',
  'imageMessage',
  'videoMessage',
  'ptvMessage',
  'audioMessage',
  'documentMessage',
  'stickerMessage',
  'lottieStickerMessage',
  'stickerPackMessage',
  'albumMessage',
  'contactMessage',
  'contactsArrayMessage',
  'locationMessage',
  'liveLocationMessage',
  'productMessage',
  'orderMessage',
  'invoiceMessage',
  'buttonsResponseMessage',
  'listResponseMessage',
  'templateButtonReplyMessage',
  'interactiveResponseMessage',
  'pollCreationMessage',
  'requestPhoneNumberMessage',
]);

function isPnJid(jid) {
  return typeof jid === 'string'
    && (jid.endsWith('@s.whatsapp.net') || jid.endsWith('@hosted'));
}

function isLidJid(jid) {
  return typeof jid === 'string'
    && (jid.endsWith('@lid') || jid.endsWith('@hosted.lid'));
}

function isGroupJid(jid) {
  return typeof jid === 'string' && jid.endsWith('@g.us');
}

function normalizeJid(jid) {
  if (typeof jid !== 'string' || !jid) return '';
  try {
    return jidNormalizedUser(jid);
  } catch {
    return jid;
  }
}

export async function selectDirectMessageTarget(msg, resolvePnForLid = async () => null) {
  const remoteJid = normalizeJid(msg?.key?.remoteJid || '');
  const alternateJid = normalizeJid(msg?.key?.remoteJidAlt || '');
  if (isGroupJid(remoteJid) || isGroupJid(alternateJid) || remoteJid === 'status@broadcast') {
    return '';
  }
  if (isPnJid(remoteJid)) return remoteJid;
  if (isPnJid(alternateJid)) return alternateJid;
  if (!isLidJid(remoteJid)) return '';

  try {
    const mapped = normalizeJid(await resolvePnForLid(remoteJid));
    if (isPnJid(mapped)) return mapped;
  } catch {
    // The LID itself remains a valid direct-chat fallback.
  }
  return remoteJid;
}

export function describeInteraction(message) {
  if (message?.editedMessage || message?.groupStatusMessage || message?.groupStatusMessageV2) {
    return null;
  }
  const content = normalizeMessageContent(message);
  const contentType = getContentType(content);
  if (!content || !contentType || !INTERACTION_TYPES.has(contentType)) return null;

  const typedContent = content[contentType];
  const textCandidates = [
    content.conversation,
    content.extendedTextMessage?.text,
    content.imageMessage?.caption,
    content.videoMessage?.caption,
    content.ptvMessage?.caption,
    content.documentMessage?.caption,
    typedContent?.selectedDisplayText,
    typedContent?.title,
  ];
  const text = textCandidates.find(value => typeof value === 'string' && value.trim())?.trim() || '';
  return { contentType, text };
}

export function interactionEventId(msg, contentType) {
  const messageId = msg?.key?.id;
  if (!messageId) return '';
  const normalized = normalizeMessageContent(msg?.message);
  const typedContent = normalized?.[getContentType(normalized)];
  const parentId = msg?.message?.messageContextInfo?.messageAssociation?.parentMessageKey?.id
    || normalized?.messageContextInfo?.messageAssociation?.parentMessageKey?.id
    || typedContent?.contextInfo?.messageAssociation?.parentMessageKey?.id;
  if (parentId) return `album:${parentId}`;
  return contentType === 'albumMessage' ? `album:${messageId}` : `message:${messageId}`;
}

function messageTimestampMs(msg) {
  const raw = msg?.messageTimestamp;
  let value;
  if (typeof raw === 'number') value = raw;
  else if (typeof raw === 'bigint') value = Number(raw);
  else if (raw && typeof raw.toNumber === 'function') value = raw.toNumber();
  else value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value < 1_000_000_000_000 ? value * 1000 : value;
}

export function createWhatsAppMessageHandler({
  sendMessage,
  routeInteraction,
  getResponseMessage,
  readAudio,
  readVideo = async () => null,
  detectLanguage,
  markRead = async () => {},
  deliveryAllowed = () => true,
  resolvePnForLid = async () => null,
  serializeClaim = async operation => operation(),
  serializeInteraction = async (_contactId, operation) => operation(),
  now = () => Date.now(),
  maxOfflineAgeMs = 15 * 60 * 1000,
  contactResolutionTimeoutMs = DEFAULT_CONTACT_RESOLUTION_TIMEOUT_MS,
  sendTimeoutMs = DEFAULT_SEND_TIMEOUT_MS,
  logger = console,
}) {
  if (typeof sendMessage !== 'function') throw new TypeError('sendMessage is required');
  if (typeof routeInteraction !== 'function') throw new TypeError('routeInteraction is required');
  if (typeof getResponseMessage !== 'function') throw new TypeError('getResponseMessage is required');
  if (typeof readAudio !== 'function') throw new TypeError('readAudio is required');
  if (typeof readVideo !== 'function') throw new TypeError('readVideo must be a function');
  if (typeof detectLanguage !== 'function') throw new TypeError('detectLanguage is required');
  if (typeof markRead !== 'function') throw new TypeError('markRead must be a function');
  if (typeof deliveryAllowed !== 'function') {
    throw new TypeError('deliveryAllowed must be a function');
  }
  if (typeof serializeClaim !== 'function') {
    throw new TypeError('serializeClaim must be a function');
  }
  if (typeof serializeInteraction !== 'function') {
    throw new TypeError('serializeInteraction must be a function');
  }
  if (typeof now !== 'function') throw new TypeError('now must be a function');
  if (!Number.isFinite(maxOfflineAgeMs) || maxOfflineAgeMs < 0) {
    throw new TypeError('maxOfflineAgeMs must be non-negative');
  }
  if (!Number.isFinite(contactResolutionTimeoutMs) || contactResolutionTimeoutMs <= 0) {
    throw new TypeError('contactResolutionTimeoutMs must be positive');
  }
  if (!Number.isFinite(sendTimeoutMs) || sendTimeoutMs <= 0) {
    throw new TypeError('sendTimeoutMs must be positive');
  }

  async function deliverResponse({ jid, decision, messageKey }) {
    const deliveryContext = { trace_id: randomUUID(), response: decision.responseKey };
    const response = getResponseMessage(decision.language, decision.responseKey) || {};
    const result = {
      status: 'handled',
      response: decision.responseKey,
      text: 'skipped',
      audio: 'skipped',
      video: 'skipped',
    };

    try {
      await markRead(messageKey);
    } catch {
      // Read receipts are best effort and must not prevent a valid response.
      logger.warn?.('[WA] Read receipt failed');
    }

    const text = typeof response.text === 'string' ? response.text.trim() : '';
    const prepare = {
      text: async () => ({ text }),
      audio: async () => toWhatsAppAudioContent(await readAudio(response.audio)),
      video: async () => toWhatsAppVideoContent(await readVideo(response.video)),
    };
    const configured = { text: Boolean(text), audio: Boolean(response.audio), video: Boolean(response.video) };
    const order = configured.video ? ['video', 'text', 'audio'] : ['text', 'audio'];
    for (const component of order) {
      if (!configured[component]) continue;
      let content;
      try {
        content = await prepare[component]();
      } catch {
        result[component] = 'failed';
        logger.error?.(`[WA] ${component} preparation failed`);
        continue;
      }
      if (!content) {
        result[component] = 'missing';
        continue;
      }
      try {
        await settleWithTimeout(
          Promise.resolve(sendMessage(jid, content, deliveryContext)),
          sendTimeoutMs,
          `El envío de ${component} de WhatsApp`,
        );
        result[component] = 'sent';
      } catch {
        // A timeout does not cancel Baileys. Keep the event claimed and never
        // retry an attempted send whose outcome is unknown.
        result[component] = 'uncertain';
        logger.error?.(`[WA] ${component} delivery unconfirmed`);
        if (component === 'video') {
          if (configured.text) result.text = 'held';
          if (configured.audio) result.audio = 'held';
          // A late video result must never resume the remaining components.
          break;
        }
      }
    }

    const outcomes = [result.text, result.audio, result.video];
    const incomplete = outcomes.some(outcome => ['failed', 'missing', 'uncertain'].includes(outcome));
    result.delivery = outcomes.includes('uncertain') ? 'uncertain'
      : incomplete ? (outcomes.includes('sent') ? 'partial' : 'failed')
        : outcomes.includes('sent') ? 'complete' : 'skipped';
    if (incomplete) {
      result.status = 'failed';
      result.reason = result.delivery === 'uncertain' ? 'delivery_uncertain' : 'delivery_incomplete';
    }
    logger.info?.(`[WA] Response processed type=${decision.responseKey} delivery=${result.delivery}`);
    return result;
  }

  async function processOne(msg, type) {
    if (msg?.key?.fromMe) {
      return { status: 'ignored', reason: 'outgoing' };
    }
    if (type === 'append') {
      const timestamp = messageTimestampMs(msg);
      const age = timestamp === null ? Number.POSITIVE_INFINITY : now() - timestamp;
      if (age < -5 * 60 * 1000 || age > maxOfflineAgeMs) {
        return { status: 'ignored', reason: 'historical_append' };
      }
    }

    const interaction = describeInteraction(msg.message);
    if (!interaction) {
      return { status: 'ignored', reason: 'non_interaction' };
    }
    if (interaction.contentType === 'albumMessage' && !interaction.text) {
      // Baileys may emit the album envelope in an earlier callback than its
      // children. It carries no customer evidence and must never consume the
      // event id, phase, or provisional language before a caption arrives.
      return { status: 'ignored', reason: 'album_placeholder' };
    }

    const eventId = interactionEventId(msg, interaction.contentType);
    if (!eventId) {
      logger.warn?.('[WA] Inbound interaction without message id ignored');
      return { status: 'ignored', reason: 'missing_event_id' };
    }

    let claimed;
    try {
      claimed = await serializeClaim(async () => {
        let jid;
        try {
          jid = await settleWithTimeout(
            selectDirectMessageTarget(msg, resolvePnForLid),
            contactResolutionTimeoutMs,
            'La resolución de identidad de WhatsApp',
          );
        } catch {
          logger.warn?.('[WA] Contact mapping timed out; using direct-chat fallback');
          jid = await selectDirectMessageTarget(msg, async () => null);
        }
        if (!jid) return { ignored: { status: 'ignored', reason: 'non_direct' } };
        try {
          if (!await deliveryAllowed()) {
            return { ignored: { status: 'ignored', reason: 'delivery_blocked' } };
          }
        } catch {
          return { ignored: { status: 'ignored', reason: 'delivery_blocked' } };
        }

        const contactAliases = [
          normalizeJid(msg?.key?.remoteJid || ''),
          normalizeJid(msg?.key?.remoteJidAlt || ''),
          jid,
        ].filter(Boolean);
        const detectedResult = interaction.text ? detectLanguage(interaction.text) : null;
        const languageEvidence = (
          detectedResult && typeof detectedResult === 'object' && !Array.isArray(detectedResult)
        ) ? detectedResult : null;
        const detectedLanguage = typeof detectedResult === 'string'
          ? detectedResult
          : languageEvidence?.language || null;
        const decision = await routeInteraction({
          contactId: jid,
          contactAliases,
          eventId,
          kind: 'content',
          detectedLanguage,
          languageEvidence,
          provisionalLanguage: languageEvidence?.strong === true
            ? null
            : provisionalLanguageFromWhatsAppIdentity(contactAliases),
        });
        if (decision?.persisted === false) throw new Error('interaction_state_not_persisted');
        return { jid, decision };
      });
    } catch {
      logger.error?.('[WA] Interaction state failed');
      return { status: 'failed', reason: 'interaction_state_failed' };
    }

    if (claimed.ignored) return claimed.ignored;
    if (claimed.decision.duplicate) {
      return { status: 'ignored', reason: 'duplicate' };
    }

    return serializeInteraction(
      claimed.decision.contactKey || claimed.jid,
      () => deliverResponse({
        jid: claimed.jid,
        decision: claimed.decision,
        messageKey: msg?.key,
      }),
    );
  }

  return async function handleMessageBatch({ messages = [], type } = {}) {
    if (!['notify', 'append'].includes(type) || !Array.isArray(messages)) return [];
    // Baileys may deliver an empty album parent before a child carrying the
    // only useful caption. Both intentionally share one event id. Select the
    // text-bearing representative first so the empty parent cannot lock a
    // provisional language and make the caption look like a persisted retry.
    const batchEvents = messages.map(msg => {
      const interaction = describeInteraction(msg?.message);
      if (!interaction) return null;
      const eventId = interactionEventId(msg, interaction.contentType);
      const identity = [
        normalizeJid(msg?.key?.remoteJid || ''),
        normalizeJid(msg?.key?.remoteJidAlt || ''),
      ].filter(Boolean);
      return {
        eventId,
        identities: new Set(identity),
        hasText: Boolean(interaction.text),
        isAlbumPlaceholder: interaction.contentType === 'albumMessage'
          && !interaction.text,
      };
    });

    // Identity metadata can become richer within one batch: an album parent
    // may carry only a LID while its child carries PN + LID. Build connected
    // components for the same logical event instead of requiring exact sets.
    // Transitive overlap also covers LID -> (LID, PN) -> PN sequences.
    const componentParents = batchEvents.map((_event, index) => index);
    const findComponent = index => {
      let root = index;
      while (componentParents[root] !== root) root = componentParents[root];
      while (componentParents[index] !== index) {
        const parent = componentParents[index];
        componentParents[index] = root;
        index = parent;
      }
      return root;
    };
    const joinComponents = (left, right) => {
      const leftRoot = findComponent(left);
      const rightRoot = findComponent(right);
      if (leftRoot !== rightRoot) componentParents[rightRoot] = leftRoot;
    };
    for (let left = 0; left < batchEvents.length; left += 1) {
      const leftEvent = batchEvents[left];
      if (!leftEvent?.eventId || leftEvent.identities.size === 0) continue;
      for (let right = left + 1; right < batchEvents.length; right += 1) {
        const rightEvent = batchEvents[right];
        if (!rightEvent || rightEvent.eventId !== leftEvent.eventId) continue;
        const overlaps = [...leftEvent.identities]
          .some(identity => rightEvent.identities.has(identity));
        if (overlaps) joinComponents(left, right);
      }
    }

    const preferredByComponent = new Map();
    for (const [index, event] of batchEvents.entries()) {
      if (!event?.eventId) continue;
      const component = findComponent(index);
      const current = preferredByComponent.get(component);
      const priority = event.hasText ? 2 : (event.isAlbumPlaceholder ? 0 : 1);
      if (!current || priority > current.priority) {
        preferredByComponent.set(component, { index, priority });
      }
    }

    return Promise.all(messages.map((msg, index) => {
      const event = batchEvents[index];
      const component = event?.eventId ? findComponent(index) : null;
      if (
        component !== null
        && preferredByComponent.get(component)?.index !== index
      ) {
        return {
          status: 'ignored',
          reason: event.isAlbumPlaceholder ? 'album_placeholder' : 'duplicate',
        };
      }
      return processOne(msg, type);
    }));
  };
}
