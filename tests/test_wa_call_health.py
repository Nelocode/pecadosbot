"""Offline checks for delivery evidence exposed by the panel's health endpoint."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as app_module


class WhatsAppCallDeliveryHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "health.json"

    def read(self, pipeline):
        self.path.write_text(json.dumps({"schema_version": 1, "pipeline": pipeline}), encoding="utf-8")
        with (
            patch.object(app_module, "WA_CALL_HEALTH_FILE", self.path),
            patch.object(app_module, "_wa_process_running", return_value=False),
            app_module.app.test_request_context("/api/wa_call_health"),
        ):
            response = app_module.api_wa_call_health()
            self.assertEqual("no-store, private", response.headers["Cache-Control"])
            return response.get_json()

    def test_endpoint_keeps_partial_and_uncertain_delivery_evidence(self):
        for video, delivery, reason in (
            ("missing", "partial", "delivery_incomplete"),
            ("failed", "failed", "delivery_incomplete"),
            ("uncertain", "uncertain", "delivery_uncertain"),
            ("sent", "complete", "completed"),
        ):
            with self.subTest(delivery=delivery):
                outcome = "handled" if delivery == "complete" else "failed"
                result = self.read({
                    "event": "offer", "outcome": outcome, "reason": reason,
                    "video": False, "video_delivery": video, "delivery": delivery,
                    "text": "sent", "audio": "skipped", "private_path": "private-fixture.mp4",
                })
                self.assertEqual(outcome, result["pipeline"]["outcome"])
                self.assertEqual(reason, result["pipeline"]["reason"])
                self.assertIs(result["pipeline"]["video"], False)
                self.assertEqual(video, result["pipeline"]["video_delivery"])
                self.assertEqual(delivery, result["pipeline"]["delivery"])
                self.assertNotIn("private", json.dumps(result))

    def test_unknown_values_do_not_escape_allowlist_or_imply_delivery(self):
        result = self.read({
            "event": "offer", "reason": "private-reason", "video": "sent",
            "video_delivery": "private-video", "delivery": "private-delivery",
            "text": "private-text", "audio": "uncertain",
        })["pipeline"]
        self.assertEqual("unexpected_error", result["reason"])
        self.assertIsNone(result["video"])
        self.assertEqual("not_applicable", result["video_delivery"])
        self.assertEqual("not_applicable", result["delivery"])
        self.assertEqual("not_applicable", result["text"])
        self.assertEqual("uncertain", result["audio"])
        self.assertNotIn("private", json.dumps(result))

    def test_older_snapshot_does_not_invent_video_success(self):
        result = self.read({"event": "offer", "outcome": "handled", "reason": "completed"})["pipeline"]
        self.assertEqual("not_applicable", result["video_delivery"])
        self.assertEqual("not_applicable", result["delivery"])
