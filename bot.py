"""Bot AutoReply comercial para una cuenta de usuario de Telegram.

Reglas de conversación:
* primera llamada real: mensaje y audio de llamada;
* primer texto o multimedia: Paso 1;
* texto o multimedia posterior: Paso 2, sin límite;
* cualquier interacción posterior, incluida una llamada, recibe Paso 2.

Cada componente de salida usa un identificador MTProto estable y el evento se
confirma después de la entrega, evitando duplicados sin perder reintentos.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
import uuid

from telethon import TelegramClient, errors, events, utils
from telethon.tl import types

from interaction_state import PersistentInteractionState
from language_detection import (
    detect_language_evidence,
    detect_supported_language,
    reload_language_rules,
)
from message_schema import load_message_file
from telegram_audio_branding import (
    brand_audio_attributes,
    build_branded_audio_media,
    resolve_audio_cover_path,
)
from telegram_call_rejection import TelegramCallRejectCoordinator
from telegram_profile_picture import poll_profile_picture_requests
from telegram_events import (
    PHONE_CALL_SUBTYPES,
    incoming_call_discard_request,
    missed_call_search_request,
    missed_call_interaction,
    new_message_interaction,
    phone_call_subtype,
    requested_call_interaction,
    resolve_reply_peer,
)
from telegram_dispatcher import (
    TelegramInteractionDispatcher,
    build_telegram_media_request,
    build_telegram_text_request,
    deliver_telegram_response_components,
)


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
AUDIO_DIR = DATA_DIR / "audios"
VIDEO_DIR = DATA_DIR / "videos"
AUDIO_COVER_PATH = resolve_audio_cover_path(BASE_DIR)
AUDIO_BRANDING_DEFAULTS_FILE = BASE_DIR / "telegram_audio_branding.defaults.json"
AUDIO_BRANDING_SETTINGS_FILE = DATA_DIR / "telegram_audio_branding.json"
MESSAGES_FILE = DATA_DIR / "messages.json"
DEFAULT_MESSAGES_FILE = BASE_DIR / "messages.json"
SESSION_FILE = str(DATA_DIR / "tg_session")
HEALTH_FILE = DATA_DIR / "tg_userbot_health.json"
IDENTITY_FILE = DATA_DIR / "tg_identity.json"
AUTHORIZED_MARKER_FILE = DATA_DIR / "tg_session_authorized.json"
PROFILE_PICTURE_CANONICAL_PATH = BASE_DIR / "assets" / "pecadosvip-profile-logo.png"
PROFILE_PICTURE_PATH = Path(
    os.environ.get("TG_PROFILE_PICTURE_PATH") or PROFILE_PICTURE_CANONICAL_PATH
)
INTERACTION_STATE_FILE = DATA_DIR / "tg_interaction_state.json"
INTERACTION_HEALTH_FILE = DATA_DIR / "tg_interaction_health.json"
TELEGRAM_SEND_TIMEOUT_SECONDS = 20
TELEGRAM_SEND_RETRIES = 3
TELEGRAM_CALL_REJECT_TIMEOUT_SECONDS = 6
TELEGRAM_CALL_REJECT_SETTLE_SECONDS = 0.5
RECENT_CALL_REJECTION_LIMIT = 256


def _load_env_file() -> None:
    """Carga únicamente las credenciales permitidas desde data/.env.local."""
    env_file = DATA_DIR / ".env.local"
    if not env_file.exists():
        return
    with env_file.open("r", encoding="utf-8") as file_handle:
        for raw_line in file_handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in {"TG_API_ID", "TG_API_HASH", "TG_PHONE"} and value:
                os.environ[key] = value


_load_env_file()

API_ID = int(os.environ["TG_API_ID"]) if os.environ.get("TG_API_ID") else None
API_HASH = os.environ.get("TG_API_HASH")
PHONE = os.environ.get("TG_PHONE")
DEFAULT_LANGUAGE = os.environ.get("AUTOREPLY_DEFAULT_LANG", "es").strip().lower()
if not re.fullmatch(r"[a-z]{2,5}", DEFAULT_LANGUAGE):
    DEFAULT_LANGUAGE = "es"

if not API_ID or not API_HASH:
    raise RuntimeError(
        "Credenciales de user bot no configuradas.\n"
        "Configura TG_API_ID, TG_API_HASH y TG_PHONE en el panel."
    )


def load_messages() -> dict:
    data = load_message_file(MESSAGES_FILE, DEFAULT_MESSAGES_FILE)
    result = {}
    for language, language_data in data.items():
        steps = language_data.get("steps", [])
        call_raw = language_data.get(
            "call",
            {"text": "📞 Llamada recibida", "audio": "", "video": ""},
        )
        result[language] = {
            "steps": [
                (
                    step.get("text", ""),
                    step.get("audio", ""),
                    bool(step.get("loop")),
                    step.get("video", ""),
                )
                for step in steps
            ],
            "call": {
                "text": call_raw.get("text", "📞 Llamada recibida"),
                "audio": call_raw.get("audio", ""),
                "video": call_raw.get("video", ""),
            },
        }
    return result


MESSAGES = load_messages()
interaction_state = PersistentInteractionState(
    INTERACTION_STATE_FILE,
    default_language=DEFAULT_LANGUAGE,
)


def detect_lang(text: str) -> str | None:
    return detect_supported_language(text)


def load_messages_fresh() -> None:
    global MESSAGES
    try:
        MESSAGES = load_messages()
        reload_language_rules()
    except Exception:
        logging.exception("No se pudo recargar messages.json; se conserva la versión anterior")


def _language_data(language: str) -> dict:
    if language in MESSAGES:
        return MESSAGES[language]
    if DEFAULT_LANGUAGE in MESSAGES:
        return MESSAGES[DEFAULT_LANGUAGE]
    if "en" in MESSAGES:
        return MESSAGES["en"]
    return next(iter(MESSAGES.values()), {"steps": [], "call": {}})


def get_response_components(language: str, response_key: str) -> tuple[str, str, str]:
    language_data = _language_data(language)
    if response_key == "call":
        call_data = language_data.get("call", {})
        return (
            call_data.get("text", ""),
            call_data.get("audio", ""),
            call_data.get("video", ""),
        )

    steps = language_data.get("steps", [])
    if not steps:
        return "", "", ""
    index = 0 if response_key == "step1" else min(1, len(steps) - 1)
    step = steps[index]
    text = step[0] if len(step) > 0 else ""
    audio = step[1] if len(step) > 1 else ""
    video = step[3] if len(step) > 3 else ""
    return text, audio, video


def get_response_message(language: str, response_key: str) -> tuple[str, str]:
    text, audio, _video = get_response_components(language, response_key)
    return text, audio


client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
self_user_id: int | None = None
call_reject_coordinator = TelegramCallRejectCoordinator(
    limit=RECENT_CALL_REJECTION_LIMIT,
)
interaction_health = {
    "schema_version": 1,
    "worker_revision": str(uuid.uuid4()),
    "connection": "starting",
    "raw_phone_revision": None,
    "phone_subtype": "never",
    "phone_revisions": {subtype: None for subtype in PHONE_CALL_SUBTYPES},
    "call_reject_revision": None,
    "call_reject_status": "never",
    "service_call_revision": None,
    "service_call_status": "never",
    "service_peer_source": "never",
    "missed_call_poll": "starting",
    "classified_revision": None,
    "last_kind": "never",
    "last_response": "never",
    "delivery": {
        "peer_resolution": "never",
        "text": "never",
        "audio": "never",
        "video": "never",
    },
}
_uploaded_media_cache: dict[str, tuple[int, int, object]] = {}


async def _upload_file_cached(file_path: Path) -> object:
    key = str(file_path.resolve())
    stat = file_path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _uploaded_media_cache.get(key)
    if cached is not None and cached[0] == signature[0] and cached[1] == signature[1]:
        return cached[2]
    uploaded = await client.upload_file(str(file_path))
    if len(_uploaded_media_cache) >= 64:
        _uploaded_media_cache.clear()
    _uploaded_media_cache[key] = (signature[0], signature[1], uploaded)
    return uploaded


def _evict_upload_cache(file_path: Path) -> None:
    _uploaded_media_cache.pop(str(file_path.resolve()), None)


def update_interaction_health(**changes) -> None:
    """Persiste únicamente señales operativas sin IDs ni contenido del cliente."""

    interaction_health.update(changes)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary_file = INTERACTION_HEALTH_FILE.with_suffix(".tmp")
        temporary_file.write_text(
            json.dumps(interaction_health, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary_file, INTERACTION_HEALTH_FILE)
    except OSError:
        logging.warning("Telegram interaction health could not be persisted")


def update_delivery_health(stage: str, state: str) -> None:
    delivery = dict(interaction_health["delivery"])
    delivery[stage] = state
    update_interaction_health(delivery=delivery)


async def reject_incoming_call(update) -> str:
    """Reject one incoming call while duplicate updates share the same RPC."""

    request = incoming_call_discard_request(update, self_user_id=self_user_id)
    if request is None:
        return "not_incoming"

    async def discard() -> str:
        update_interaction_health(
            call_reject_revision=str(uuid.uuid4()),
            call_reject_status="pending",
        )
        try:
            await asyncio.wait_for(
                client(request),
                timeout=TELEGRAM_CALL_REJECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            status = "timed_out"
        except (
            errors.CallAlreadyAcceptedError,
            errors.CallAlreadyDeclinedError,
            errors.CallPeerInvalidError,
        ):
            status = "already_finished"
        except (errors.RPCError, OSError):
            status = "failed"
        except Exception as exc:
            logging.warning("Telegram call rejection failed (%s)", type(exc).__name__)
            status = "failed"
        else:
            status = "sent"

        update_interaction_health(call_reject_status=status)
        if status == "sent":
            await asyncio.sleep(TELEGRAM_CALL_REJECT_SETTLE_SECONDS)
        return status

    return await call_reject_coordinator.execute(request.peer.id, discard)


def write_health(ready: bool) -> None:
    """Publica un heartbeat mínimo, sin teléfono ni identidad de la cuenta."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_file = HEALTH_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps({"ready": ready, "updated_at": time.time()}),
        encoding="utf-8",
    )
    os.replace(temporary_file, HEALTH_FILE)
    update_interaction_health(connection="open" if ready else "closed")


