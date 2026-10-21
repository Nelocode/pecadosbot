"""Apply panel-requested branding to a verified Telegram user account.

Starting the worker is not consent to change a profile. Only the panel writes a
short-lived request after a successful link. An exclusive, durable result file
claims each request before any Telegram call, so uncertain writes are not retried.
"""

import asyncio
from contextlib import contextmanager
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import time
import uuid


REQUEST_FILE = "tg_profile_picture_request.json"
RESULTS_DIR = "tg_profile_picture_results"
REQUEST_TTL_SECONDS = 600
UPLOAD_TIMEOUT_SECONDS = 30
CLAIM_GRACE_SECONDS = 15
RESULT_LIMIT = 32

_MESSAGES = {
    "not_requested": "La foto de Telegram se solicitará al completar una vinculación nueva.",
    "disabled": "La actualización de la foto de Telegram está desactivada.",
    "pending": "El logotipo de Telegram está pendiente de aplicar.",
    "applying": "Se está aplicando el logotipo de Telegram.",
    "applied": "El logotipo de PecadosVIP se aplicó y se verificó en Telegram.",
    "failed": "No se pudo aplicar el logotipo de Telegram.",
    "unverified": "No se pudo confirmar la foto en Telegram. No se repetirá automáticamente.",
    "expired": "La solicitud de foto de Telegram caducó sin aplicarse.",
    "image_changed": "El logotipo cambió después de solicitar la foto; no se aplicó.",
    "image_unavailable": "El archivo del logotipo de Telegram no está disponible o no es válido.",
    "invalid_account": "No se pudo verificar la cuenta para actualizar su foto.",
    "superseded": "Una vinculación posterior sustituyó esta solicitud de foto.",
    "storage_failed": "No se pudo guardar de forma segura la solicitud de foto de Telegram.",
    "unresolved_limit": "Hay solicitudes de foto sin resolver; no se iniciará otra actualización.",
}


def _enabled():
    return os.environ.get("TG_PROFILE_PICTURE_ENABLED", "1") != "0"


def _public(status, reason=None, updated_at=None):
    if not isinstance(status, str):
        status = "unverified"
    message_key = reason if isinstance(reason, str) and reason else status
    result = {"status": status, "message": _MESSAGES.get(message_key, _MESSAGES["failed"])}
    if isinstance(updated_at, (int, float)) and math.isfinite(updated_at):
        result["updated_at"] = updated_at
    return result


def _account_hash(phone):
    value = str(phone or "").strip()
    if not re.fullmatch(r"\+?[0-9 ()-]+", value):
        return None
    digits = re.sub(r"[^0-9]", "", value)
    if not 7 <= len(digits) <= 15:
        return None
    return hashlib.sha256(digits.encode("ascii")).hexdigest()


def _image_bytes(image_path):
    content = Path(image_path).read_bytes()
    if not content.startswith(b"\x89PNG\r\n\x1a\n") or len(content) > 5 * 1024 * 1024:
        raise ValueError("Invalid profile image")
    return content


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _request(data_dir):
    value = _read_json(Path(data_dir) / REQUEST_FILE)
    if not value or not re.fullmatch(r"[a-f0-9]{32}", str(value.get("request_id", ""))):
        return None
    if value.get("status") != "pending":
        return None
    if not all(re.fullmatch(r"[a-f0-9]{64}", str(value.get(key, ""))) for key in ("account_hash", "image_hash")):
        return None
    requested_at = value.get("requested_at")
    if not isinstance(requested_at, (int, float)) or not math.isfinite(requested_at):
        return None
    return value


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _result_path(data_dir, request):
    return Path(data_dir) / RESULTS_DIR / (request["request_id"] + ".json")


def _result_status(path):
    value = _read_json(path)
    if not value:
        return _public("unverified")
    status = value.get("status")
    updated_at = value.get("updated_at")
    if not isinstance(status, str):
        return _public("unverified")
    if status == "applying":
        if not isinstance(updated_at, (int, float)) or not math.isfinite(updated_at) or not 0 <= time.time() - updated_at <= UPLOAD_TIMEOUT_SECONDS + CLAIM_GRACE_SECONDS:
            return _public("unverified", updated_at=updated_at)
    elif status not in {"applied", "failed", "unverified"}:
        return _public("unverified")
    return _public(status, value.get("reason"), updated_at)


