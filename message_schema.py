"""Validation and non-destructive migration for the fixed two-step flow."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys

LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2,5}$")


def build_default_language_template(language: str, lang_name: str | None = None) -> dict:
    code = language.strip().lower()
    display_name = (lang_name or "").strip() or code.upper()
    return {
        "lang_name": display_name,
        "steps": [
            {
                "step": 1,
                "text": "👋",
                "audio": f"{code}_msg1.mp3",
                "video": "",
                "loop": False,
            },
            {
                "step": 2,
                "text": "⏳",
                "audio": f"{code}_msg2.mp3",
                "video": "",
                "loop": True,
            },
        ],
        "call": {
            "text": "📞",
            "audio": f"{code}_call.mp3",
            "video": "",
        },
    }


def normalize_message_schema(messages: object, defaults: object) -> tuple[dict, bool]:
    if not isinstance(messages, dict) or not isinstance(defaults, dict):
        raise ValueError("messages and defaults must be objects")

    normalized = deepcopy(messages)
    changed = False

    effective_defaults = deepcopy(defaults)
    for language, raw_data in list(normalized.items()):
        if language in effective_defaults:
            continue
        if (
            isinstance(language, str)
            and LANGUAGE_CODE_RE.fullmatch(language)
            and isinstance(raw_data, dict)
        ):
            raw_name = raw_data.get("lang_name")
            effective_defaults[language] = build_default_language_template(
                language,
                raw_name if isinstance(raw_name, str) else None,
            )

    for language, default_language_data in effective_defaults.items():
        if not isinstance(default_language_data, dict):
            continue
        language_data = normalized.get(language)
        if not isinstance(language_data, dict):
            normalized[language] = deepcopy(default_language_data)
            language_data = normalized[language]
            changed = True

        default_lang_name = default_language_data.get("lang_name")
        current_lang_name = language_data.get("lang_name")
        if (
            isinstance(default_lang_name, str)
            and default_lang_name.strip()
            and (not isinstance(current_lang_name, str) or not current_lang_name.strip())
        ):
            language_data["lang_name"] = default_lang_name.strip()
            changed = True

        steps = language_data.get("steps")
        if not isinstance(steps, list):
            steps = []
            language_data["steps"] = steps
            changed = True
        default_steps = default_language_data.get("steps", [])
        if not isinstance(default_steps, list) or len(default_steps) < 2:
            raise ValueError(f"No hay dos pasos predeterminados para {language}")
        while len(steps) < 2:
            steps.append(deepcopy(default_steps[len(steps)]))
            changed = True

        for index in range(2):
            default_step = default_steps[index]
            if not isinstance(default_step, dict):
                raise ValueError(f"El paso {index + 1} predeterminado de {language} no es válido")
            if not isinstance(steps[index], dict):
                steps[index] = deepcopy(default_step)
                changed = True
            for field in ("text", "audio"):
                current_value = steps[index].get(field)
                # An explicit empty audio disables that component; it is not a missing field.
                if field == "audio" and current_value == "":
                    continue
                if not isinstance(current_value, str) or not current_value.strip():
                    default_value = default_step.get(field)
                    if not isinstance(default_value, str) or not default_value.strip():
                        raise ValueError(
                            f"Falta {field} en el paso {index + 1} predeterminado de {language}"
                        )
                    steps[index][field] = default_value
                    changed = True
            default_video = default_step.get("video")
            default_video_str = default_video.strip() if isinstance(default_video, str) else ""
            current_video = steps[index].get("video")
            if not isinstance(current_video, str):
                steps[index]["video"] = default_video_str
                changed = True
            elif not current_video.strip() and default_video_str:
                steps[index]["video"] = default_video_str
                changed = True
            elif current_video != current_video.strip():
                steps[index]["video"] = current_video.strip()
                changed = True
            desired_loop = index == 1
            if steps[index].get("loop") is not desired_loop:
                steps[index]["loop"] = desired_loop
                changed = True
            if steps[index].get("step") != index + 1:
                steps[index]["step"] = index + 1
                changed = True

        default_call = default_language_data.get("call", {})
        if not isinstance(default_call, dict):
            raise ValueError(f"La llamada predeterminada de {language} no es válida")
        if not isinstance(language_data.get("call"), dict):
            language_data["call"] = deepcopy(default_call)
            changed = True
        call = language_data["call"]
        for field in ("text", "audio"):
            current_value = call.get(field)
            if field == "audio" and current_value == "":
                continue
            if not isinstance(current_value, str) or not current_value.strip():
                default_value = default_call.get(field)
                if not isinstance(default_value, str) or not default_value.strip():
                    raise ValueError(f"Falta {field} en la llamada predeterminada de {language}")
                call[field] = default_value
                changed = True
        default_call_video = default_call.get("video")
        default_call_video_str = (
            default_call_video.strip() if isinstance(default_call_video, str) else ""
        )
        current_call_video = call.get("video")
        if not isinstance(current_call_video, str):
            call["video"] = default_call_video_str
            changed = True
        elif not current_call_video.strip() and default_call_video_str:
            call["video"] = default_call_video_str
            changed = True
        elif current_call_video != current_call_video.strip():
            call["video"] = current_call_video.strip()
            changed = True

    return normalized, changed


def load_message_file(
    messages_path: str | Path,
    defaults_path: str | Path,
    *,
    persist: bool = False,
) -> dict:
    messages_path = Path(messages_path)
    defaults_path = Path(defaults_path)
    defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
    missing_live_file = not messages_path.is_file()
    messages = (
        deepcopy(defaults)
        if missing_live_file
        else json.loads(messages_path.read_text(encoding="utf-8"))
    )
    normalized, changed = normalize_message_schema(messages, defaults)
    if persist and (changed or missing_live_file):
        messages_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = messages_path.with_suffix(messages_path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(normalized, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary_path, messages_path)
    return normalized


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Uso: message_schema.py <messages.json> <defaults.json>", file=sys.stderr)
        return 2
    load_message_file(argv[1], argv[2], persist=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
