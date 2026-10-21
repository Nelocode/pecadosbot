import fs from 'fs';
import path from 'path';
import { randomUUID } from 'crypto';

const CONNECTION_STATES = new Set(['starting', 'connecting', 'open', 'closed', 'unknown']);
const DISCONNECT_REASONS = new Set([
  'never', 'transient', 'logged_out', 'session_invalid', 'timeout', 'shutdown', 'unknown',
]);
const EVENT_STATES = new Set([
  'offer', 'ringing', 'preaccept', 'transport', 'relaylatency', 'terminate',
  'timeout', 'reject', 'accept', 'other', 'missing', 'never',
]);
const OUTCOME_STATES = new Set(['handled', 'ignored', 'failed', 'never']);
const REASONS = new Set([
  'completed',
  'delivery_incomplete',
  'delivery_uncertain',
  'non_offer',
  'invalid_event',
  'missing_identity',
  'group_call',
  'duplicate',
  'unexpected_error',
  'never',
]);
const DELIVERY_STATES = new Set([
  'sent',
  'failed',
  'uncertain',
  'missing',
  'skipped',
  'skipped_offline',
  'not_applicable',
  'never',
]);
const PAYLOAD_STATES = new Set(['array', 'object', 'invalid', 'never']);
const DELIVERY_SUMMARIES = new Set(['complete', 'partial', 'failed', 'uncertain', 'skipped', 'not_applicable', 'never']);
const SIZE_STATES = new Set(['empty', 'one', 'multiple', 'never']);
const TARGET_STATES = new Set(['caller_pn', 'chat_id', 'from', 'missing', 'not_applicable', 'never']);
const TARGET_KIND_STATES = new Set(['pn', 'lid', 'other', 'missing', 'not_applicable', 'never']);

function allowed(value, choices, fallback) {
  return typeof value === 'string' && choices.has(value) ? value : fallback;
}

function mapEventStatus(value) {
  if (typeof value !== 'string' || value.length === 0) return 'missing';
  return EVENT_STATES.has(value) ? value : 'other';
}

function initialState(idFactory) {
  return {
    schema_version: 1,
    worker_revision: idFactory(),
    connection: 'starting',
    disconnect_reason: 'never',
    reauth_required: false,
    listener: 'pending',
    raw_listener: 'pending',
    raw_call_revision: null,
    parsed_call_revision: null,
    pipeline_revision: null,
    last_event: 'never',
    last_batch: {
      payload: 'never',
      size: 'never',
    },
    pipeline: {
      event: 'never',
      outcome: 'never',
      reason: 'never',
      offline: null,
      video: null,
      group: null,
      reject: 'never',
      text: 'never',
      audio: 'never',
      video_delivery: 'never',
      delivery: 'never',
      target: 'never',
      target_kind: 'never',
    },
  };
}

/**
 * Persists a deliberately low-cardinality WhatsApp call health snapshot.
 * It excludes phone numbers, JIDs, call IDs, text, filenames, errors,
 * timestamps and traffic counters.
 */
export function createWhatsAppCallHealth({
  filePath,
  logger = console,
  idFactory = randomUUID,
  fsModule = fs,
} = {}) {
  if (!filePath) throw new TypeError('filePath is required');

  let state = initialState(idFactory);

  function persist() {
    const directory = path.dirname(filePath);
    const tempPath = `${filePath}.${process.pid}.tmp`;
    try {
      fsModule.mkdirSync(directory, { recursive: true });
      fsModule.writeFileSync(tempPath, `${JSON.stringify(state, null, 2)}\n`, {
        encoding: 'utf8',
        mode: 0o600,
      });
      try {
        fsModule.renameSync(tempPath, filePath);
      } catch {
        fsModule.rmSync(filePath, { force: true });
        fsModule.renameSync(tempPath, filePath);
      }
    } catch {
      try {
        fsModule.rmSync(tempPath, { force: true });
      } catch {
        // Best-effort cleanup only. Diagnostics must never stop the bot.
      }
      logger.warn?.('[WA CALL HEALTH] Snapshot could not be persisted');
    }
  }

  function record(metric = {}) {
    try {
      switch (metric.type) {
        case 'connection':
          state.connection = allowed(metric.state, CONNECTION_STATES, 'unknown');
          if (state.connection === 'open' || state.connection === 'connecting') {
            state.disconnect_reason = 'never';
            state.reauth_required = false;
          } else if (state.connection === 'closed') {
            state.disconnect_reason = allowed(metric.reason, DISCONNECT_REASONS, 'unknown');
            state.reauth_required = metric.reauthRequired === true;
          }
          break;
        case 'listener_registered':
          state.listener = 'registered';
          break;
        case 'raw_listener':
          state.raw_listener = allowed(
            metric.state,
            new Set(['registered', 'unavailable']),
            'unavailable',
          );
          break;
        case 'raw_call':
          state.raw_call_revision = idFactory();
          break;
        case 'batch':
          state.parsed_call_revision = idFactory();
          state.last_batch = {
            payload: allowed(metric.payload, PAYLOAD_STATES, 'invalid'),
            size: allowed(metric.size, SIZE_STATES, 'empty'),
          };
          break;
        case 'outcome':
          state.last_event = mapEventStatus(metric.event);
          if (state.last_event !== 'offer') break;
          state.pipeline_revision = idFactory();
          state.pipeline = {
            event: state.last_event,
            outcome: allowed(metric.outcome, OUTCOME_STATES, 'failed'),
            reason: allowed(metric.reason, REASONS, 'unexpected_error'),
            offline: typeof metric.offline === 'boolean' ? metric.offline : null,
            video: typeof metric.video === 'boolean' ? metric.video : null,
            group: typeof metric.group === 'boolean' ? metric.group : null,
            reject: allowed(metric.reject, DELIVERY_STATES, 'not_applicable'),
            text: allowed(metric.text, DELIVERY_STATES, 'not_applicable'),
            audio: allowed(metric.audio, DELIVERY_STATES, 'not_applicable'),
            video_delivery: allowed(metric.videoDelivery, DELIVERY_STATES, 'not_applicable'),
            delivery: allowed(metric.delivery, DELIVERY_SUMMARIES, 'not_applicable'),
            target: allowed(metric.target, TARGET_STATES, 'not_applicable'),
            target_kind: allowed(metric.targetKind, TARGET_KIND_STATES, 'not_applicable'),
          };
          break;
        default:
          return;
      }
      persist();
    } catch {
      logger.warn?.('[WA CALL HEALTH] Metric could not be recorded');
    }
  }

  function snapshot() {
    return JSON.parse(JSON.stringify(state));
  }

  persist();
  return { record, snapshot };
}