def write_authorized_marker() -> None:
    temporary_file = AUTHORIZED_MARKER_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps({"authorized": True, "updated_at": time.time()}),
        encoding="utf-8",
    )
    os.replace(temporary_file, AUTHORIZED_MARKER_FILE)


def write_identity(user) -> None:
    """Publica sólo una identidad mostrable; nunca teléfono ni credenciales."""

    display_name = " ".join(
        part for part in (getattr(user, "first_name", ""), getattr(user, "last_name", ""))
        if isinstance(part, str) and part.strip()
    ).strip()
    username = getattr(user, "username", None)
    payload = {
        "display_name": display_name[:120] or "Cuenta de Telegram",
        "username": username[:64] if isinstance(username, str) and username else None,
        "updated_at": time.time(),
    }
    temporary_file = IDENTITY_FILE.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary_file, IDENTITY_FILE)


async def heartbeat() -> None:
    while True:
        write_health(client.is_connected())
        await asyncio.sleep(15)


async def _retry_telegram_operation(label: str, operation):
    last_error = None
    for attempt in range(1, TELEGRAM_SEND_RETRIES + 1):
        try:
            result = await asyncio.wait_for(
                operation(),
                timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
            )
            update_delivery_health(label.replace("_delivery", ""), "sent")
            return result
        except Exception as exc:
            last_error = exc
            update_delivery_health(label.replace("_delivery", ""), "failed")
            logging.warning(
                "Telegram %s attempt %s/%s failed (%s)",
                label,
                attempt,
                TELEGRAM_SEND_RETRIES,
                type(exc).__name__,
            )
            if attempt < TELEGRAM_SEND_RETRIES:
                await asyncio.sleep(attempt)
    raise RuntimeError(f"telegram_{label}_failed") from last_error


