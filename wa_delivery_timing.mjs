import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const MAX_SAMPLES = 32;
const MAX_DURATION_MS = 3_600_000;
const RESPONSES = new Set(['step1', 'step2', 'call']);
const COMPONENTS = new Set(['text', 'audio', 'video', 'other']);
const OUTCOMES = new Set(['completed', 'failed', 'uncertain', 'blocked', 'cancelled']);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function inputField(input, key) {
  // Ignore inherited properties and accessors: telemetry never needs to run
  // caller code or serialize the original input object.
  try {
    if (!input || typeof input !== 'object' || Array.isArray(input)) return undefined;
    const descriptor = Object.getOwnPropertyDescriptor(input, key);
    return descriptor && Object.hasOwn(descriptor, 'value') ? descriptor.value : undefined;
  } catch {
    return undefined;
  }
}

function duration(value) {
  return typeof value === 'number' && Number.isFinite(value)
    && value >= 0 && value <= MAX_DURATION_MS ? Math.max(0, Math.round(value)) : null;
}

function timestamp(now) {
  try {
    const value = now();
    if (typeof value === 'number' && Number.isFinite(value)
      && value >= 0 && value <= Number.MAX_SAFE_INTEGER) return Math.round(value);
  } catch { /* A broken clock must not interrupt delivery. */ }
  return Date.now();
}

function persistAtomic(filePath, value) {
  const temporaryPath = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
  let descriptor;
  let ownsTemporaryFile = false;
  try {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    descriptor = fs.openSync(temporaryPath, 'wx', 0o600);
    ownsTemporaryFile = true;
    fs.writeFileSync(descriptor, JSON.stringify(value), { encoding: 'utf8' });
    fs.closeSync(descriptor);
    descriptor = undefined;
    fs.renameSync(temporaryPath, filePath);
    ownsTemporaryFile = false;
  } finally {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch { /* Preserve the write failure. */ }
    }
    if (ownsTemporaryFile) {
      try { fs.rmSync(temporaryPath, { force: true }); } catch { /* Best effort cleanup. */ }
    }
  }
}

/** Bounded diagnostics only. A completed SDK call does not prove delivery. */
export function createWhatsAppDeliveryTiming({ filePath, now = Date.now, logger } = {}) {
  const destination = typeof filePath === 'string' && filePath.length > 0 ? filePath : null;
  let updatedAt = timestamp(now);
  const samples = [];

  function snapshot() {
    // Only objects built below are stored, so copying cannot invoke caller code.
    return JSON.parse(JSON.stringify({ schema_version: 1, updated_at: updatedAt, samples }));
  }

  function record(input) {
    const traceId = inputField(input, 'trace_id');
    const response = inputField(input, 'response');
    const rawComponent = inputField(input, 'component');
    const rawOutcome = inputField(input, 'outcome');
    const component = COMPONENTS.has(rawComponent) ? rawComponent : 'other';
    const outcome = OUTCOMES.has(rawOutcome) ? rawOutcome : 'uncertain';
    const totalMs = duration(inputField(input, 'total_ms'));
    updatedAt = timestamp(now);
    const sample = {
      trace_id: typeof traceId === 'string' && traceId.length === 36 && UUID.test(traceId)
        ? traceId : randomUUID(),
      response: RESPONSES.has(response) ? response : null,
      observed_at: updatedAt,
      elapsed_ms: totalMs,
      outcome,
      components: {
        [component]: {
          outcome,
          queue_ms: duration(inputField(input, 'queue_ms')),
          pacing_ms: duration(inputField(input, 'pacing_ms')),
          sdk_ms: duration(inputField(input, 'sdk_ms')),
          total_ms: totalMs,
        },
      },
    };
    samples.push(sample);
    if (samples.length > MAX_SAMPLES) samples.shift();
    if (destination) {
      try {
        persistAtomic(destination, { schema_version: 1, updated_at: updatedAt, samples });
      } catch {
        try {
          void Promise.resolve(logger?.warn?.('WhatsApp delivery timing persistence failed')).catch(() => {});
        } catch { /* Diagnostic only, including a throwing logger getter. */ }
      }
    }
  }

  return { record, snapshot };
}
