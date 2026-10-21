import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

const STATE_VERSION = 1;
const DEFAULT_RETRY_MS = 60 * 60 * 1000;
const RATE_LIMIT_RETRY_MS = 24 * 60 * 60 * 1000;
const DEFAULT_TIMEOUT_MS = 30_000;
const MAX_ACCOUNTS = 12;
const REQUEST_TTL_MS = 10 * 60 * 1000;
const inFlight = new Set();

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

export function whatsappProfileAccountHash(jid) {
  const value = String(jid || '').trim().toLowerCase();
  if (!value) return null;
  const [local, server = 's.whatsapp.net'] = value.split('@', 2);
  const account = local.split(':', 1)[0];
  if (!account || !server) return null;
  return sha256(`${account}@${server}`);
}

function readRequest(requestPath, accountHash, timestamp) {
  if (!requestPath) return { status: 'not_requested' };
  let request;
  try { request = JSON.parse(fs.readFileSync(requestPath, 'utf8')); } catch (error) {
    return { status: error?.code === 'ENOENT' ? 'not_requested' : 'failed', errorCode: 'request_unavailable' };
  }
  if (request?.brand !== 'pecadosvip' || request.status !== 'pending'
    || !/^[a-f0-9]{32}$/.test(request.requestId || '')
    || !/^[a-f0-9]{64}$/.test(request.accountHash || '')
    || !/^[a-f0-9]{64}$/.test(request.imageHash || '')
    || !Number.isFinite(request.requestedAt) || !Number.isFinite(request.expiresAt)
    || request.expiresAt - request.requestedAt !== REQUEST_TTL_MS) {
    return { status: 'failed', errorCode: 'request_invalid' };
  }
  if (request.accountHash !== accountHash) return { status: 'not_requested' };
  if (timestamp < request.requestedAt || timestamp > request.expiresAt) {
    return { status: 'failed', errorCode: 'request_expired' };
  }
  return { request };
}

function readState(statePath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    if (parsed?.version === STATE_VERSION && parsed.accounts
      && typeof parsed.accounts === 'object' && !Array.isArray(parsed.accounts)) {
      return parsed;
    }
  } catch (error) {
    if (error?.code === 'ENOENT') return { version: STATE_VERSION, accounts: {} };
  }
  // Do not erase evidence of an outstanding request if the state is unreadable.
  throw new Error('state_unavailable');
}

