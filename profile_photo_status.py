"""Read-only, account-scoped status for the WhatsApp profile photo worker."""
import hashlib
import json
import math
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


def _pending_whatsapp_request_status(request_path: Path, account_hash: str, image_hash: str):
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if (not isinstance(request, dict)
                or not re.fullmatch(r"[0-9a-f]{64}", str(request.get("accountHash", "")))):
            return None, {"status": "failed", "message": "No se pudo verificar la solicitud de foto guardada."}
        if request.get("accountHash") != account_hash:
            return None, None
        if (request.get("brand") != "pecadosvip" or request.get("status") != "pending"
                or not re.fullmatch(r"[0-9a-f]{32}", str(request.get("requestId", "")))
                or request.get("imageHash") != image_hash):
            return None, {"status": "failed", "message": "La solicitud de foto no corresponde a esta marca y este logotipo."}
        requested = request.get("requestedAt")
        expires = request.get("expiresAt")
        if (not isinstance(requested, (int, float)) or not isinstance(expires, (int, float))
                or not math.isfinite(requested) or not math.isfinite(expires)
                or expires - requested != 600_000
                or not requested <= time.time() * 1000 <= expires):
            return request["requestId"], {"status": "failed", "message": "La solicitud de foto caducó sin una confirmación de WhatsApp."}
        return request["requestId"], {"status": "pending", "message": "El logotipo de PecadosVIP está pendiente para la cuenta recién vinculada."}
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, TypeError):
        return None, {"status": "failed", "message": "No se pudo verificar la solicitud de foto guardada."}


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
        image_hash = hashlib.sha256(official_path.read_bytes()).hexdigest()
        request_id, pending = _pending_whatsapp_request_status(
            state_path.parent / "wa_profile_picture_request.json", account_hash, image_hash,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "unknown", "message": "No se pudo verificar la cuenta para consultar su foto."}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("version") != 1:
            raise ValueError("unknown version")
        record = state["accounts"][account_hash]
        if not isinstance(record, dict):
            raise ValueError("invalid record")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return pending or {"status": "unknown", "message": "Todavía no hay un resultado comprobable para esta cuenta y este logotipo."}

    status = record.get("status")
    retry_after = record.get("retryAfter")
    cooldown = (status == "failed" and isinstance(retry_after, (int, float))
                and math.isfinite(retry_after) and time.time() * 1000 < retry_after)
    # An unresolved operation or cooldown blocks the whole account, even when
    # the new request has a different image. Never hide it behind new pending.
    blocked = status in ("pending", "uncertain") or cooldown
    if pending and (request_id is None or request_id != record.get("requestId")) and not blocked:
        return pending
    if record.get("imageHash") != image_hash and not blocked:
        return pending or {"status": "unknown", "message": "Todavía no hay un resultado comprobable para esta cuenta y este logotipo."}
    if status == "pending":
        attempted = record.get("attemptedAt")
        if not isinstance(attempted, (int, float)) or not 0 <= time.time() * 1000 - attempted <= 75_000:
            status = "uncertain"
    if status == "applied" and record.get("verification") == "provider_acknowledged":
        return {"status": "applied", "message": "WhatsApp confirmó la última actualización registrada del logotipo; la foto actual no se ha consultado.", "verification": "provider_acknowledged"}
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