async def send_response(
    delivery_peer: object,
    response_key: str,
    language: str,
    delivery_fingerprint: str,
) -> None:
    load_messages_fresh()
    message_text, audio_file, video_file = get_response_components(language, response_key)
    update_interaction_health(
        last_response=response_key,
        delivery={
            "peer_resolution": "pending",
            "text": "pending" if message_text else "skipped",
            "audio": "pending" if audio_file else "skipped",
            "video": "pending" if video_file else "skipped",
        },
    )
    resolved_peer = await _retry_telegram_operation(
        "peer_resolution",
        lambda: client.get_input_entity(delivery_peer),
    )

    async def send_text_message():
        await _retry_telegram_operation(
            "text_delivery",
            lambda: client(
                build_telegram_text_request(
                    resolved_peer,
                    message_text,
                    delivery_fingerprint,
                )
            ),
        )

    async def send_audio_message():
        safe_audio_name = Path(audio_file).name
        audio_path = AUDIO_DIR / safe_audio_name
        if not safe_audio_name or not audio_path.is_file():
            update_delivery_health("audio", "missing")
            raise FileNotFoundError(f"Configured Telegram audio is missing: {audio_file}")
        audio_attributes, audio_mime_type = utils.get_attributes(
            str(audio_path),
            force_document=False,
            voice_note=False,
        )
        audio_attributes = brand_audio_attributes(
            audio_attributes,
            filename=audio_path.name,
            defaults_path=AUDIO_BRANDING_DEFAULTS_FILE,
            settings_path=AUDIO_BRANDING_SETTINGS_FILE,
        )

        async def send_audio_media():
            uploaded_file = await _upload_file_cached(audio_path)
            uploaded_thumb = None
            if AUDIO_COVER_PATH.is_file():
                try:
                    uploaded_thumb = await _upload_file_cached(AUDIO_COVER_PATH)
                except (OSError, errors.RPCError) as exc:
                    _evict_upload_cache(AUDIO_COVER_PATH)
                    logging.warning(
                        "Telegram audio cover upload failed (%s); sending without it",
                        type(exc).__name__,
                    )

            def media_request(thumb):
                media = build_branded_audio_media(
                    uploaded_file=uploaded_file,
                    uploaded_thumb=thumb,
                    mime_type=audio_mime_type,
                    attributes=audio_attributes,
                )
                return build_telegram_media_request(
                    resolved_peer,
                    media,
                    delivery_fingerprint,
                    component="audio",
                )

            try:
                return await client(media_request(uploaded_thumb))
            except errors.RPCError as exc:
                _evict_upload_cache(audio_path)
                if uploaded_thumb is None:
                    raise
                _evict_upload_cache(AUDIO_COVER_PATH)
                logging.warning(
                    "Telegram rejected the audio cover (%s); retrying without it",
                    type(exc).__name__,
                )
                return await client(media_request(None))

        await _retry_telegram_operation(
            "audio_delivery",
            send_audio_media,
        )

    async def send_video_message():
        safe_video_name = Path(video_file).name
        video_path = VIDEO_DIR / safe_video_name
        if not safe_video_name or not video_path.is_file():
            update_delivery_health("video", "missing")
            raise FileNotFoundError(f"Configured Telegram video is missing: {video_file}")
        video_attributes, video_mime_type = utils.get_attributes(
            str(video_path),
            force_document=False,
            voice_note=False,
            video_note=False,
            supports_streaming=True,
        )

        async def send_video_media():
            uploaded_file = await _upload_file_cached(video_path)
            media = types.InputMediaUploadedDocument(
                file=uploaded_file,
                mime_type=video_mime_type or "video/mp4",
                attributes=list(video_attributes),
                nosound_video=False,
                force_file=False,
            )
            request = build_telegram_media_request(
                resolved_peer,
                media,
                delivery_fingerprint,
                component="video",
            )
            try:
                return await client(request)
            except errors.RPCError:
                _evict_upload_cache(video_path)
                raise

        await _retry_telegram_operation(
            "video_delivery",
            send_video_media,
        )

    await deliver_telegram_response_components(
        response_key,
        send_text=send_text_message if message_text else None,
        send_audio=send_audio_message if audio_file else None,
        send_video=send_video_message if video_file else None,
    )