def _unresolved_status(data_dir, account_hash):
    if not account_hash:
        return None
    for path in (Path(data_dir) / RESULTS_DIR).glob("*.json"):
        result = _read_json(path)
        if not result:
            # Its account cannot be recovered, so replacing it is not safe.
            return _public("unverified")
        if result.get("account_hash") == account_hash:
            status = _result_status(path)
            if status["status"] in {"applying", "unverified"}:
                return status
    return None


def _applied_status(data_dir, account_hash, image_hash):
    for path in (Path(data_dir) / RESULTS_DIR).glob("*.json"):
        result = _read_json(path)
        if result and result.get("status") == "applied" and result.get("account_hash") == account_hash and result.get("image_hash") == image_hash:
            return _result_status(path)
    return None


def public_profile_picture_status(data_dir, phone, image_path):
    """Return only status/message/time, scoped to the selected account and logo."""
    if not _enabled():
        return _public("disabled")
    account_hash = _account_hash(phone)
    unresolved = _unresolved_status(data_dir, account_hash)
    if unresolved:
        return unresolved
    request = _request(data_dir)
    result_path = _result_path(data_dir, request) if request and request["account_hash"] == account_hash else None
    if result_path and result_path.exists():
        status = _result_status(result_path)
        if status["status"] in {"applying", "unverified"}:
            return status
    try:
        image_hash = hashlib.sha256(_image_bytes(image_path)).hexdigest()
    except (OSError, ValueError):
        return _public("failed", "image_unavailable")
    applied = _applied_status(data_dir, account_hash, image_hash)
    if applied:
        return applied
    if not result_path:
        return _public("not_requested")
    if request["image_hash"] != image_hash:
        return _public("failed", "image_changed")
    if result_path.exists():
        return _result_status(result_path)
    if not 0 <= time.time() - request["requested_at"] <= REQUEST_TTL_SECONDS:
        return _public("failed", "expired", request["requested_at"])
    return _public("pending", updated_at=request["requested_at"])


@contextmanager
def _queue_lock(data_dir):
    """Short OS lock: survives concurrent callbacks, releases automatically on exit."""
    path = Path(data_dir) / "tg_profile_picture_queue.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def request_profile_picture(data_dir, phone, image_path):
    """Queue branding only after the panel has completed a successful account link."""
    if not _enabled():
        return _public("disabled")
    if not _account_hash(phone):
        return _public("failed", "invalid_account")
    try:
        _image_bytes(image_path)
        with _queue_lock(data_dir):
            return _request_profile_picture_locked(data_dir, phone, image_path)
    except ValueError:
        return _public("failed", "image_unavailable")
    except OSError:
        return _public("failed", "storage_failed")


def _request_profile_picture_locked(data_dir, phone, image_path):
    account_hash = _account_hash(phone)
    if not account_hash:
        return _public("failed", "invalid_account")
    try:
        image_hash = hashlib.sha256(_image_bytes(image_path)).hexdigest()
    except (OSError, ValueError):
        return _public("failed", "image_unavailable")
    previous = _request(data_dir)
    # Preserve uncertainty even if another account was linked in the meantime.
    unresolved = _unresolved_status(data_dir, account_hash)
    if unresolved:
        return unresolved
    applied = _applied_status(data_dir, account_hash, image_hash)
    if applied:
        return applied
    if previous and previous["account_hash"] == account_hash:
        previous_result = _result_path(data_dir, previous)
        if previous_result.exists():
            status = _result_status(previous_result)
            # Never turn a repeated callback into a retry of an uncertain write,
            # even if the asset changed in the meantime.
            if status["status"] in {"applying", "unverified"}:
                return status
        elif previous["image_hash"] == image_hash and 0 <= time.time() - previous["requested_at"] <= REQUEST_TTL_SECONDS:
            return _public("pending", updated_at=previous["requested_at"])
    request = {
        "request_id": uuid.uuid4().hex,
        "account_hash": account_hash,
        "image_hash": image_hash,
        "requested_at": time.time(),
        "status": "pending",
    }
    try:
        _prune_results(data_dir, previous["request_id"] if previous else request["request_id"])
        if len(list((Path(data_dir) / RESULTS_DIR).glob("*.json"))) >= RESULT_LIMIT:
            return _public("failed", "unresolved_limit")
        _atomic_json(Path(data_dir) / REQUEST_FILE, request)
    except OSError:
        return _public("failed", "storage_failed")
    return _public("pending", updated_at=request["requested_at"])


