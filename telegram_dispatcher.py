"""Ordered per-chat delivery for Telegram interactions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
import hashlib

from telethon.tl import functions

from interaction_state import InteractionDecision, PersistentInteractionState
from telegram_delivery_journal import TelegramVideoDeliveryUncertain


telegram_queue_wait_ms = ContextVar("telegram_queue_wait_ms", default=0.0)


def interaction_delivery_fingerprint(chat_id: int, event_id: str) -> str:
    """Return an opaque, stable delivery key without exposing Telegram IDs."""

    material = f"telegram-delivery-v1\0{chat_id}\0{event_id}".encode(
        "utf-8",
        errors="strict",
    )
    return hashlib.sha256(material).hexdigest()


def telegram_delivery_random_id(interaction_fingerprint: str, component: str) -> int:
    """Derive Telegram's signed 64-bit idempotency ID for one component."""

    material = (
        f"telegram-random-id-v1\0{interaction_fingerprint}\0{component}"
    ).encode("utf-8", errors="strict")
    value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)
    return value or 1


def build_telegram_text_request(
    peer: object,
    message: str,
    interaction_fingerprint: str,
):
    return functions.messages.SendMessageRequest(
        peer=peer,
        message=message,
        random_id=telegram_delivery_random_id(interaction_fingerprint, "text"),
    )


def build_telegram_media_request(
    peer: object,
    media: object,
    interaction_fingerprint: str,
    *,
    component: str = "audio",
):
    return functions.messages.SendMediaRequest(
        peer=peer,
        media=media,
        message="",
        random_id=telegram_delivery_random_id(interaction_fingerprint, component),
    )


async def deliver_telegram_response_components(
    response_key: str,
    *,
    send_text: Callable[[], Awaitable[object]] | None,
    send_audio: Callable[[], Awaitable[object]] | None,
    send_video: Callable[[], Awaitable[object]] | None = None,
) -> None:
    """Deliver one response in the channel-specific presentation order.

    Configured videos lead every response, followed by text and optional audio.
    Without video, calls retain audio-then-text and steps text-then-audio.
    The component senders keep using their stable per-interaction ``random_id``;
    therefore replaying this orchestration after a partial/ambiguous failure is
    idempotent at Telegram's MTProto boundary.
    """

    first_error: Exception | None = None
    if send_video is not None:
        senders = (send_video, send_text, send_audio)
    elif response_key == "call":
        senders = (send_audio, send_text)
    else:
        senders = (send_text, send_audio)
    for sender in senders:
        if sender is None:
            continue
        try:
            await sender()
        except TelegramVideoDeliveryUncertain:
            # A possible video delivery must settle before follow-up components.
            raise
        except Exception as exc:
            # A failed component must not suppress the remaining configured media.
            # Re-raising after all attempts keeps the interaction uncommitted,
            # allowing the stable component random_ids to make retries safe.
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


class TelegramInteractionDispatcher:
    def __init__(
        self,
        state: PersistentInteractionState,
        send_response: Callable[[object, str, str, str], Awaitable[None]],
        *,
        on_committed: Callable[[str], None] | None = None,
    ) -> None:
        self.state = state
        self.send_response = send_response
        self.on_committed = on_committed
        self._locks: dict[int, asyncio.Lock] = {}

    def _prune_idle_locks(self, active_chat_id: int) -> None:
        if len(self._locks) <= 1024:
            return
        for key in list(self._locks.keys()):
            if key == active_chat_id:
                continue
            candidate = self._locks.get(key)
            if candidate is not None and not candidate.locked():
                self._locks.pop(key, None)
                if len(self._locks) <= 512:
                    break

    async def dispatch(
        self,
        *,
        chat_id: int,
        event_id: str,
        kind: str,
        detected_language: str | None = None,
        language_evidence: dict[str, object] | None = None,
        provisional_language: str | None = None,
        reply_peer: object | None = None,
    ) -> InteractionDecision:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        queued_at = asyncio.get_running_loop().time()
        try:
            async with lock:
                decision = self.state.preview(
                    contact_id=chat_id,
                    event_id=event_id,
                    kind=kind,
                    detected_language=detected_language,
                    language_evidence=language_evidence,
                    provisional_language=provisional_language,
                )
                if not decision.duplicate and decision.response_key:
                    delivery_fingerprint = interaction_delivery_fingerprint(chat_id, event_id)
                    queue_token = telegram_queue_wait_ms.set(
                        max(0.0, (asyncio.get_running_loop().time() - queued_at) * 1000)
                    )
                    try:
                        await self.send_response(
                            reply_peer if reply_peer is not None else chat_id,
                            decision.response_key,
                            decision.language,
                            delivery_fingerprint,
                        )
                    finally:
                        telegram_queue_wait_ms.reset(queue_token)
                    registered = self.state.register(
                        contact_id=chat_id,
                        event_id=event_id,
                        kind=kind,
                        detected_language=detected_language,
                        language_evidence=language_evidence,
                        provisional_language=provisional_language,
                    )
                    if registered.persisted and self.on_committed is not None:
                        try:
                            self.on_committed(delivery_fingerprint)
                        except TelegramVideoDeliveryUncertain:
                            # Keep confirmed evidence if cleanup fails; delivery succeeded.
                            pass
                    return registered
                return decision
        finally:
            self._prune_idle_locks(chat_id)
