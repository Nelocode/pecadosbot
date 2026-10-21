from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from message_schema import load_message_file, normalize_message_schema


DEFAULTS = {
    "es": {
        "steps": [
            {"step": 1, "text": "uno", "audio": "1.mp3", "loop": False},
            {"step": 2, "text": "dos", "audio": "2.mp3", "loop": True},
        ],
        "call": {"text": "llamada", "audio": "call.mp3"},
    }
}


class MessageSchemaTests(unittest.TestCase):
    def test_completes_missing_step_without_removing_legacy_third_step(self):
        live = {"es": {"steps": [{"text": "personalizado", "audio": "x.mp3"}]}}

        normalized, changed = normalize_message_schema(live, DEFAULTS)

        self.assertTrue(changed)
        self.assertEqual("personalizado", normalized["es"]["steps"][0]["text"])
        self.assertEqual("dos", normalized["es"]["steps"][1]["text"])
        self.assertTrue(normalized["es"]["steps"][1]["loop"])
        self.assertEqual("llamada", normalized["es"]["call"]["text"])

    def test_preserves_a_legacy_third_step_but_marks_second_as_loop(self):
        live = {
            "es": {
                "steps": [
                    {"step": 1, "text": "uno", "audio": "1.mp3", "loop": True},
                    {"step": 2, "text": "dos", "audio": "2.mp3", "loop": False},
                    {"step": 3, "text": "legacy", "audio": "3.mp3", "loop": True},
                ],
                "call": {"text": "x", "audio": "x.mp3"},
            }
        }

        normalized, _ = normalize_message_schema(live, DEFAULTS)

        self.assertEqual(3, len(normalized["es"]["steps"]))
        self.assertFalse(normalized["es"]["steps"][0]["loop"])
        self.assertTrue(normalized["es"]["steps"][1]["loop"])

    def test_persists_atomic_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            messages_path = Path(directory) / "messages.json"
            defaults_path = Path(directory) / "defaults.json"
            messages_path.write_text(
                json.dumps({"es": {"steps": []}}),
                encoding="utf-8",
            )
            defaults_path.write_text(json.dumps(DEFAULTS), encoding="utf-8")

            result = load_message_file(messages_path, defaults_path, persist=True)

            self.assertEqual(2, len(result["es"]["steps"]))
            self.assertEqual(result, json.loads(messages_path.read_text(encoding="utf-8")))

    def test_fills_missing_fields_without_overwriting_personalized_values(self):
        live = {
            "es": {
                "steps": [
                    {"text": "personalizado", "audio": "", "loop": False},
                    {"text": None, "audio": "custom.mp3", "loop": True},
                ],
                "call": {"text": "", "audio": "custom-call.mp3"},
            }
        }

        normalized, changed = normalize_message_schema(live, DEFAULTS)

        self.assertTrue(changed)
        self.assertEqual("personalizado", normalized["es"]["steps"][0]["text"])
        self.assertEqual("", normalized["es"]["steps"][0]["audio"])
        self.assertEqual("dos", normalized["es"]["steps"][1]["text"])
        self.assertEqual("custom.mp3", normalized["es"]["steps"][1]["audio"])
        self.assertEqual("llamada", normalized["es"]["call"]["text"])
        self.assertEqual("custom-call.mp3", normalized["es"]["call"]["audio"])
        self.assertEqual("", normalized["es"]["steps"][0]["video"])
        self.assertEqual("", normalized["es"]["call"]["video"])

    def test_preserves_explicit_empty_audio_for_both_steps_and_call(self):
        live = deepcopy(DEFAULTS)
        for response in (*live["es"]["steps"], live["es"]["call"]):
            response["audio"] = ""
            response["video"] = "configured.mp4"

        normalized, changed = normalize_message_schema(live, DEFAULTS)

        self.assertFalse(changed)
        self.assertEqual(live, normalized)
        # The source configuration and named defaults are never rewritten in place.
        self.assertEqual("1.mp3", DEFAULTS["es"]["steps"][0]["audio"])
        self.assertEqual("call.mp3", DEFAULTS["es"]["call"]["audio"])

    def test_missing_null_invalid_and_whitespace_audio_keep_legacy_defaults(self):
        missing = object()
        for value in (missing, None, False, 12, [], {}, " \t "):
            with self.subTest(value=value):
                live = deepcopy(DEFAULTS)
                for response in (*live["es"]["steps"], live["es"]["call"]):
                    if value is missing:
                        response.pop("audio")
                    else:
                        response["audio"] = value

                normalized, changed = normalize_message_schema(live, DEFAULTS)

                self.assertTrue(changed)
                self.assertEqual("1.mp3", normalized["es"]["steps"][0]["audio"])
                self.assertEqual("2.mp3", normalized["es"]["steps"][1]["audio"])
                self.assertEqual("call.mp3", normalized["es"]["call"]["audio"])

    def test_persisted_migration_keeps_empty_and_named_audio_on_repeated_reads(self):
        live = deepcopy(DEFAULTS)
        live["es"]["steps"][0]["audio"] = ""
        live["es"]["steps"][0]["video"] = "configured.mp4"
        live["es"]["call"]["audio"] = ""
        with tempfile.TemporaryDirectory() as directory:
            messages_path = Path(directory) / "messages.json"
            defaults_path = Path(directory) / "defaults.json"
            messages_path.write_text(json.dumps(live), encoding="utf-8")
            defaults_path.write_text(json.dumps(DEFAULTS), encoding="utf-8")

            first = load_message_file(messages_path, defaults_path, persist=True)
            saved = messages_path.read_bytes()
            second = load_message_file(messages_path, defaults_path, persist=True)

            self.assertEqual(first, second)
            self.assertEqual(saved, messages_path.read_bytes())
            self.assertEqual("", second["es"]["steps"][0]["audio"])
            self.assertEqual("2.mp3", second["es"]["steps"][1]["audio"])
            self.assertEqual("", second["es"]["call"]["audio"])
            self.assertEqual(DEFAULTS, json.loads(defaults_path.read_text(encoding="utf-8")))

    def test_preserves_personalized_video_fields_and_adds_empty_default(self):
        live = {
            "es": {
                "steps": [
                    {"step": 1, "text": "uno", "audio": "1.mp3", "video": "es_msg1.mp4", "loop": False},
                    {"step": 2, "text": "dos", "audio": "2.mp3", "loop": True},
                ],
                "call": {"text": "llamada", "audio": "call.mp3", "video": "es_call.mp4"},
            }
        }

        normalized, changed = normalize_message_schema(live, DEFAULTS)

        self.assertTrue(changed)
        self.assertEqual("es_msg1.mp4", normalized["es"]["steps"][0]["video"])
        self.assertEqual("", normalized["es"]["steps"][1]["video"])
        self.assertEqual("es_call.mp4", normalized["es"]["call"]["video"])

    def test_normalizes_custom_modular_language_not_in_defaults(self):
        live = {
            "es": DEFAULTS["es"],
            "pt": {
                "lang_name": "Português",
                "steps": [{"text": "Olá!"}],
            },
        }
        normalized, changed = normalize_message_schema(live, DEFAULTS)

        self.assertTrue(changed)
        self.assertIn("pt", normalized)
        self.assertEqual("Português", normalized["pt"]["lang_name"])
        self.assertEqual(2, len(normalized["pt"]["steps"]))
        self.assertEqual("Olá!", normalized["pt"]["steps"][0]["text"])
        self.assertEqual("pt_msg1.mp3", normalized["pt"]["steps"][0]["audio"])
        self.assertFalse(normalized["pt"]["steps"][0]["loop"])
        self.assertTrue(normalized["pt"]["steps"][1]["loop"])
        self.assertEqual("pt_call.mp3", normalized["pt"]["call"]["audio"])

if __name__ == "__main__":
    unittest.main()
