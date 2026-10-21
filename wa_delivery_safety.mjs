function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(Math.max(parsed, minimum), maximum);
}

function boundedNumber(value, fallback, minimum, maximum) {
  const parsed = Number.parseFloat(String(value ?? ''));
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(Math.max(parsed, minimum), maximum);
}

export function readWhatsAppSafetyConfig(env = process.env) {
  const result = {
    presenceEnabled: env.WA_PRESENCE_ENABLED !== '0',
    readReceiptsEnabled: env.WA_READ_RECEIPTS_ENABLED !== '0',
    readDelayMinMs: boundedInteger(env.WA_READ_DELAY_MIN_MS, 250, 0, 5_000),
    readDelayMaxMs: boundedInteger(env.WA_READ_DELAY_MAX_MS, 900, 0, 8_000),
    textDelayMinMs: boundedInteger(env.WA_TEXT_DELAY_MIN_MS, 350, 0, 8_000),
    textDelayMaxMs: boundedInteger(env.WA_TEXT_DELAY_MAX_MS, 2_500, 0, 15_000),
    textMsPerCharacter: boundedNumber(env.WA_TEXT_MS_PER_CHAR, 18, 0, 100),
    audioDelayMinMs: boundedInteger(env.WA_AUDIO_DELAY_MIN_MS, 1_500, 0, 15_000),
    audioDelayMaxMs: boundedInteger(env.WA_AUDIO_DELAY_MAX_MS, 4_000, 0, 30_000),
    jitterRatio: boundedNumber(env.WA_UX_JITTER_RATIO, 0.15, 0, 0.3),
    minimumSendIntervalMs: boundedInteger(env.WA_MIN_SEND_INTERVAL_MS, 500, 0, 30_000),
    maxSendsPerMinute: boundedInteger(env.WA_MAX_SENDS_PER_MINUTE, 20, 1, 120),
    backoffBaseMs: boundedInteger(env.WA_BACKOFF_BASE_MS, 30_000, 1_000, 15 * 60_000),
    backoffMaxMs: boundedInteger(env.WA_BACKOFF_MAX_MS, 15 * 60_000, 5_000, 60 * 60_000),
    failureThreshold: boundedInteger(env.WA_CIRCUIT_FAILURE_THRESHOLD, 5, 2, 20),
    failureWindowMs: boundedInteger(env.WA_CIRCUIT_FAILURE_WINDOW_MS, 5 * 60_000, 30_000, 60 * 60_000),
    circuitOpenMs: boundedInteger(env.WA_CIRCUIT_OPEN_MS, 15 * 60_000, 30_000, 60 * 60_000),
    forbiddenCircuitOpenMs: boundedInteger(
      env.WA_FORBIDDEN_CIRCUIT_OPEN_MS,
      30 * 60_000,
      60_000,
      24 * 60 * 60_000,
    ),
    sendTimeoutMs: boundedInteger(env.WA_SAFE_SEND_TIMEOUT_MS, 20_000, 1_000, 60_000),
    auxiliaryTimeoutMs: boundedInteger(env.WA_AUXILIARY_TIMEOUT_MS, 5_000, 500, 30_000),
    maxPendingSends: boundedInteger(env.WA_MAX_PENDING_SENDS, 50, 1, 500),
    queueWaitTimeoutMs: boundedInteger(env.WA_QUEUE_WAIT_TIMEOUT_MS, 30_000, 1_000, 120_000),
    reconnectBaseMs: boundedInteger(env.WA_RECONNECT_BASE_MS, 2_000, 1_000, 60_000),
    reconnectMaxMs: boundedInteger(env.WA_RECONNECT_MAX_MS, 60_000, 5_000, 15 * 60_000),
    reconnectStableMs: boundedInteger(env.WA_RECONNECT_STABLE_MS, 60_000, 5_000, 10 * 60_000),
  };
  result.readDelayMaxMs = Math.max(result.readDelayMinMs, result.readDelayMaxMs);
  result.textDelayMaxMs = Math.max(result.textDelayMinMs, result.textDelayMaxMs);
  result.audioDelayMaxMs = Math.max(result.audioDelayMinMs, result.audioDelayMaxMs);
  result.backoffMaxMs = Math.max(result.backoffBaseMs, result.backoffMaxMs);
  result.reconnectMaxMs = Math.max(result.reconnectBaseMs, result.reconnectMaxMs);
  return result;
}

