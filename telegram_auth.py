"""Flujo web seguro y consistente para autorizar una cuenta de Telegram.

Telethon exige que un cliente conectado permanezca en el mismo event loop.
El panel recibe el código en una petición HTTP distinta, así que este manager
mantiene un único loop en segundo plano durante todo el intento de acceso.
"""

from __future__ import annotations

import asyncio
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class AuthOutcome:
    """Resultado público y, sólo en memoria, credenciales ya verificadas."""

    public: dict[str, Any]
    credentials: tuple[int, str, str] | None = None
    attempt_token: str | None = None
    qr_url: str | None = field(default=None, repr=False)


_DELIVERY_LABELS = {
    "SentCodeTypeApp": "una sesión activa de Telegram",
    "SentCodeTypeSms": "un SMS",
    "SentCodeTypeCall": "una llamada telefónica",
    "SentCodeTypeFlashCall": "una llamada flash",
    "SentCodeTypeMissedCall": "una llamada perdida",
    "SentCodeTypeFragmentSms": "Fragment SMS",
    "SentCodeTypeFirebaseSms": "una notificación/SMS de Telegram",
    "SentCodeTypeEmailCode": "el correo asociado a la cuenta",
    "SentCodeTypeSmsPhrase": "un SMS con frase",
    "SentCodeTypeSmsWord": "un SMS con palabra",
    "SentCodeTypeSetUpEmailRequired": "la configuración de correo de Telegram",
    "CodeTypeSms": "SMS",
    "CodeTypeCall": "llamada telefónica",
    "CodeTypeFlashCall": "llamada flash",
    "CodeTypeMissedCall": "llamada perdida",
    "CodeTypeFragmentSms": "Fragment SMS",
}


def describe_sent_code(sent_code: Any) -> dict[str, Any]:
    """Convierte el tipo TL de Telegram en datos públicos y comprensibles."""

    delivery_type = type(getattr(sent_code, "type", None)).__name__
    next_type = type(getattr(sent_code, "next_type", None)).__name__
    timeout = getattr(sent_code, "timeout", None)
    length = getattr(getattr(sent_code, "type", None), "length", None)

    delivery = _DELIVERY_LABELS.get(delivery_type, "el canal elegido por Telegram")
    next_delivery = _DELIVERY_LABELS.get(next_type) if next_type != "NoneType" else None
    try:
        timeout_seconds = max(0, int(timeout or 0))
    except (TypeError, ValueError):
        timeout_seconds = 0

    result: dict[str, Any] = {
        "delivery": delivery,
        "delivery_type": delivery_type or "unknown",
        "timeout_seconds": timeout_seconds,
    }
    if next_delivery:
        result["next_delivery"] = next_delivery
    if isinstance(length, int) and 1 <= length <= 12:
        result["code_length"] = length
    return result


def _error_outcome(exc: Exception) -> AuthOutcome:
    """Mapea errores de Telethon sin filtrar detalles internos o secretos."""

    name = type(exc).__name__
    if name == "FloodWaitError":
        seconds = max(1, int(getattr(exc, "seconds", 60) or 60))
        return AuthOutcome({
            "ok": False,
            "error_code": "flood_wait",
            "retry_after": seconds,
            "error": f"Telegram exige esperar {seconds} segundos antes de solicitar otro código.",
        })

    errors = {
        "ApiIdInvalidError": ("invalid_api_credentials", "El api_id o api_hash no es válido."),
        "PhoneNumberInvalidError": ("invalid_phone", "El número no es válido. Usa formato internacional, por ejemplo +57..."),
        "PhoneNumberBannedError": ("phone_banned", "Telegram no permite iniciar sesión con esta cuenta."),
        "PhoneNumberFloodError": ("phone_flood", "Telegram bloqueó temporalmente nuevos intentos para este número."),
        "PhoneCodeInvalidError": ("invalid_code", "El código no es válido. Revisa el mensaje más reciente de Telegram."),
        "PhoneCodeExpiredError": ("expired_code", "El código venció. Cancela este intento y solicita uno nuevo."),
        "PasswordHashInvalidError": ("invalid_password", "La contraseña de verificación en dos pasos no es correcta."),
        "AuthRestartError": ("auth_restart", "Telegram pidió reiniciar la autorización. Solicita un código nuevo."),
    }
    code, message = errors.get(
        name,
        ("telegram_error", "Telegram no pudo completar la operación. Intenta nuevamente en un momento."),
    )
    return AuthOutcome({"ok": False, "error_code": code, "error": message})