telegram_dispatcher = TelegramInteractionDispatcher(interaction_state, send_response)


async def process_interaction(
    *,
    chat_id: int,
    event_id: str,
    kind: str,
    detected_language: str | None = None,
    language_evidence: dict[str, object] | None = None,
    provisional_language: str | None = None,
    reply_peer: object | None = None,
) -> None:
    decision = await telegram_dispatcher.dispatch(
        chat_id=chat_id,
        event_id=event_id,
        kind=kind,
        detected_language=detected_language,
        language_evidence=language_evidence,
        provisional_language=provisional_language,
        reply_peer=reply_peer,
    )
    if decision.duplicate:
        logging.info("Telegram duplicate interaction ignored")
        return
    if not decision.persisted:
        logging.warning("Telegram interaction is only stored in memory")
    logging.info(
        "Telegram interaction processed kind=%s phase=%s response=%s lang=%s",
        kind,
        decision.phase,
        decision.response_key,
        decision.language,
    )
    update_interaction_health(
        classified_revision=str(uuid.uuid4()),
        last_kind=kind,
        last_response=decision.response_key or "never",
    )


@client.on(events.NewMessage(incoming=True))
async def handle_message(event) -> None:
    """Maneja una vez cada texto, voz, imagen, documento o multimedia."""
    try:
        reply_peer = await event.get_input_chat()
    except (ValueError, TypeError):
        reply_peer = event.chat_id
    interaction = new_message_interaction(
        event.message,
        chat_id=event.chat_id,
        is_private=event.is_private,
        reply_peer=reply_peer,
    )
    if not interaction:
        return
    language_evidence = (
        detect_language_evidence(interaction.text) if interaction.text else None
    )
    await process_interaction(
        chat_id=interaction.contact_id,
        event_id=interaction.event_id,
        kind=interaction.kind,
        detected_language=(
            language_evidence["language"] if language_evidence else None
        ),
        language_evidence=language_evidence,
        provisional_language="es",
        reply_peer=interaction.reply_peer,
    )


