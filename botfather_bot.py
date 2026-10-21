"""
BotFather Bot — Telegram (Bot API)
Usa python-telegram-bot. Misma lógica de idioma y pasos que el user bot.
Requiere AUTOREPLY_BOT_TOKEN. Corre en paralelo con bot.py (Telethon).
"""
import os
import json
import time
import logging
import hashlib
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from botfather_language_state import (
    apply_message_language_evidence,
    detect_supported_language,
)
from language_detection import reload_language_rules
from message_schema import load_message_file
from telegram_audio_branding import resolve_audio_branding

# ── Config ────────────────────────────────────────────────────────────
from instance_paths import resolve_instance_data_directory

BASE_DIR = Path(__file__).parent
DATA_DIR = resolve_instance_data_directory(BASE_DIR)
AUDIO_DIR = DATA_DIR / "audios"
VIDEO_DIR = DATA_DIR / "videos"
AUDIO_BRANDING_DEFAULTS_FILE = BASE_DIR / "telegram_audio_branding.defaults.json"
AUDIO_BRANDING_SETTINGS_FILE = DATA_DIR / "telegram_audio_branding.json"
MESSAGES_FILE = DATA_DIR / "messages.json"
DEFAULT_MESSAGES_FILE = BASE_DIR / "messages.json"
RESET_TIMEOUT = int(float(os.environ.get("AUTOREPLY_CONVERSATION_TTL_SECONDS", "86400")))
MAX_USER_STATE_ENTRIES = 10_000

BOT_TOKEN = os.environ.get("AUTOREPLY_BOT_TOKEN")

# ── Intentar cargar de .env.local si no está en environment ────────
def _load_env_file():
    env_file = DATA_DIR / ".env.local"
    if not env_file.exists():
        return
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key == "AUTOREPLY_BOT_TOKEN" and val:
                os.environ[key] = val

_load_env_file()
BOT_TOKEN = os.environ.get("AUTOREPLY_BOT_TOKEN")  # Re-leer

if not BOT_TOKEN:
    logging.warning("AUTOREPLY_BOT_TOKEN not set. BotFather bot will not start.")
    # Import stays safe for diagnostics/tests; main() performs the no-token exit.

# ── Mensajes ───────────────────────────────────────────────────────────
def load_messages() -> dict:
    source = MESSAGES_FILE if MESSAGES_FILE.is_file() else DEFAULT_MESSAGES_FILE
    data = load_message_file(source, DEFAULT_MESSAGES_FILE)
    result = {}
    for lang, lang_data in data.items():
        steps = lang_data.get("steps", [])
        call_raw = lang_data.get("call", {"text": "📞 Llamada recibida", "audio": "", "video": ""})
        result[lang] = {
            "steps": [
                (
                    s.get("text", ""),
                    s.get("audio", ""),
                    bool(s.get("loop", False)),
                    s.get("video", ""),
                )
                for s in steps
            ],
            "call": {
                "text": call_raw.get("text", "📞 Llamada recibida"),
                "audio": call_raw.get("audio", ""),
                "video": call_raw.get("video", ""),
            },
        }
    return result

MESSAGES = load_messages()

# ── Estado por usuario ────────────────────────────────────────────────
user_state: dict[int, dict] = {}


def _prune_user_state(keep_chat_id: int | None = None) -> None:
    if len(user_state) <= MAX_USER_STATE_ENTRIES:
        return
    excess = len(user_state) - MAX_USER_STATE_ENTRIES
    candidates = sorted(
        (
            (float(data.get("last_seen", 0)), cid)
            for cid, data in user_state.items()
            if cid != keep_chat_id
        ),
        key=lambda item: item[0],
    )
    for _, cid in candidates[:excess]:
        user_state.pop(cid, None)


def _claim_message(state: dict, message: object) -> bool:
    """Claim one Bot API message before language/step mutation."""

    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return True
    event_key = hashlib.sha256(
        f"botfather-event\0{message_id}".encode("utf-8", errors="strict")
    ).hexdigest()
    recent = state.setdefault("recent_events", [])
    if event_key in recent:
        return False
    recent.append(event_key)
    del recent[:-256]
    return True

def detect_lang(text: str) -> str | None:
    return detect_supported_language(text)


def is_expired(state: dict) -> bool:
    return time.time() - state.get("last_seen", 0) > RESET_TIMEOUT


def load_messages_fresh():
    global MESSAGES
    try:
        MESSAGES = load_messages()
        reload_language_rules()
    except Exception:
        pass


# ── Handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silent start — no welcome, user's first message triggers lang detection."""
    chat_id = update.effective_chat.id
    if chat_id in user_state:
        del user_state[chat_id]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    now = time.time()
    state = user_state.get(chat_id)

    if state is None or is_expired(state):
        if state is not None:
            logging.info("BotFather conversation cycle expired")
        load_messages_fresh()
        preserved = {}
        if isinstance(state, dict):
            for key in (
                "lang",
                "language_provisional",
                "language_source",
                "language_candidate",
                "language_candidate_streak",
            ):
                if key in state:
                    preserved[key] = state[key]
        state = {"step": 0, "last_seen": now, **preserved}
        if not _claim_message(state, update.message):
            return
        apply_message_language_evidence(state, update.message)
        user_state[chat_id] = state
        _prune_user_state(chat_id)
        step_to_use = 0
    else:
        if not _claim_message(state, update.message):
            logging.info("BotFather duplicate interaction ignored")
            return
        apply_message_language_evidence(state, update.message)
        step_to_use = min(state["step"] + 1, len(MESSAGES.get(state["lang"], MESSAGES["en"])["steps"]) - 1)
        state["last_seen"] = now

    lang = state["lang"]
    lang_data = MESSAGES.get(lang, MESSAGES["en"])
    step_tuple = lang_data["steps"][step_to_use]
    msg_text, audio_file, is_loop = step_tuple[0], step_tuple[1], step_tuple[2]
    video_file = step_tuple[3] if len(step_tuple) > 3 else ""

    if not is_loop:
        state["step"] = step_to_use

    if msg_text:
        await update.message.reply_text(msg_text)

    if audio_file:
        safe_audio = Path(audio_file).name
        audio_path = AUDIO_DIR / safe_audio
        if safe_audio and audio_path.is_file():
            title, performer = resolve_audio_branding(
                defaults_path=AUDIO_BRANDING_DEFAULTS_FILE,
                settings_path=AUDIO_BRANDING_SETTINGS_FILE,
            )
            with open(audio_path, "rb") as f:
                await update.message.reply_audio(
                    audio=f,
                    title=title,
                    performer=performer,
                )

    if video_file and hasattr(update.message, "reply_video"):
        safe_video = Path(video_file).name
        video_path = VIDEO_DIR / safe_video
        if safe_video and video_path.is_file():
            with open(video_path, "rb") as vf:
                await update.message.reply_video(
                    video=vf,
                    supports_streaming=True,
                )

    logging.info(
        "BotFather message processed lang=%s step=%s",
        lang,
        step_to_use,
    )


async def handle_call(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Voice/video note treated as call."""
    chat_id = update.effective_chat.id
    logging.info("BotFather voice/video interaction received")

    state = user_state.get(chat_id)
    now = time.time()
    if state is None or is_expired(state):
        load_messages_fresh()
        preserved = {}
        if isinstance(state, dict):
            for key in (
                "lang",
                "language_provisional",
                "language_source",
                "language_candidate",
                "language_candidate_streak",
            ):
                if key in state:
                    preserved[key] = state[key]
        state = {"step": 0, "last_seen": now, **preserved}
        if not _claim_message(state, update.message):
            return
        apply_message_language_evidence(state, update.message)
        user_state[chat_id] = state
        _prune_user_state(chat_id)
    else:
        if not _claim_message(state, update.message):
            logging.info("BotFather duplicate interaction ignored")
            return
        apply_message_language_evidence(state, update.message)
        state["last_seen"] = now
    lang = state["lang"]

    lang_data = MESSAGES.get(lang, MESSAGES["en"])
    call_data = lang_data.get("call", {"text": "📞 Llamada recibida", "audio": "", "video": ""})
    msg_text = call_data.get("text", "📞 Llamada recibida")
    audio_file = call_data.get("audio", "")
    video_file = call_data.get("video", "")

    if msg_text:
        try:
            await update.message.reply_text(msg_text)
        except Exception:
            pass

    if audio_file:
        safe_audio = Path(audio_file).name
        audio_path = AUDIO_DIR / safe_audio
        if safe_audio and audio_path.is_file():
            title, performer = resolve_audio_branding(
                defaults_path=AUDIO_BRANDING_DEFAULTS_FILE,
                settings_path=AUDIO_BRANDING_SETTINGS_FILE,
            )
            with open(audio_path, "rb") as f:
                try:
                    await update.message.reply_audio(
                        audio=f,
                        title=title,
                        performer=performer,
                    )
                except Exception:
                    pass

    if video_file and hasattr(update.message, "reply_video"):
        safe_video = Path(video_file).name
        video_path = VIDEO_DIR / safe_video
        if safe_video and video_path.is_file():
            with open(video_path, "rb") as vf:
                try:
                    await update.message.reply_video(
                        video=vf,
                        supports_streaming=True,
                    )
                except Exception:
                    pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("[BF] Handler error (%s)", type(context.error).__name__)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        format="%(asctime)s [BF %(levelname)s] %(message)s",
        level=logging.INFO,
    )

    if not BOT_TOKEN:
        logging.warning("No AUTOREPLY_BOT_TOKEN — BotFather bot exiting.")
        return

    while True:
        try:
            app = Application.builder().token(BOT_TOKEN).build()

            app.add_handler(CommandHandler("start", start))
            content_filter = (
                filters.TEXT
                | (filters.ATTACHMENT & ~filters.VOICE & ~filters.VIDEO_NOTE)
            ) & ~filters.COMMAND
            app.add_handler(MessageHandler(content_filter, handle_message))
            app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO_NOTE, handle_call))
            app.add_error_handler(error_handler)

            logging.info("BotFather bot starting")
            app.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            logging.error(
                "[BF] BotFather bot crashed (%s); restarting in 5s",
                type(e).__name__,
            )
            time.sleep(5)


if __name__ == "__main__":
    main()