function writeStateAtomic(statePath, state) {
  const temporaryPath = `${statePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  try {
    fs.writeFileSync(temporaryPath, JSON.stringify(state), { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(temporaryPath, statePath);
  } catch (error) {
    // Never delete the previous state when replacing it fails.
    try { fs.rmSync(temporaryPath, { force: true }); } catch { /* Preserve original failure. */ }
    throw error;
  }
}

function writeAccount(statePath, accountHash, record, timestamp) {
  // Other accounts may finish while this account awaits the provider.
  const state = readState(statePath);
  state.accounts[accountHash] = record;
  const protectedEntries = [];
  const completedEntries = [];
  for (const entry of Object.entries(state.accounts)) {
    const account = entry[1];
    if (account?.status === 'pending' || account?.status === 'uncertain'
      || Number(account?.retryAfter || 0) > timestamp) {
      protectedEntries.push(entry);
    } else {
      completedEntries.push(entry);
    }
  }
  completedEntries.sort((left, right) => Number(right[1]?.attemptedAt || 0) - Number(left[1]?.attemptedAt || 0));
  state.accounts = Object.fromEntries([...protectedEntries, ...completedEntries.slice(0, MAX_ACCOUNTS)]);
  writeStateAtomic(statePath, state);
}

function providerFailure(error) {
  // Baileys IQ rejections carry their provider code in Boom.data. Socket
  // disconnect codes instead use output.statusCode and may arrive after send.
  const remoteStatus = typeof error?.data === 'number' ? error.data : undefined;
  const status = Number(remoteStatus ?? error?.output?.statusCode ?? error?.data?.statusCode ?? error?.statusCode);
  const message = String(error?.message || error?.data || '').toLowerCase();
  if (status === 429 || message.includes('rate-overlimit') || message.includes('rate limit')) {
    return { status: 'failed', errorCode: 'rate_limited', retryDelay: RATE_LIMIT_RETRY_MS };
  }
  const disconnected = remoteStatus === undefined && [401, 403, 408, 411, 428, 440].includes(status);
  if (status >= 400 && status < 500 && status !== 408 && !disconnected) {
    return { status: 'failed', errorCode: 'rejected', retryDelay: DEFAULT_RETRY_MS };
  }
  // Transport errors can occur after WhatsApp accepted the update.
  return { status: 'uncertain', errorCode: 'update_unconfirmed' };
}

async function boundedUpdate(update, timeoutMs) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(update).then(
        () => ({ status: 'applied', verification: 'provider_acknowledged' }),
        error => providerFailure(error),
      ),
      new Promise(resolve => {
        timer = setTimeout(() => resolve({ status: 'uncertain', errorCode: 'update_timeout' }), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export async function applyWhatsAppProfilePicture({
  jid,
  updateProfilePicture,
  imagePath,
  statePath,
  requestPath,
  expectedImageHash,
  logger = console,
  now = Date.now,
  timeoutMs = DEFAULT_TIMEOUT_MS,
}) {
  const accountHash = whatsappProfileAccountHash(jid);
  if (!accountHash || typeof updateProfilePicture !== 'function') return { status: 'missing' };
  const key = `${path.resolve(statePath)}:${accountHash}`;
  if (inFlight.has(key)) return { status: 'pending' };
  inFlight.add(key);

  try {
    const timestamp = Number(now());
    const requested = readRequest(requestPath, accountHash, timestamp);
    if (!requested.request) return requested;
    const request = requested.request;
    const previous = readState(statePath).accounts[accountHash];
    // A previous process may have stopped after sending its request. An image
    // change cannot make that outstanding operation safe to retry automatically.
    if (previous?.status === 'pending' || previous?.status === 'uncertain') {
      if (previous.status === 'pending') {
        writeAccount(statePath, accountHash, {
          ...previous, status: 'uncertain', errorCode: 'interrupted',
        }, timestamp);
      }
      return { status: 'uncertain', errorCode: previous.status === 'pending' ? 'interrupted' : previous.errorCode };
    }
    // Preserve an account cooldown even while a configured image is unavailable
    // or changes: a temporary configuration error must not erase the rate limit.
    if (previous?.status === 'failed' && timestamp < Number(previous.retryAfter || 0)) {
      return { status: 'cooldown', errorCode: previous.errorCode || 'rejected' };
    }

    let image;
    try { image = fs.readFileSync(imagePath); } catch { /* Recorded below without local paths. */ }
    const imageHash = image ? sha256(image) : null;
    const record = { imageHash, attemptedAt: timestamp, requestId: request.requestId, brand: 'pecadosvip' };
    if (expectedImageHash !== undefined) record.expectedImageHash = expectedImageHash;
    const save = result => {
      writeAccount(statePath, accountHash, { ...record, ...result }, timestamp);
      return result.errorCode
        ? { status: result.status, errorCode: result.errorCode }
        : { status: result.status, verification: result.verification };
    };

    if (!/^[a-f0-9]{64}$/.test(expectedImageHash || '')) {
      return save({ status: 'missing', errorCode: 'official_image_missing' });
    }
    if (!image) return save({ status: 'missing', errorCode: 'image_missing' });
    if (imageHash !== expectedImageHash) {
      return save({ status: 'failed', errorCode: 'unexpected_image' });
    }
    if (imageHash !== request.imageHash) return save({ status: 'failed', errorCode: 'request_image_changed' });
    if (previous?.requestId === request.requestId && previous.status === 'failed') {
      return { status: 'failed', errorCode: previous.errorCode || 'rejected' };
    }
    // Reconnects may replay the same request. A new explicit link request must
    // apply again: an older ACK does not describe the account's current photo.
    if (previous?.status === 'applied' && previous.imageHash === imageHash
      && previous.requestId === request.requestId) {
      if (!previous.verification) {
        writeAccount(statePath, accountHash, {
          ...previous, verification: 'provider_acknowledged',
        }, timestamp);
      }
      return { status: 'unchanged', verification: 'provider_acknowledged' };
    }

    writeAccount(statePath, accountHash, { ...record, status: 'pending' }, timestamp);
    const boundedTimeout = Math.min(60_000, Math.max(1, Number(timeoutMs) || DEFAULT_TIMEOUT_MS));
    const result = await boundedUpdate(
      () => {
        const current = readRequest(requestPath, accountHash, Number(now())).request;
        if (current?.requestId !== request.requestId) {
          throw Object.assign(new Error('request superseded'), { data: 409 });
        }
        return updateProfilePicture(jid, image, { width: 640, height: 640 });
      },
      boundedTimeout,
    );
    const { retryDelay, ...outcome } = result;
    if (retryDelay) outcome.retryAfter = Number(now()) + retryDelay;
    if (outcome.status === 'applied') outcome.appliedAt = Number(now());
    const saved = save(outcome);
    if (outcome.status === 'applied') {
      logger.info?.('[WA] WhatsApp confirmó la actualización del logo de perfil');
    } else {
      logger.warn?.('[WA] Actualización del logo sin confirmar; consulte el estado de perfil');
    }
    return saved;
  } catch {
    // In particular, a write failure after provider ACK is never a retryable
    // rejection: the pending record preserves the uncertain operation.
    logger.warn?.('[WA] No fue posible guardar o leer el estado del logo de perfil');
    return { status: 'uncertain', errorCode: 'state_unavailable' };
  } finally {
    inFlight.delete(key);
  }
}

export function pollWhatsAppProfilePicture({ getOptions, isActive, intervalMs = 5_000, durationMs = REQUEST_TTL_MS }) {
  let stopped = false;
  let timer;
  const deadline = Date.now() + durationMs;
  const stop = () => {
    stopped = true;
    clearTimeout(timer);
  };
  const tick = async () => {
    if (stopped || !isActive() || Date.now() > deadline) { stop(); return; }
    try { await applyWhatsAppProfilePicture(getOptions()); } catch { /* Status remains unconfirmed. */ }
    if (stopped || !isActive() || Date.now() > deadline) { stop(); return; }
    timer = setTimeout(tick, intervalMs);
    timer.unref?.();
  };
  void tick();
  return { stop };
}