function defaultSleep(ms, signal) {
  if (signal?.aborted) return Promise.reject(signal.reason || new Error('operation_cancelled'));
  return new Promise((resolve, reject) => {
    let timer;
    const cleanup = () => signal?.removeEventListener('abort', onAbort);
    const onResolve = () => {
      cleanup();
      resolve();
    };
    const onAbort = () => {
      clearTimeout(timer);
      cleanup();
      reject(signal.reason || new Error('operation_cancelled'));
    };
    timer = setTimeout(onResolve, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function statusCodeFromError(error) {
  const candidates = [
    error?.output?.statusCode,
    error?.data?.statusCode,
    error?.response?.status,
    error?.statusCode,
    error?.status,
    error?.cause?.output?.statusCode,
    error?.cause?.data?.statusCode,
    error?.cause?.statusCode,
  ];
  for (const value of candidates) {
    const parsed = Number.parseInt(String(value ?? ''), 10);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

async function settleWithTimeout(promise, timeoutMs, label) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve(promise),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label}_timeout`)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

const TRANSPORT_RELEASE_REASONS = new Set([
  'connection_closed',
  'connection_timeout',
  'invalid_session',
  'operation_cancelled',
  'socket_replaced',
  'worker_shutdown',
]);

function waitForSettlementOrAbort(promise, signal) {
  return new Promise((resolve) => {
    let finished = false;
    const cleanup = () => signal?.removeEventListener('abort', onAbort);
    const finish = (outcome) => {
      if (finished) return;
      finished = true;
      cleanup();
      resolve(outcome);
    };
    const onAbort = () => finish({ status: 'cancelled' });

    Promise.resolve(promise).then(
      value => finish({ status: 'fulfilled', value }),
      reason => finish({ status: 'rejected', reason }),
    );
    if (signal?.aborted) onAbort();
    else signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export class WhatsAppDeliveryBlockedError extends Error {
  constructor(code) {
    super(code);
    this.name = 'WhatsAppDeliveryBlockedError';
    this.code = code;
  }
}

export function recordWhatsAppProviderSignal({
  statusCode,
  health,
  config = readWhatsAppSafetyConfig(),
  now = () => Date.now(),
  attempt = 1,
  cancelPending = () => {},
} = {}) {
  const parsedStatus = Number.parseInt(String(statusCode ?? ''), 10);
  if (parsedStatus === 403) {
    health.record('forbidden');
    health.setCircuitOpenUntil(now() + config.forbiddenCircuitOpenMs);
    health.setOperatorPaused(true);
    cancelPending('provider_forbidden');
    return 'forbidden';
  }
  if (parsedStatus === 429) {
    health.record('rate_limited');
    const duration = Math.min(
      config.backoffBaseMs * (2 ** Math.max(0, attempt - 1)),
      config.backoffMaxMs,
    );
    health.setBackoffUntil(now() + duration);
    cancelPending('provider_rate_limited');
    return 'rate_limited';
  }
  return null;
}

/**
 * Adds bounded UX pacing and conservative delivery controls around Baileys.
 * This is operational safety, not a promise to bypass or influence Meta rules.
 */
export function createWhatsAppDeliverySafety({
  sendMessage,
  sendPresenceUpdate = async () => {},
  readMessages = async () => {},
  health,
  config = readWhatsAppSafetyConfig(),
  now = () => Date.now(),
  random = Math.random,
  sleep = defaultSleep,
  logger = console,
  timing = null,
  monotonicNow = () => performance.now(),
} = {}) {
  if (typeof sendMessage !== 'function') throw new TypeError('sendMessage is required');
  if (!health || typeof health.record !== 'function') throw new TypeError('health is required');

  const controllers = new Set();
  const transportControllers = new Set();
  let lastSendStartedAt = 0;
  let consecutiveFailures = 0;
  let backoffAttempt = 0;
  let sendGate = Promise.resolve();
  let cancellationRevision = 0;
  let pendingSends = 0;
  let presenceRevision = 0;
  let activePresence = null;

  function beginTiming(content, context) {
    const startedAt = monotonicNow();
    let recorded = false;
    const sample = {
      trace_id: context?.trace_id,
      response: context?.response,
      component: typeof content?.text === 'string' ? 'text'
        : content?.audio ? 'audio' : content?.video ? 'video' : 'other',
      queue_ms: null,
      pacing_ms: null,
      sdk_ms: null,
    };
    return {
      sample,
      elapsed: since => Math.max(0, monotonicNow() - since),
      queued: () => { sample.queue_ms = Math.max(0, monotonicNow() - startedAt); },
      finish(outcome) {
        if (recorded) return;
        recorded = true;
        try {
          timing?.record({ ...sample, outcome, total_ms: Math.max(0, monotonicNow() - startedAt) });
        } catch {
          // Diagnostics never change delivery or retry an attempted send.
        }
      },
    };
  }

  function timingOutcome(error) {
    if (error?.message === 'send_timeout') return 'uncertain';
    if (error instanceof WhatsAppDeliveryBlockedError) {
      return TRANSPORT_RELEASE_REASONS.has(error.code) ? 'cancelled' : 'blocked';
    }
    return 'failed';
  }

  function jittered(value) {
    if (value <= 0 || config.jitterRatio <= 0) return Math.max(0, Math.round(value));
    const multiplier = 1 + ((random() * 2) - 1) * config.jitterRatio;
    return Math.max(0, Math.round(value * multiplier));
  }

  function controlState() {
    return health.refresh();
  }

  function assertDeliveryAllowed() {
    const state = controlState();
    if (state.operator_paused) throw new WhatsAppDeliveryBlockedError('operator_paused');
    if (state.circuit_open_until) throw new WhatsAppDeliveryBlockedError('circuit_open');
    if (state.backoff_until) throw new WhatsAppDeliveryBlockedError('backoff_active');
  }

  async function waitInterruptibly(totalMs, signal) {
    let remaining = Math.max(0, Math.round(totalMs));
    while (remaining > 0) {
      assertDeliveryAllowed();
      const chunk = Math.min(remaining, 250);
      await sleep(chunk, signal);
      remaining -= chunk;
    }
    assertDeliveryAllowed();
  }

  function controllerForOperation() {
    const controller = new AbortController();
    controllers.add(controller);
    return controller;
  }

  function releaseController(controller) {
    controllers.delete(controller);
  }

  function cancelAll(reason = 'operation_cancelled') {
    cancellationRevision += 1;
    for (const controller of controllers) {
      controller.abort(new WhatsAppDeliveryBlockedError(reason));
    }
    controllers.clear();
    if (TRANSPORT_RELEASE_REASONS.has(reason)) {
      for (const controller of transportControllers) {
        controller.abort(new WhatsAppDeliveryBlockedError(reason));
      }
      transportControllers.clear();
    }
  }

  function refreshControls() {
    const state = controlState();
    if (state.operator_paused || state.circuit_open_until || state.backoff_until) {
      cancelAll(state.operator_paused ? 'operator_paused' : 'delivery_control_active');
    }
    return state;
  }

  async function markRead(messageKey) {
    if (!config.readReceiptsEnabled || !messageKey) return { status: 'disabled' };
    const controller = controllerForOperation();
    try {
      const span = Math.max(0, config.readDelayMaxMs - config.readDelayMinMs);
      const delay = config.readDelayMinMs + random() * span;
      await waitInterruptibly(delay, controller.signal);
      await settleWithTimeout(
        readMessages([messageKey]),
        config.auxiliaryTimeoutMs,
        'read_receipt',
      );
      return { status: 'read' };
    } catch (error) {
      registerProviderSignal(error);
      if (!(error instanceof WhatsAppDeliveryBlockedError)) {
        logger.warn?.('[WA SAFETY] Read receipt could not be sent');
      }
      return { status: 'skipped' };
    } finally {
      releaseController(controller);
    }
  }

  function pacingFor(content) {
    if (typeof content?.text === 'string') {
      const base = config.textDelayMinMs + content.text.length * config.textMsPerCharacter;
      return {
        presence: 'composing',
        delay: jittered(Math.min(Math.max(base, config.textDelayMinMs), config.textDelayMaxMs)),
      };
    }
    if (content?.audio) {
      const bytes = Buffer.isBuffer(content.audio) ? content.audio.length : 0;
      const estimated = bytes > 0 ? (bytes / 16_000) * 1000 : config.audioDelayMinMs;
      const base = Math.min(Math.max(estimated, config.audioDelayMinMs), config.audioDelayMaxMs);
      return { presence: 'recording', delay: jittered(base) };
    }
    if (content?.video) return { presence: 'composing', delay: 0, concurrent: true };
    return { presence: null, delay: 0 };
  }

  function startVideoPresence(jid, signal) {
    const revision = ++presenceRevision;
    activePresence = { jid, revision };
    let stopped = false;
    let updates = 0;
    let compensated = false;
    let renewalTimer;
    let expiryTimer;
    const publish = async presence => {
      let attempted = false;
      let timedOut = false;
      const providerPromise = Promise.resolve().then(() => {
        if (presence === 'composing' && (stopped || signal.aborted)) return;
        if (controlState().delivery_blocked) return;
        if (presence === 'composing' && revision !== presenceRevision) return;
        if (presence === 'paused' && activePresence?.jid === jid && activePresence.revision !== revision) return;
        attempted = true;
        return sendPresenceUpdate(presence, jid);
      });
      if (presence === 'composing') {
        void providerPromise.then(() => {
          if (attempted && stopped && !compensated) {
            compensated = true;
            void publish('paused').catch(() => {});
          }
        }, error => {
          if (timedOut) registerProviderSignal(error);
        }).catch(() => {});
      }
      try {
        await settleWithTimeout(providerPromise, config.auxiliaryTimeoutMs, 'presence');
        return true;
      } catch (error) {
        timedOut = error?.message === 'presence_timeout';
        registerProviderSignal(error);
        logger.warn?.('[WA SAFETY] Video presence update could not be sent');
        return false;
      }
    };
    const stop = () => {
      if (stopped) return;
      stopped = true;
      clearTimeout(renewalTimer);
      clearTimeout(expiryTimer);
      signal.removeEventListener('abort', stop);
      if (activePresence?.revision === revision) activePresence = null;
      // Cleanup is best effort and never delays the next component. Provider
      // restrictions still prevent additional presence traffic.
      if (!controlState().delivery_blocked) void publish('paused').catch(() => {});
    };
    const pulse = async () => {
      if (stopped || signal.aborted || controlState().delivery_blocked) return stop();
      updates += 1;
      if (!await publish('composing')) return stop();
      if (stopped || signal.aborted || controlState().delivery_blocked) return stop();
      if (updates < 6) {
        renewalTimer = setTimeout(() => { void pulse().catch(() => stop()); }, 5_000);
        renewalTimer.unref?.();
      }
    };
    signal.addEventListener('abort', stop, { once: true });
    expiryTimer = setTimeout(stop, Math.min(config.sendTimeoutMs, 30_000));
    expiryTimer.unref?.();
    void pulse().catch(() => stop());
    return stop;
  }

  function registerFailure(error) {
    const status = statusCodeFromError(error);
    consecutiveFailures += 1;
    if (status === 403) {
      registerProviderSignal(error);
      return;
    }
    if (status === 429) {
      backoffAttempt += 1;
      registerProviderSignal(error, backoffAttempt);
      return;
    }
    health.record('delivery_failure');
    const recentFailures = health.recentEventTimes(
      'delivery_failure',
      config.failureWindowMs,
    ).length;
    if (consecutiveFailures >= config.failureThreshold || recentFailures >= config.failureThreshold) {
      health.setCircuitOpenUntil(now() + config.circuitOpenMs);
    }
  }

  function registerProviderSignal(error, attempt = 1) {
    const status = statusCodeFromError(error);
    return recordWhatsAppProviderSignal({
      statusCode: status,
      health,
      config,
      now,
      attempt,
      cancelPending: cancelAll,
    }) !== null;
  }

  async function enforceRateLimit(signal) {
    assertDeliveryAllowed();
    const outgoing = health.recentEventTimes('outgoing', 60_000);
    if (outgoing.length >= config.maxSendsPerMinute) {
      health.record('local_rate_limit');
      health.setBackoffUntil(now() + config.backoffBaseMs);
      throw new WhatsAppDeliveryBlockedError('local_rate_limit');
    }
    const remaining = config.minimumSendIntervalMs - (now() - lastSendStartedAt);
    if (remaining > 0) await waitInterruptibly(remaining, signal);
  }

  async function sendSerialized(jid, content, deliveryTiming) {
    const controller = controllerForOperation();
    const pacing = pacingFor(content);
    let transportController = null;
    let stopVideoPresence = null;
    let presenceOwner = null;
    const pacingStartedAt = monotonicNow();
    try {
      await enforceRateLimit(controller.signal);
      if (
        config.presenceEnabled
        && pacing.presence
        && !pacing.concurrent
        && !controller.signal.aborted
        && !controlState().delivery_blocked
      ) {
        presenceOwner = ++presenceRevision;
        activePresence = { jid, revision: presenceOwner };
        try {
          await settleWithTimeout(
            sendPresenceUpdate(pacing.presence, jid),
            config.auxiliaryTimeoutMs,
            'presence',
          );
        } catch (error) {
          registerProviderSignal(error);
          logger.warn?.('[WA SAFETY] Presence update could not be sent');
        }
      }
      if (pacing.delay > 0) {
        await waitInterruptibly(pacing.delay, controller.signal);
      }

      assertDeliveryAllowed();
      deliveryTiming.sample.pacing_ms = deliveryTiming.elapsed(pacingStartedAt);
      if (config.presenceEnabled && pacing.concurrent) {
        stopVideoPresence = startVideoPresence(jid, controller.signal);
      }
      lastSendStartedAt = now();
      transportController = new AbortController();
      transportControllers.add(transportController);
      const transportPromise = Promise.resolve().then(() => sendMessage(jid, content));
      const sdkStartedAt = monotonicNow();
      try {
        const result = await settleWithTimeout(
          transportPromise,
          config.sendTimeoutMs,
          'send',
        );
        deliveryTiming.sample.sdk_ms = deliveryTiming.elapsed(sdkStartedAt);
        consecutiveFailures = 0;
        backoffAttempt = 0;
        health.record('outgoing');
        return result;
      } catch (error) {
        deliveryTiming.sample.sdk_ms = deliveryTiming.elapsed(sdkStartedAt);
        if (error?.message === 'send_timeout') {
          stopVideoPresence?.();
          deliveryTiming.finish('uncertain');
          // Baileys does not expose cancellation for an in-flight send. Keep
          // the global gate occupied until the transport settles or this
          // socket is explicitly cancelled, otherwise a late delivery could
          // overtake the next response.
          health.record('delivery_failure');
          health.setCircuitOpenUntil(now() + config.circuitOpenMs);
          const finalOutcome = await waitForSettlementOrAbort(
            transportPromise,
            transportController.signal,
          );
          if (finalOutcome.status === 'fulfilled') {
            health.record('outgoing');
          } else if (finalOutcome.status === 'rejected') {
            registerProviderSignal(finalOutcome.reason);
          }
          throw error;
        }
        registerFailure(error);
        throw error;
      }
    } finally {
      stopVideoPresence?.();
      if (deliveryTiming.sample.pacing_ms === null) {
        deliveryTiming.sample.pacing_ms = deliveryTiming.elapsed(pacingStartedAt);
      }
      if (
        config.presenceEnabled
        && pacing.presence
        && !pacing.concurrent
        && !controller.signal.aborted
        && !controlState().delivery_blocked
      ) {
        try {
          await settleWithTimeout(
            sendPresenceUpdate('paused', jid),
            config.auxiliaryTimeoutMs,
            'presence_pause',
          );
        } catch (error) {
          registerProviderSignal(error);
          // Presence is best effort and never changes delivery outcome.
        }
      }
      if (transportController) transportControllers.delete(transportController);
      if (activePresence?.revision === presenceOwner) activePresence = null;
      releaseController(controller);
    }
  }

  function send(jid, content, context = null) {
    const deliveryTiming = beginTiming(content, context);
    try {
      assertDeliveryAllowed();
    } catch (error) {
      deliveryTiming.queued();
      deliveryTiming.finish(timingOutcome(error));
      return Promise.reject(error);
    }
    if (pendingSends >= config.maxPendingSends) {
      health.record('local_rate_limit');
      deliveryTiming.queued();
      deliveryTiming.finish('blocked');
      return Promise.reject(new WhatsAppDeliveryBlockedError('queue_full'));
    }
    pendingSends += 1;
    const previous = sendGate;
    const expectedRevision = cancellationRevision;
    const queuedAt = now();
    let release;
    sendGate = new Promise(resolve => { release = resolve; });
    return (async () => {
      await previous;
      deliveryTiming.queued();
      try {
        if (expectedRevision !== cancellationRevision) {
          throw new WhatsAppDeliveryBlockedError('operation_cancelled');
        }
        if (now() - queuedAt > config.queueWaitTimeoutMs) {
          health.record('local_rate_limit');
          throw new WhatsAppDeliveryBlockedError('queue_timeout');
        }
        return await sendSerialized(jid, content, deliveryTiming);
      } catch (error) {
        deliveryTiming.finish(deliveryTiming.sample.sdk_ms === null ? timingOutcome(error) : 'uncertain');
        throw error;
      } finally {
        deliveryTiming.finish('completed');
        pendingSends -= 1;
        release();
      }
    })();
  }

  function canDeliver() {
    return health.refresh().delivery_blocked !== true;
  }

  return {
    send,
    markRead,
    cancelAll,
    refreshControls,
    canDeliver,
    snapshot: health.snapshot,
  };
}

export const __test = { statusCodeFromError };
