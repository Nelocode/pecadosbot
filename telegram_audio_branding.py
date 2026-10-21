"""Small, deterministic helpers for branded Telegram audio messages."""

from __future__ import annotations

import json
import hashlib
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
import unicodedata

from telethon.tl import types

try:
    from hachoir.core import config as hachoir_config
except ImportError:  # Optional locally; installed in the production image.
    hachoir_config = None
else:
    # Some generated MP3s contain large C2PA/GEOB records. Their warnings do
    # not affect duration extraction and would otherwise flood worker logs.
    hachoir_config.quiet = True


AUDIO_TITLE = "PecadosVIP"
AUDIO_PERFORMER = "PecadosVIP"
AUDIO_BRANDING_MAX_LENGTH = 80
_LEGACY_BRAND_LABELS = frozenset({"las fiesteras", "caché madrid"})
_LEGACY_COVER_SHA256 = frozenset({
    "e6f37056f908ff6289972ef349fb07373e9676e2bae187835af563ffecc7427f",
    "8df2135ee1fba375ec455c44ed4db0320351c120818c8dda0b46e18a4391a364",
})


def _environment_text(
    environ: Mapping[str, str],
    name: str,
    default: str,
) -> str:
    value = environ.get(name, "")
    if not isinstance(value, str):
        return default
    return value.strip() or default


def _branding_text(value: object) -> str | None:
    """Return a safe Telegram label or ``None`` for an unusable value."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > AUDIO_BRANDING_MAX_LENGTH:
        return None
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        return None
    return normalized


def _legacy_brand_label(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return normalized in _LEGACY_BRAND_LABELS


def _instance_branding_text(value: object) -> str | None:
    """Ignore only the inherited brand; preserve unrelated custom labels."""
    return None if _legacy_brand_label(value) else _branding_text(value)


def _read_branding_file(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        field: normalized
        for field in ("title", "performer")
        if (normalized := _instance_branding_text(parsed.get(field))) is not None
    }


def resolve_audio_branding(
    *,
    environ: Mapping[str, str] | None = None,
    defaults_path: str | Path | None = None,
    settings_path: str | Path | None = None,
) -> tuple[str, str]:
    """Resolve packaged, environment and panel labels in precedence order."""

    source = os.environ if environ is None else environ
    resolved = {"title": AUDIO_TITLE, "performer": AUDIO_PERFORMER}
    resolved.update(_read_branding_file(defaults_path))
    environment_title = _instance_branding_text(source.get("TG_AUDIO_TITLE"))
    if environment_title is not None:
        resolved["title"] = environment_title
    environment_performer = _instance_branding_text(source.get("TG_AUDIO_PERFORMER"))
    if environment_performer is not None:
        resolved["performer"] = environment_performer
    resolved.update(_read_branding_file(settings_path))
    return resolved["title"], resolved["performer"]


def save_audio_branding_settings(
    settings_path: str | Path,
    *,
    title: object,
    performer: object,
) -> dict[str, str]:
    """Validate and atomically persist both labels edited in the panel."""

    if _legacy_brand_label(title) or _legacy_brand_label(performer):
        raise ValueError("La marca heredada no corresponde a PecadosVIP; usa sus textos o una personalización propia.")
    normalized_title = _branding_text(title)
    if normalized_title is None:
        raise ValueError(
            f"El título debe tener entre 1 y {AUDIO_BRANDING_MAX_LENGTH} caracteres visibles."
        )
    normalized_performer = _branding_text(performer)
    if normalized_performer is None:
        raise ValueError(
            f"El nombre de la agencia debe tener entre 1 y {AUDIO_BRANDING_MAX_LENGTH} caracteres visibles."
        )

    destination = Path(settings_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "title": normalized_title,
        "performer": normalized_performer,
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return payload


def resolve_audio_cover_path(
    base_dir: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a deployment-specific cover relative to the application root."""

    source = os.environ if environ is None else environ
    application_root = Path(base_dir)
    official_cover = application_root / "assets" / "audio-cover.jpg"
    configured = _environment_text(source, "TG_AUDIO_COVER_PATH", "")
    if not configured:
        return official_cover

    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = application_root / candidate
    try:
        if hashlib.sha256(candidate.read_bytes()).hexdigest() in _LEGACY_COVER_SHA256:
            return official_cover
    except OSError:
        pass  # Preserve the worker's existing handling of an unavailable override.
    return candidate


def brand_audio_attributes(
    attributes: Iterable[object],
    *,
    filename: str,
    environ: Mapping[str, str] | None = None,
    defaults_path: str | Path | None = None,
    settings_path: str | Path | None = None,
) -> list[object]:
    """Preserve file metadata and enforce Telegram's non-voice audio card."""

    title, performer = resolve_audio_branding(
        environ=environ,
        defaults_path=defaults_path,
        settings_path=settings_path,
    )
    result: list[object] = []
    duration = 0
    has_filename = False

    for attribute in attributes:
        if isinstance(attribute, types.DocumentAttributeAudio):
            try:
                duration = max(0, int(attribute.duration or 0))
            except (TypeError, ValueError):
                duration = 0
            continue
        if isinstance(attribute, types.DocumentAttributeFilename):
            has_filename = True
        result.append(attribute)

    if not has_filename:
        result.append(types.DocumentAttributeFilename(file_name=filename))
    result.append(
        types.DocumentAttributeAudio(
            duration=duration,
            voice=False,
            title=title,
            performer=performer,
        )
    )
    return result


def build_branded_audio_media(
    *,
    uploaded_file: object,
    uploaded_thumb: object | None,
    mime_type: str,
    attributes: list[object],
):
    """Create Telegram's audio media payload with an optional cover image."""

    return types.InputMediaUploadedDocument(
        file=uploaded_file,
        thumb=uploaded_thumb,
        mime_type=mime_type,
        attributes=attributes,
        force_file=False,
    )