@client.on(events.Raw(types.UpdatePhoneCall))
async def handle_phone_call(update) -> None:
    """Cuenta solicitudes de llamadas reales, no notas de voz."""
    subtype = phone_call_subtype(update)
    revision = str(uuid.uuid4())
    revisions = dict(interaction_health["phone_revisions"])
    revisions[subtype] = revision
    update_interaction_health(
        raw_phone_revision=revision,
        phone_subtype=subtype,
        phone_revisions=revisions,
    )
    interaction = requested_call_interaction(update, self_user_id=self_user_id)
    if not interaction:
        return
    await reject_incoming_call(update)
    reply_peer, peer_source = await resolve_reply_peer(
        client,
        update,
        contact_id=interaction.contact_id,
    )
    update_interaction_health(service_peer_source=peer_source)
    await process_interaction(
        chat_id=interaction.contact_id,
        event_id=interaction.event_id,
        kind=interaction.kind,
        provisional_language="es",
        reply_peer=reply_peer,
    )


@client.on(events.Raw(types.UpdateNewMessage))
async def handle_missed_call_service(update) -> None:
    """Fallback para una llamada perdida recibida después de reconectar."""
    message = getattr(update, "message", None)
    if isinstance(message, types.MessageService) and isinstance(
        getattr(message, "action", None), types.MessageActionPhoneCall
    ):
        update_interaction_health(
            service_call_revision=str(uuid.uuid4()),
            service_call_status="seen",
        )
    interaction = missed_call_interaction(update, self_user_id=self_user_id)
    if not interaction:
        if isinstance(message, types.MessageService) and isinstance(
            getattr(message, "action", None), types.MessageActionPhoneCall
        ):
            update_interaction_health(service_call_status="ignored")
        return
    reply_peer, peer_source = await resolve_reply_peer(
        client,
        update,
        contact_id=interaction.contact_id,
    )
    update_interaction_health(
        service_call_status="classified",
        service_peer_source=peer_source,
    )
    try:
        await process_interaction(
            chat_id=interaction.contact_id,
            event_id=interaction.event_id,
            kind=interaction.kind,
            provisional_language="es",
            reply_peer=reply_peer,
        )
    except Exception:
        update_interaction_health(service_call_status="delivery_failed")
        raise
    update_interaction_health(service_call_status="processed")


