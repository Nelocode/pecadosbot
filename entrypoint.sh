#!/bin/bash
# ─── Entrypoint: lanza los 3 procesos del Bot AutoReply ────────────────
# ─────────────────────────────────────────────────────────────────────────

echo "╔══════════════════════════════════════════════╗"
echo "║   Bot AutoReply — Iniciando servicios       ║"
echo "╚══════════════════════════════════════════════╝"

cd /app

# Validate ownership before copying defaults or loading any persisted settings.
python -c 'from instance_paths import resolve_instance_data_directory; resolve_instance_data_directory("/app")' || exit 1

# ── 0. Instalar dependencias faltantes si es necesario ───────────────
if [ ! -d /app/node_modules/@whiskeysockets/baileys ]; then
    npm ci --omit=dev --ignore-scripts
fi

# ── 0b. Preparar directorio de datos persistente ─────────────────────
mkdir -p /app/data/audios /app/data/videos /app/data/wa_auth
# Copiar defaults si no existen en data/
[ -f /app/data/messages.json ] || cp /app/messages.json /app/data/messages.json
[ -f /app/data/.env.local ] || touch /app/data/.env.local
python /app/message_schema.py /app/data/messages.json /app/messages.json
# Sembrar sólo los audios que falten; nunca sobrescribir los personalizados.
for source_audio in /app/audios/*; do
    [ -f "$source_audio" ] || continue
    destination_audio="/app/data/audios/$(basename "$source_audio")"
    [ -f "$destination_audio" ] || cp "$source_audio" "$destination_audio"
done
if [ -d /app/videos ]; then
    for source_video in /app/videos/*; do
        [ -f "$source_video" ] || continue
        destination_video="/app/data/videos/$(basename "$source_video")"
        [ -f "$destination_video" ] || cp "$source_video" "$destination_video"
    done
fi
# Corregir únicamente el M4A histórico conocido que tenía extensión .mp3.
# El migrador compara hashes, crea respaldo y no toca audios personalizados.
python /app/audio_migrations.py /app/data/audios /app/audios

# ── 1. Cargar sólo credenciales conocidas, sin ejecutar .env.local ──
if [ -f /app/data/.env.local ]; then
    while IFS='=' read -r env_key env_value; do
        env_key="${env_key#${env_key%%[![:space:]]*}}"
        env_key="${env_key%${env_key##*[![:space:]]}}"
        env_value="${env_value%$'\r'}"
        case "$env_key" in
            TG_API_ID|TG_API_HASH|TG_PHONE|AUTOREPLY_BOT_TOKEN)
                if [[ "$env_value" == \"*\" && "$env_value" == *\" ]]; then
                    env_value="${env_value:1:${#env_value}-2}"
                elif [[ "$env_value" == \'*\' && "$env_value" == *\' ]]; then
                    env_value="${env_value:1:${#env_value}-2}"
                fi
                export "$env_key=$env_value"
                ;;
        esac
    done < /app/data/.env.local
fi

# ── 2. Bot Telegram (User Bot - Telethon) ────────────────────────────
if [ -n "$TG_API_ID" ] && [ -n "$TG_API_HASH" ] \
   && [ -f /app/data/tg_session.session ] \
   && [ -f /app/data/tg_session_authorized.json ]; then
    echo "📱 Iniciando Bot Telegram (User Bot)..."
    nohup env -u PANEL_ADMIN_RECOVERY_KEY python bot.py > /app/data/bot_tg.log 2>&1 &
    echo $! > /app/data/tg_userbot.pid
    echo "  → PID: $!"
elif [ -n "$TG_API_ID" ] && [ -n "$TG_API_HASH" ]; then
    echo "⚠️  Credenciales de Telegram presentes, pero falta autorizar la sesión desde el panel."
else
    echo "⚠️  Credenciales de user bot no configuradas. Configura api_id, api_hash y phone desde el panel."
fi

# ── 3. Bot Telegram (BotFather - Bot API) ───────────────────────────
if [ -n "$AUTOREPLY_BOT_TOKEN" ]; then
    echo "🤖 Iniciando BotFather Bot..."
    nohup env -u PANEL_ADMIN_RECOVERY_KEY python botfather_bot.py > /app/data/bot_bf.log 2>&1 &
    echo $! > /app/data/botfather.pid
    echo "  → PID: $!"
else
    echo "⚠️  AUTOREPLY_BOT_TOKEN no configurado. El BotFather bot no arrancará."
fi

# ── 4. Bot WhatsApp ──────────────────────────────────────────────────
if [ -d "/app/data/wa_auth" ] && [ "$(ls -A /app/data/wa_auth 2>/dev/null)" ]; then
    echo "💬 Iniciando Bot WhatsApp..."
    nohup env -u PANEL_ADMIN_RECOVERY_KEY node wa_bot.mjs > /app/data/bot_wa.log 2>&1 &
    echo $! > /app/data/wa_bot.pid
    echo "  → PID: $!"
else
    echo "⚠️  WhatsApp no vinculado. Se vincula desde el panel."
fi

# ── 5. Panel Admin ───────────────────────────────────────────────────
echo "🖥️  Iniciando Panel Admin..."
exec gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 --access-logfile - --error-logfile - --timeout 120 app:app