class TelegramAuthManager:
    """Mantiene el cliente de autenticación en un único loop/hilo."""

    def __init__(
        self,
        session_file: str,
        *,
        client_factory: Callable[[str, int, str], Any] | None = None,
        request_timeout: int = 35,
        pending_ttl: int = 600,
    ) -> None:
        self.session_file = session_file
        self.client_factory = client_factory
        self.request_timeout = request_timeout
        self.pending_ttl = pending_ttl
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._operation_lock: asyncio.Lock | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop

            self._ready.clear()
            loop = asyncio.new_event_loop()

            def run_loop() -> None:
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._operation_lock = None
                self._ready.set()
                loop.run_forever()

            thread = threading.Thread(
                target=run_loop,
                name="telegram-auth-loop",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            if not self._ready.wait(timeout=5):
                raise RuntimeError("No fue posible iniciar el gestor de autenticación")
            return loop

    def _submit(self, coroutine: Any) -> AuthOutcome:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._serialized(coroutine), loop)
        try:
            return future.result(timeout=self.request_timeout)
        except TimeoutError:
            future.cancel()
            async def drain_cancellation() -> None:
                await asyncio.sleep(0)

            try:
                asyncio.run_coroutine_threadsafe(drain_cancellation(), loop).result(timeout=2)
            except Exception:
                pass
            return AuthOutcome({
                "ok": False,
                "error_code": "request_timeout",
                "error": "Telegram tardó demasiado en responder. Espera un momento antes de reintentar.",
            })

    async def _serialized(self, coroutine: Any) -> AuthOutcome:
        # Evita crear dos clientes sobre el mismo archivo o consumir un QR dos veces.
        if self._operation_lock is None:
            self._operation_lock = asyncio.Lock()
        started = False
        try:
            async with self._operation_lock:
                started = True
                return await coroutine
        finally:
            if not started:
                coroutine.close()

    def _new_client(self, api_id: int, api_hash: str) -> Any:
        if self.client_factory is not None:
            return self.client_factory(self.session_file, api_id, api_hash)
        from telethon import TelegramClient

        return TelegramClient(self.session_file, api_id, api_hash)

    async def _disconnect(self, client: Any) -> None:
        try:
            await client.disconnect()
        except Exception:
            pass

    async def _clear_pending(self) -> None:
        pending, self._pending = self._pending, None
        expiry_handle = pending.get("expiry_handle") if pending else None
        if expiry_handle is not None:
            expiry_handle.cancel()
        task = pending.get("qr_task") if pending else None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if pending and pending.get("client") is not None:
            if pending.get("kind") == "qr" and not pending.get("qr_committed"):
                await self._revoke_qr(pending)
            await self._disconnect(pending["client"])
            if pending.get("kind") == "qr" and not pending.get("qr_committed"):
                self._remove_uncommitted_qr_session()

    def _remove_uncommitted_qr_session(self) -> None:
        for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
            Path(self.session_file + suffix).unlink(missing_ok=True)

    async def _revoke_qr(self, pending: dict[str, Any]) -> None:
        """No deja una sesión nueva utilizable tras cancelar o escanear otra cuenta."""
        client = pending["client"]
        try:
            if await client.is_user_authorized():
                await asyncio.wait_for(client.log_out(), timeout=5)
        except Exception:
            pass

    async def _expire_pending(self, attempt_token: str) -> None:
        """Cierra de forma autónoma un reto abandonado al vencer su TTL."""

        pending = self._pending
        if not pending:
            return
        expected = pending.get("attempt_token") or ""
        if not expected or not secrets.compare_digest(expected, attempt_token):
            return
        if time.monotonic() >= pending["expires_at"]:
            await self._clear_pending()

    def _arm_expiry(self, pending: dict[str, Any], expires_in: float) -> None:
        previous = pending.get("expiry_handle")
        if previous is not None:
            previous.cancel()
        token = pending["attempt_token"]
        pending["expiry_handle"] = asyncio.get_running_loop().call_later(
            expires_in,
            lambda: asyncio.create_task(self._expire_pending(token)),
        )

    def has_pending(self) -> bool:
        """Indica si existe un reto vigente sin exponer sus datos internos."""

        outcome = self._submit(self._pending_status())
        return bool(outcome.public.get("pending"))

    async def _pending_status(self) -> AuthOutcome:
        return AuthOutcome({"ok": True, "pending": bool(await self._get_pending())})

    async def _get_pending(
        self,
        attempt_token: str | None = None,
        *,
        require_token: bool = False,
    ) -> dict[str, Any] | None:
        pending = self._pending
        if not pending:
            return None
        if time.monotonic() > pending["expires_at"]:
            await self._clear_pending()
            return None
        if require_token:
            expected = pending.get("attempt_token") or ""
            supplied = attempt_token if isinstance(attempt_token, str) else ""
            if not expected or not secrets.compare_digest(expected, supplied):
                return None
        return pending

    def begin(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        attempt_token: str | None = None,
    ) -> AuthOutcome:
        return self._submit(self._begin(api_id, api_hash, phone, attempt_token))

    async def _begin(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        attempt_token: str | None,
    ) -> AuthOutcome:
        existing = await self._get_pending()
        if existing:
            same_request = (
                existing["api_id"] == api_id
                and existing["api_hash"] == api_hash
                and existing["phone"] == phone
            )
            owns_request = (
                isinstance(attempt_token, str)
                and bool(attempt_token)
                and secrets.compare_digest(existing["attempt_token"], attempt_token)
            )
            if owns_request and same_request and existing.get("kind") == "qr":
                await self._clear_pending()
                return await self._begin(api_id, api_hash, phone, None)
            if not owns_request or not same_request:
                remaining = max(
                    1,
                    math.ceil(max(existing["resend_at"], time.monotonic() + 1) - time.monotonic()),
                )
                return AuthOutcome({
                    "ok": True,
                    "needs_code": True,
                    "request_in_progress": True,
                    "owned_by_this_browser": False,
                    "retry_after": remaining,
                })
            if time.monotonic() < existing["resend_at"]:
                remaining = max(1, math.ceil(existing["resend_at"] - time.monotonic()))
                return AuthOutcome({
                    "ok": True,
                    "needs_code": True,
                    "request_in_progress": True,
                    "retry_after": remaining,
                    **existing["delivery"],
                })
            if same_request:
                try:
                    sent_code = await existing["client"].send_code_request(phone)
                    phone_code_hash = getattr(sent_code, "phone_code_hash", None)
                    if not phone_code_hash:
                        return AuthOutcome({
                            "ok": False,
                            "error_code": "missing_code_challenge",
                            "error": "Telegram no devolvió un reto de verificación válido.",
                        })
                    delivery = describe_sent_code(sent_code)
                    existing.update({
                        "phone_code_hash": phone_code_hash,
                        "delivery": delivery,
                        "expires_at": time.monotonic() + max(
                            self.pending_ttl,
                            delivery["timeout_seconds"] + 120,
                        ),
                        "resend_at": time.monotonic() + max(30, delivery["timeout_seconds"]),
                    })
                    self._arm_expiry(
                        existing,
                        max(self.pending_ttl, delivery["timeout_seconds"] + 120),
                    )
                    return AuthOutcome({"ok": True, "needs_code": True, "resent": True, **delivery})
                except asyncio.CancelledError:
                    await self._clear_pending()
                    raise
                except Exception as exc:
                    outcome = _error_outcome(exc)
                    if outcome.public.get("error_code") == "flood_wait":
                        existing["resend_at"] = time.monotonic() + int(
                            outcome.public["retry_after"]
                        )
                        existing["expires_at"] = max(
                            existing["expires_at"],
                            existing["resend_at"] + 120,
                        )
                    return outcome
            await self._clear_pending()

        client = self._new_client(api_id, api_hash)
        try:
            await client.connect()
            if await client.is_user_authorized():
                await self._disconnect(client)
                return AuthOutcome(
                    {"ok": True, "already_authorized": True},
                    (api_id, api_hash, phone),
                )

            sent_code = await client.send_code_request(phone)
            phone_code_hash = getattr(sent_code, "phone_code_hash", None)
            if not phone_code_hash:
                await self._disconnect(client)
                return AuthOutcome({
                    "ok": False,
                    "error_code": "missing_code_challenge",
                    "error": "Telegram no devolvió un reto de verificación válido.",
                })

            delivery = describe_sent_code(sent_code)
            expires_in = max(self.pending_ttl, delivery["timeout_seconds"] + 120)
            self._pending = {
                "kind": "code",
                "client": client,
                "phone_code_hash": phone_code_hash,
                "phone": phone,
                "api_id": api_id,
                "api_hash": api_hash,
                "expires_at": time.monotonic() + expires_in,
                "resend_at": time.monotonic() + max(30, delivery["timeout_seconds"]),
                "delivery": delivery,
                "attempt_token": secrets.token_urlsafe(32),
            }
            self._arm_expiry(self._pending, expires_in)
            return AuthOutcome(
                {"ok": True, "needs_code": True, **delivery},
                attempt_token=self._pending["attempt_token"],
            )
        except asyncio.CancelledError:
            await self._disconnect(client)
            raise
        except Exception as exc:
            if self._pending and self._pending.get("client") is client:
                await self._clear_pending()
            else:
                await self._disconnect(client)
            return _error_outcome(exc)

    @staticmethod
    def _qr_account_matches(user: Any, phone: str) -> bool:
        actual = re.sub(r"\D", "", str(getattr(user, "phone", "") or ""))
        expected = re.sub(r"\D", "", phone)
        return bool(actual and actual == expected and not getattr(user, "bot", False))

    @staticmethod
    def _wrong_qr_account() -> AuthOutcome:
        return AuthOutcome({
            "ok": False,
            "error_code": "qr_account_mismatch",
            "error": "El QR se aceptó con otra cuenta. Selecciona en Telegram la cuenta del teléfono indicado y genera un QR nuevo.",
        })

    def begin_qr(
        self, api_id: int, api_hash: str, phone: str,
        attempt_token: str | None = None,
    ) -> AuthOutcome:
        return self._submit(self._begin_qr(api_id, api_hash, phone, attempt_token))

    async def _begin_qr(
        self, api_id: int, api_hash: str, phone: str, attempt_token: str | None,
    ) -> AuthOutcome:
        existing = await self._get_pending()
        if existing:
            owned = (
                isinstance(attempt_token, str)
                and secrets.compare_digest(existing["attempt_token"], attempt_token)
            )
            same = (existing["api_id"], existing["api_hash"], existing["phone"]) == (
                api_id, api_hash, phone,
            )
            if not owned or not same:
                return AuthOutcome({
                    "ok": False, "error_code": "auth_in_progress",
                    "error": "Ya hay una autorización pendiente. Continúa o cancélala en la pestaña que la inició.",
                })
            if existing.get("kind") == "qr":
                return await self._qr_status(attempt_token)
            await self._clear_pending()

        client = self._new_client(api_id, api_hash)
        try:
            await client.connect()
            if await client.is_user_authorized():
                user = await client.get_me()
                await self._disconnect(client)
                if not self._qr_account_matches(user, phone):
                    return self._wrong_qr_account()
                return AuthOutcome({"ok": True, "already_authorized": True},
                                   (api_id, api_hash, phone))
            qr = await client.qr_login()
            pending = {
                "kind": "qr", "client": client, "qr": qr,
                "api_id": api_id, "api_hash": api_hash, "phone": phone,
                "attempt_token": secrets.token_urlsafe(32),
                "expires_at": time.monotonic() + self.pending_ttl,
                "resend_at": time.monotonic(), "qr_state": "waiting",
            }
            self._pending = pending
            self._arm_expiry(pending, self.pending_ttl)
            pending["qr_task"] = asyncio.create_task(self._watch_qr(pending))
            # Telethon exige que wait() esté escuchando antes de mostrar el QR.
            await asyncio.sleep(0)
            outcome = self._qr_snapshot(pending)
            if pending["qr_state"] == "error":
                await self._clear_pending()
                return outcome
            return AuthOutcome(outcome.public, attempt_token=pending["attempt_token"],
                               qr_url=outcome.qr_url)
        except asyncio.CancelledError:
            if self._pending and self._pending.get("client") is client:
                await self._clear_pending()
            else:
                await self._disconnect(client)
            raise
        except Exception as exc:
            if self._pending and self._pending.get("client") is client:
                await self._clear_pending()
            else:
                await self._disconnect(client)
            return _error_outcome(exc)

    async def _watch_qr(self, pending: dict[str, Any]) -> None:
        try:
            while self._pending is pending:
                try:
                    user = await pending["qr"].wait()
                except asyncio.TimeoutError:
                    if self._pending is not pending:
                        return
                    await pending["qr"].recreate()
                    continue
                if self._pending is not pending:
                    return
                if not self._qr_account_matches(user, pending["phone"]):
                    pending["qr_error"] = self._wrong_qr_account().public
                    pending["qr_state"] = "error"
                    await self._revoke_qr(pending)
                    await self._disconnect(pending["client"])
                    self._remove_uncommitted_qr_session()
                else:
                    pending["qr_state"] = "authorized"
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pending is pending:
                if type(exc).__name__ == "SessionPasswordNeededError":
                    pending["qr_state"] = "password"
                else:
                    pending["qr_error"] = _error_outcome(exc).public
                    pending["qr_state"] = "error"
                    await self._revoke_qr(pending)
                    await self._disconnect(pending["client"])
                    self._remove_uncommitted_qr_session()

    def _qr_snapshot(self, pending: dict[str, Any]) -> AuthOutcome:
        if pending["qr_state"] == "password":
            return AuthOutcome({"ok": False, "needs_password": True})
        if pending["qr_state"] == "error":
            return AuthOutcome(dict(pending["qr_error"]))
        if pending["qr_state"] != "waiting":
            return AuthOutcome({"ok": True, "waiting": True})
        qr = pending["qr"]
        expires_in = max(0, math.ceil(qr.expires.timestamp() - time.time()))
        return AuthOutcome({"ok": True, "needs_qr": True, "expires_in": expires_in},
                           qr_url=qr.url)

    def qr_status(self, attempt_token: str | None) -> AuthOutcome:
        return self._submit(self._qr_status(attempt_token))

    async def _qr_status(self, attempt_token: str | None) -> AuthOutcome:
        pending = await self._get_pending(attempt_token, require_token=True)
        if not pending or pending.get("kind") != "qr":
            return AuthOutcome({
                "ok": False, "error_code": "invalid_auth_attempt",
                "error": "El intento QR venció o pertenece a otra pestaña. Genera un QR nuevo.",
            })
        if pending["qr_state"] == "authorized":
            pending["qr_state"] = "finishing"
            return await self._finish_qr(pending)
        outcome = self._qr_snapshot(pending)
        if pending["qr_state"] == "error":
            await self._clear_pending()
        return outcome

    async def _finish_qr(self, pending: dict[str, Any]) -> AuthOutcome:
        try:
            client = pending["client"]
            user = await client.get_me()
            if self._pending is not pending:
                return AuthOutcome({"ok": False, "error_code": "invalid_auth_attempt",
                                    "error": "El intento QR ya no está activo."})
            if not await client.is_user_authorized() or not self._qr_account_matches(user, pending["phone"]):
                await self._clear_pending()
                return self._wrong_qr_account()
            credentials = (pending["api_id"], pending["api_hash"], pending["phone"])
            pending["qr_committed"] = True
            await self._clear_pending()
            return AuthOutcome({"ok": True}, credentials)
        except asyncio.CancelledError:
            await self._clear_pending()
            raise
        except Exception as exc:
            await self._clear_pending()
            return _error_outcome(exc)

    def verify_code(self, code: str, attempt_token: str | None = None) -> AuthOutcome:
        return self._submit(self._verify_code(code, attempt_token))

    async def _verify_code(self, code: str, attempt_token: str | None) -> AuthOutcome:
        pending = await self._get_pending(attempt_token, require_token=True)
        if not pending:
            return AuthOutcome({
                "ok": False,
                "error_code": "invalid_auth_attempt",
                "error": "Este navegador no posee el intento activo. Solicita un código nuevo.",
            })

        client = pending["client"]
        if pending.get("kind") == "qr":
            return AuthOutcome({"ok": False, "error_code": "wrong_auth_method",
                                "error": "Este intento se confirma escaneando el QR en Telegram."})
        try:
            await client.sign_in(
                phone=pending["phone"],
                code=code,
                phone_code_hash=pending["phone_code_hash"],
            )
            if not await client.is_user_authorized():
                await self._clear_pending()
                return AuthOutcome({
                    "ok": False,
                    "error_code": "not_authorized",
                    "error": "Telegram no confirmó la sesión.",
                })

            credentials = (pending["api_id"], pending["api_hash"], pending["phone"])
            await self._clear_pending()
            return AuthOutcome({"ok": True}, credentials)
        except asyncio.CancelledError:
            await self._clear_pending()
            raise
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeededError":
                return AuthOutcome({"ok": False, "needs_password": True})
            outcome = _error_outcome(exc)
            if outcome.public.get("error_code") in {"expired_code", "auth_restart", "telegram_error"}:
                await self._clear_pending()
            return outcome

    def verify_password(self, password: str, attempt_token: str | None = None) -> AuthOutcome:
        return self._submit(self._verify_password(password, attempt_token))

    async def _verify_password(self, password: str, attempt_token: str | None) -> AuthOutcome:
        pending = await self._get_pending(attempt_token, require_token=True)
        if not pending:
            return AuthOutcome({
                "ok": False,
                "error_code": "invalid_auth_attempt",
                "error": "Este navegador no posee el intento activo. Solicita un código nuevo.",
            })

        client = pending["client"]
        if pending.get("kind") == "qr" and pending["qr_state"] != "password":
            return AuthOutcome({"ok": False, "error_code": "wrong_auth_method",
                                "error": "Primero acepta el QR en Telegram."})
        try:
            await client.sign_in(password=password)
            if pending.get("kind") == "qr":
                pending["qr_state"] = "finishing"
                return await self._finish_qr(pending)
            if not await client.is_user_authorized():
                await self._clear_pending()
                return AuthOutcome({
                    "ok": False,
                    "error_code": "not_authorized",
                    "error": "Telegram no confirmó la sesión.",
                })

            credentials = (pending["api_id"], pending["api_hash"], pending["phone"])
            await self._clear_pending()
            return AuthOutcome({"ok": True}, credentials)
        except asyncio.CancelledError:
            await self._clear_pending()
            raise
        except Exception as exc:
            outcome = _error_outcome(exc)
            if outcome.public.get("error_code") not in {"invalid_password"}:
                await self._clear_pending()
            return outcome

    def cancel(self, attempt_token: str | None = None) -> AuthOutcome:
        return self._submit(self._cancel(attempt_token))

    async def _cancel(self, attempt_token: str | None) -> AuthOutcome:
        pending = await self._get_pending(attempt_token, require_token=True)
        if not pending:
            return AuthOutcome({
                "ok": False,
                "error_code": "invalid_auth_attempt",
                "error": "No se puede cancelar un intento de otro navegador.",
            })
        await self._clear_pending()
        return AuthOutcome({"ok": True})