async def poll_recent_missed_calls() -> None:
    """Fallback for missed calls whose real-time service update was incomplete."""

    not_before = datetime.now(timezone.utc) - timedelta(seconds=45)
    while True:
        try:
            # ``SearchGlobalRequest`` rejects InputMessagesFilterPhoneCalls.
            # The same filter is supported by messages.search when its peer is
            # InputPeerEmpty, which searches the user's private call history.
            result = await client(missed_call_search_request(not_before))
            entities = {
                utils.get_peer_id(entity): entity
                for entity in (*result.users, *result.chats)
            }
            update_interaction_health(missed_call_poll="healthy")
            for message in reversed(result.messages):
                message_date = getattr(message, "date", None)
                if message_date is None:
                    continue
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                if message_date < not_before:
                    continue
                if hasattr(message, "_finish_init"):
                    message._finish_init(client, entities, None)
                await handle_missed_call_service(
                    SimpleNamespace(message=message, _entities=entities)
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            update_interaction_health(missed_call_poll="failed")
            logging.warning(
                "Telegram missed-call fallback failed (%s)",
                type(exc).__name__,
            )
        await asyncio.sleep(12)


async def main() -> None:
    global self_user_id
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
    )

    while True:
        try:
            logging.info("Starting Telegram User Bot...")
            update_interaction_health(connection="connecting")
            await client.connect()
            if not await client.is_user_authorized():
                AUTHORIZED_MARKER_FILE.unlink(missing_ok=True)
                logging.error(
                    "La sesión de Telegram no está autorizada; completa la vinculación en el panel."
                )
                break

            me = await client.get_me()
            self_user_id = me.id
            write_authorized_marker()
            write_identity(me)
            logging.info("Telegram session authorized; user bot ready.")
            await client.catch_up()
            write_health(True)
            heartbeat_task = asyncio.create_task(heartbeat())
            missed_call_task = asyncio.create_task(poll_recent_missed_calls())
            profile_picture_task = asyncio.create_task(
                poll_profile_picture_requests(
                    client,
                    me,
                    DATA_DIR,
                    PROFILE_PICTURE_PATH,
                    canonical_image_path=PROFILE_PICTURE_CANONICAL_PATH,
                )
            )
            try:
                await client.run_until_disconnected()
            finally:
                update_interaction_health(connection="closed")
                heartbeat_task.cancel()
                missed_call_task.cancel()
                profile_picture_task.cancel()
                try:
                    await asyncio.gather(
                        heartbeat_task,
                        missed_call_task,
                        profile_picture_task,
                    )
                except asyncio.CancelledError:
                    pass
                try:
                    HEALTH_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                await client.disconnect()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.warning(
                "Telegram UserBot disconnected or encountered error (%s); reconnecting in 5s",
                type(exc).__name__,
            )
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())