def _prune_results(data_dir, active_id):
    directory = Path(data_dir) / RESULTS_DIR
    if not directory.exists():
        return
    files = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[RESULT_LIMIT // 2:]:
        result = _read_json(path)
        if path.stem != active_id and result and result.get("status") in ("applied", "failed"):
            path.unlink(missing_ok=True)


def _claim(path, request):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump({"status": "applying", "account_hash": request["account_hash"], "image_hash": request["image_hash"], "updated_at": time.time()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except FileExistsError:
        return False


def _photo_request(uploaded):
    # Lazy import keeps panel status reads and offline tests independent of a client.
    from telethon.tl.functions.photos import UploadProfilePhotoRequest

    return UploadProfilePhotoRequest(file=uploaded)


async def process_profile_picture(client, me, data_dir, image_path, *, timeout_seconds=UPLOAD_TIMEOUT_SECONDS):
    """Consume one matching panel request; never create a request or retry a claim."""
    if not _enabled():
        return _public("disabled")
    request = _request(data_dir)
    if not request or getattr(me, "bot", False) or request["account_hash"] != _account_hash(getattr(me, "phone", None)):
        return _public("not_requested")
    result_path = _result_path(data_dir, request)
    try:
        if not _claim(result_path, request):
            return _result_status(result_path)
    except OSError:
        return _public("failed", "storage_failed")

    def finish(status, reason=None):
        result = {"status": status, "reason": reason, "account_hash": request["account_hash"], "image_hash": request["image_hash"], "updated_at": time.time()}
        try:
            _atomic_json(result_path, result)
        except OSError:
            # The durable claim remains: a possibly applied photo is never retried.
            return _public("unverified")
        return _public(status, reason, result["updated_at"])

    if not 0 <= time.time() - request["requested_at"] <= REQUEST_TTL_SECONDS:
        return finish("failed", "expired")
    try:
        content = _image_bytes(image_path)
    except (OSError, ValueError):
        return finish("failed", "image_unavailable")
    if hashlib.sha256(content).hexdigest() != request["image_hash"]:
        return finish("failed", "image_changed")

    phase = "upload"

    async def apply_and_verify():
        nonlocal phase
        image = io.BytesIO(content)
        image.name = "pecadosvip-profile-logo.png"
        uploaded = await client.upload_file(image)
        current_request = _request(data_dir)
        if not _enabled() or not current_request or current_request["request_id"] != request["request_id"]:
            return "failed", "superseded"
        phase = "apply"
        response = await client(_photo_request(uploaded))
        phase = "verify"
        photo_id = getattr(getattr(response, "photo", None), "id", None)
        if not photo_id:
            return "unverified", None
        photos = await client.get_profile_photos(me, limit=1)
        if not photos or getattr(photos[0], "id", None) != photo_id:
            return "unverified", None
        return "applied", None

    try:
        status, reason = await asyncio.wait_for(apply_and_verify(), timeout=min(timeout_seconds, UPLOAD_TIMEOUT_SECONDS))
        return finish(status, reason)
    except asyncio.CancelledError:
        finish("unverified")
        raise
    except asyncio.TimeoutError:
        return finish("unverified")
    except Exception:
        return finish("failed" if phase == "upload" else "unverified")


async def poll_profile_picture_requests(client, me, data_dir, image_path, *, canonical_image_path=None):
    """Run independently of user-bot heartbeat and message handling."""
    while True:
        try:
            if canonical_image_path is None or _image_bytes(image_path) == _image_bytes(canonical_image_path):
                await process_profile_picture(client, me, data_dir, image_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning("Telegram profile picture worker failed (%s)", type(exc).__name__)
        await asyncio.sleep(5)
