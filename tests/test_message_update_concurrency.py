"""Concurrent panel requests must preserve unrelated message/media updates."""
from contextlib import ExitStack
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import app as panel
import message_schema
from tests.admin_session import grant_operator_admin, install_operator_key


class MessageUpdateConcurrencyTests(unittest.TestCase):
    def setUp(self):
        install_operator_key(self)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in {
            "DATA_DIR": self.root,
            "MESSAGES_FILE": self.root / "messages.json",
            "DEFAULT_MESSAGES_FILE": self.root / "defaults.json",
            "AUDIO_DIR": self.root / "audios",
            "VIDEO_DIR": self.root / "videos",
            "CUSTOM_LANGUAGE_RULES_FILE": self.root / "language_rules.custom.json",
        }.items():
            self.stack.enter_context(patch.object(panel, name, value))
        self.stack.enter_context(patch.dict(panel.app.config, TESTING=True))
        self.stack.enter_context(patch.object(panel, "reload_language_rules"))
        self.initial = {
            "es": {
                "lang_name": "Español",
                "editor_note": {"retain": "language metadata"},
                "steps": [
                    {"step": 1, "text": "First", "audio": "", "video": "", "loop": False, "custom": "retain first"},
                    {"step": 2, "text": "Second", "audio": "second.mp3", "video": "second.mp4", "loop": True},
                ],
                "call": {"text": "Call", "audio": "", "video": "", "custom": "retain call"},
            },
        }
        defaults = deepcopy(self.initial)
        defaults["es"]["steps"][0]["audio"] = "default.mp3"
        defaults["es"]["call"]["audio"] = "default-call.mp3"
        panel.DEFAULT_MESSAGES_FILE.write_text(json.dumps(defaults), encoding="utf-8")
        self.reset_messages()

    def reset_messages(self):
        panel.MESSAGES_FILE.write_text(json.dumps(self.initial), encoding="utf-8")

    def stored_messages(self):
        return json.loads(panel.MESSAGES_FILE.read_text(encoding="utf-8"))

    def post(self, route, **kwargs):
        with panel.app.test_client() as client:
            grant_operator_admin(client, csrf="c" * 48)
            response = client.post(route, headers={"X-Channel-CSRF": "c" * 48}, **kwargs)
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertTrue(response.get_json()["ok"], response.get_json())
        return response.get_json()

    def edit_text(self):
        return self.post("/api/messages", json={
            "lang": "es", "action": "edit_text", "step": 0, "text": "Edited text",
        })

    def run_interleaved(self, first, second, *, checkpoint_module=panel,
                        checkpoint_name="save_messages_json", pause_after=False):
        """Pause a writer with its snapshot, then start a competing HTTP request.

        Before serialization the second request commits while the first waits,
        and its update is lost. With serialization it waits for the first commit
        and reads the newer state. Each request has its own Flask client/thread.
        """
        snapshot_ready = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_done = threading.Event()
        failures = []
        results = {}
        original_checkpoint = getattr(checkpoint_module, checkpoint_name)
        paused = False

        def pause_first_snapshot(*args, **kwargs):
            nonlocal paused
            result = original_checkpoint(*args, **kwargs) if pause_after else None
            if threading.current_thread() is first_thread and not paused:
                paused = True
                snapshot_ready.set()
                if not release_first.wait(5):
                    raise AssertionError("First writer was not released")
            return result if pause_after else original_checkpoint(*args, **kwargs)

        def run(name, callback):
            if name == "second":
                second_started.set()
            try:
                results[name] = callback()
            except BaseException as exc:
                failures.append(exc)
                snapshot_ready.set()
            finally:
                if name == "second":
                    second_done.set()

        first_thread = threading.Thread(target=run, args=("first", first), daemon=True)
        second_thread = threading.Thread(target=run, args=("second", second), daemon=True)
        with patch.object(checkpoint_module, checkpoint_name, pause_first_snapshot):
            try:
                first_thread.start()
                self.assertTrue(snapshot_ready.wait(5), "First request did not reach its save")
                if failures:
                    raise failures[0]
                second_thread.start()
                self.assertTrue(second_started.wait(5), "Second request did not start")
                # Allow an unprotected writer to finish before the stale save.
                # A protected request intentionally remains blocked here.
                second_done.wait(0.25)
            finally:
                release_first.set()
                first_thread.join(5)
                if second_thread.ident is not None:
                    second_thread.join(5)
            self.assertFalse(first_thread.is_alive(), "First request deadlocked")
            self.assertFalse(second_thread.is_alive(), "Second request deadlocked")
        if failures:
            raise failures[0]
        return results

    def upload(self, target, field, chunked):
        is_call = target == "call"
        upload_type = ("call" if is_call else "step") + ("_video" if field == "video" else "")
        filename = "fixture.mp4" if field == "video" else "fixture.mp3"
        payload = {"lang": "es", "step": "0"}
        if chunked:
            self.post("/api/upload_chunk", data={
                **payload, "type": upload_type, "chunk_index": "0", "total_chunks": "1",
                "original_name": filename, "chunk": (io.BytesIO(b"media fixture"), filename),
            })
            return lambda: self.post("/api/upload_assemble", json={
                "lang": "es", "step": 0, "type": upload_type,
                "total_chunks": 1, "original_name": filename,
            })
        route = "/api/upload_" + ("call_" if is_call else "") + field
        return lambda: self.post(route, data={
            **payload, field: (io.BytesIO(b"media fixture"), filename),
        })

    def test_text_and_all_media_upload_routes_preserve_both_updates(self):
        for chunked in (False, True):
            for target in ("step", "call"):
                for field in ("video", "audio"):
                    with self.subTest(chunked=chunked, target=target, field=field):
                        self.reset_messages()
                        upload = self.upload(target, field, chunked)
                        results = self.run_interleaved(self.edit_text, upload)
                        expected = deepcopy(self.initial)
                        expected["es"]["steps"][0]["text"] = "Edited text"
                        section = expected["es"]["call"] if target == "call" else expected["es"]["steps"][0]
                        section[field] = results["second"]["filename"]
                        self.assertEqual(expected, self.stored_messages())
                        directory = panel.VIDEO_DIR if field == "video" else panel.AUDIO_DIR
                        self.assertEqual(b"media fixture", (directory / section[field]).read_bytes())

    def test_upload_snapshot_cannot_overwrite_a_concurrent_text_edit(self):
        upload = self.upload("step", "video", False)
        results = self.run_interleaved(upload, self.edit_text)
        expected = deepcopy(self.initial)
        expected["es"]["steps"][0].update(text="Edited text", video=results["first"]["filename"])
        self.assertEqual(expected, self.stored_messages())

    def test_normalizing_reader_cannot_overwrite_a_concurrent_video_upload(self):
        legacy = deepcopy(self.initial)
        del legacy["es"]["steps"][0]["step"]
        panel.MESSAGES_FILE.write_text(json.dumps(legacy), encoding="utf-8")
        results = self.run_interleaved(
            panel.load_messages_json, self.upload("step", "video", False),
            checkpoint_module=message_schema,
            checkpoint_name="normalize_message_schema", pause_after=True,
        )
        expected = deepcopy(self.initial)
        expected["es"]["steps"][0]["video"] = results["second"]["filename"]
        self.assertEqual(expected, self.stored_messages())
        self.assertEqual(1, results["first"]["es"]["steps"][0]["step"])

    def test_step_and_call_text_edits_preserve_both_updates(self):
        self.run_interleaved(self.edit_text, lambda: self.post("/api/messages", json={
            "lang": "es", "action": "edit_call_text", "text": "Edited call",
        }))
        expected = deepcopy(self.initial)
        expected["es"]["steps"][0]["text"] = "Edited text"
        expected["es"]["call"]["text"] = "Edited call"
        self.assertEqual(expected, self.stored_messages())

    def test_text_edit_cannot_restore_references_to_deleted_media(self):
        for target in ("step", "call"):
            for field in ("video", "audio"):
                with self.subTest(target=target, field=field):
                    self.reset_messages()
                    messages = self.stored_messages()
                    filename = "removed.mp4" if field == "video" else "removed.mp3"
                    section = messages["es"]["call"] if target == "call" else messages["es"]["steps"][0]
                    section[field] = filename
                    panel.MESSAGES_FILE.write_text(json.dumps(messages), encoding="utf-8")
                    directory = panel.VIDEO_DIR if field == "video" else panel.AUDIO_DIR
                    directory.mkdir(exist_ok=True)
                    media_file = directory / filename
                    media_file.write_bytes(b"old fixture")
                    self.run_interleaved(self.edit_text, lambda: self.post("/api/messages", json={
                        "lang": "es", "step": 0, "action": f"delete_{target}_{field}",
                    }))
                    expected = deepcopy(self.initial)
                    expected["es"]["steps"][0]["text"] = "Edited text"
                    self.assertEqual(expected, self.stored_messages())
                    self.assertFalse(media_file.exists())

    def test_parallel_language_additions_preserve_messages_and_detection_rules(self):
        def add_language(code):
            return self.post("/api/messages", json={
                "action": "add_language", "lang": code, "lang_name": code.upper(),
                "keywords": ["keyword-" + code],
            })

        self.run_interleaved(lambda: add_language("pt"), lambda: add_language("de"))
        messages = self.stored_messages()
        self.assertEqual({"es", "pt", "de"}, set(messages))
        self.assertEqual(self.initial["es"], messages["es"])
        rules = json.loads(panel.CUSTOM_LANGUAGE_RULES_FILE.read_text(encoding="utf-8"))
        self.assertEqual({"pt", "de"}, set(rules["languages"]))
        for code in ("pt", "de"):
            self.assertEqual("keyword-" + code, rules["languages"][code]["rules"][0]["term"])

    def test_failed_save_releases_transaction_for_the_next_request(self):
        with patch.object(panel, "save_messages_json", side_effect=OSError("fixture save failure")):
            with self.assertRaisesRegex(OSError, "fixture save failure"):
                self.edit_text()
        completed = threading.Event()
        failures = []

        def retry():
            try:
                self.edit_text()
            except BaseException as exc:
                failures.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=retry, daemon=True)
        thread.start()
        self.assertTrue(completed.wait(5), "Failed request left the transaction locked")
        thread.join(5)
        if failures:
            raise failures[0]
        self.assertEqual("Edited text", self.stored_messages()["es"]["steps"][0]["text"])
