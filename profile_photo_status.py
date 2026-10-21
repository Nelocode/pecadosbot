"""Read-only, account-scoped status for the WhatsApp profile photo worker."""
import hashlib
import json
import re
import time
from pathlib import Path


def official_image_matches(image_path: Path, official_path: Path) -> bool:
    try:
        return hashlib.sha256(image_path.read_bytes()).digest() == hashlib.sha256(
            official_path.read_bytes()
        ).digest()
    except OSError:
        return False


def whatsapp_profile_picture_status(
    identity_path: Path, state_path: Path, image_path: Path, official_path: Path,
    *, enabled: bool = True,
) -> dict:
    if not enabled:
        return {"status": "disabled", "message": "La foto automática está desactivada."}
    if not official_image_matches(image_path, official_path):
        return {"status": "failed", "message": "La imagen configurada no coincide con el logotipo oficial de PecadosVIP o no está disponible."}
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        account_hash = identity.get("account_hash", "")
        if not isinstance(account_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", account_hash):
            raise ValueError("unknown account")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("version") != 1:
            raise ValueError("unknown version")
        record = state["accounts"][account_hash]
        if record.get("imageHash") != hashlib.sha256(official_path.read_bytes()).hexdigest():
            raise ValueError("different image")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "unknown", "message": "Todavía no hay un resultado comprobable para esta cuenta y este logotipo."}

    status = record.get("status")
    if status == "pending":
        attempted = record.get("attemptedAt")
        if not isinstance(attempted, (int, float)) or not 0 <= time.time() * 1000 - attempted <= 75_000:
            status = "uncertain"
    if status == "applied" and record.get("verification") == "provider_acknowledged":
        return {"status": "applied", "message": "WhatsApp confirmó la actualización del logotipo. La foto puede tardar en aparecer en otros dispositivos.", "verification": "provider_acknowledged"}
    messages = {
        "pending": "Aplicando el logotipo de PecadosVIP…",
        "uncertain": "No se pudo confirmar la actualización. Revisa la foto en WhatsApp antes de volver a intentarlo.",
        "failed": "WhatsApp no aceptó la actualización del logotipo. La cuenta sigue vinculada.",
        "missing": "No se encontró el logotipo oficial para actualizar la foto.",
    }
    if status == "failed" and record.get("errorCode") == "rate_limited":
        return {"status": "failed", "message": "WhatsApp limitó las actualizaciones de foto. La cuenta sigue vinculada; se respetará la espera indicada por el proveedor."}
    if isinstance(status, str) and status in messages:
        return {"status": status, "message": messages[status]}
    return {"status": "unknown", "message": "No hay confirmación de WhatsApp para este logotipo."}
