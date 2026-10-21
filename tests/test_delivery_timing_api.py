"""Offline coverage for passive, bounded delivery diagnostics."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as panel


def trace_id(index=1):
    return f"00000000-0000-4000-8000-{index:012x}"


class DeliveryTimingApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.data = self.root / "data"
        self.data.mkdir()
        self.stack.enter_context(patch.object(panel, "DATA_DIR", self.data))
        self.stack.enter_context(patch.dict(panel.app.config, TESTING=True))
        self.client = panel.app.test_client()

    def write(self, filename, payload):
        path = self.data / filename
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def sample(self, **overrides):
        return {
            "trace_id": trace_id(), "observed_at": 1_800_000_000,
            "response": "step1", "outcome": "sent", "total_ms": 120,
            **overrides,
        }

    def get(self):
        response = self.client.get("/api/delivery_timing")
        self.assertEqual(200, response.status_code)
        self.assertEqual("no-store, private", response.headers["Cache-Control"])
        self.assertEqual("no-cache", response.headers["Pragma"])
        self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
        self.assertEqual("no-referrer", response.headers["Referrer-Policy"])
        return response.get_json()

    def test_channels_use_their_real_snapshot_shapes_and_timestamp_units(self):
        telegram = self.sample(
            queue_ms=1, prepare_ms=2, upload_ms=100, rpc_ms=17,
            video_bytes=25_000_000, video_cache_hit=False,
        )
        self.write("tg_interaction_health.json", {"schema_version": 1, "telemetry": telegram})
        self.write("wa_delivery_timing.json", {
            "schema_version": 1, "updated_at": 1_800_000_000_000,
            "samples": [self.sample(
                observed_at=1_800_000_000_000, outcome="completed", elapsed_ms=430,
                components={"video": {
                    "outcome": "completed", "queue_ms": 20, "pacing_ms": 10,
                    "sdk_ms": 400, "total_ms": 430,
                }},
            )],
        })

        result = self.get()

        self.assertEqual("unix_seconds", result["observed_at_unit"])
        self.assertEqual("milliseconds", result["duration_unit"])
        self.assertTrue(result["telegram"]["available"])
        self.assertTrue(result["whatsapp"]["available"])
        self.assertEqual(telegram, result["telegram"]["samples"][0])
        whatsapp = result["whatsapp"]["samples"][0]
        self.assertEqual(telegram["observed_at"], whatsapp["observed_at"])
        self.assertEqual("completed", whatsapp["outcome"])
        self.assertEqual(400, whatsapp["components"]["video"]["sdk_ms"])
        self.assertNotIn("ack_ms", whatsapp["components"]["video"])

    def test_get_and_head_never_touch_configuration_sessions_or_workers(self):
        snapshot = self.write("tg_interaction_health.json", {
            "schema_version": 1, "telemetry": self.sample(),
        })
        before = snapshot.read_bytes(), snapshot.stat().st_mtime_ns
        names_before = {path.name for path in self.data.iterdir()}
        with ExitStack() as forbidden:
            for name in (
                "load_messages_json", "save_messages_json", "_read_env_var",
                "_can_manage_channels", "_telegram_session_is_authorized",
                "_reap_expired_wa_switch", "_cleanup_wa_switch_candidate",
                "restart_telegram_worker", "restart_wa_bot", "api_start_botfather",
            ):
                forbidden.enter_context(patch.object(panel, name, side_effect=AssertionError(name)))
            self.get()
            response = self.client.head("/api/delivery_timing")
        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(before, (snapshot.read_bytes(), snapshot.stat().st_mtime_ns))
        self.assertEqual(names_before, {path.name for path in self.data.iterdir()})

    def test_missing_or_old_snapshots_report_unavailable_without_creation(self):
        self.write("tg_interaction_health.json", {"schema_version": 1, "delivery": {"video": "sent"}})
        result = self.get()
        self.assertEqual({"available": False, "samples": []}, result["telegram"])
        self.assertEqual({"available": False, "samples": []}, result["whatsapp"])
        self.assertFalse((self.data / "wa_delivery_timing.json").exists())

    def test_caps_history_at_the_latest_32_samples(self):
        self.write("wa_delivery_timing.json", {
            "schema_version": 1,
            "samples": [self.sample(trace_id=trace_id(index)) for index in range(1, 50)],
        })
        samples = self.get()["whatsapp"]["samples"]
        self.assertEqual(32, len(samples))
        self.assertEqual(trace_id(18), samples[0]["trace_id"])
        self.assertEqual(trace_id(49), samples[-1]["trace_id"])

    def test_only_allowlisted_fields_and_values_are_public(self):
        self.write("wa_delivery_timing.json", {
            "schema_version": 1, "private_token": "private-fixture-token",
            "samples": [self.sample(
                observed_at=1_800_000_000_000, response="private-phone", outcome="private-error",
                path="private-path", account_id="private-id", content="private-content",
                components={
                    "private-component": {"outcome": "sent"},
                    "video": {"outcome": "uncertain", "sdk_ms": 12, "raw_error": "private-provider-error"},
                    "text": {"outcome": "held", "total_ms": 0},
                    "audio": [],
                },
            )],
        })
        result = self.get()
        sample = result["whatsapp"]["samples"][0]
        self.assertIsNone(sample["response"])
        self.assertEqual("unknown", sample["outcome"])
        self.assertEqual({"video", "text"}, set(sample["components"]))
        self.assertEqual("held", sample["components"]["text"]["outcome"])
        self.assertNotIn("private", json.dumps(result))

    def test_invalid_metrics_cannot_escape_as_strings_booleans_or_nonfinite_numbers(self):
        invalid = [True, "private-fixture", -1, float("nan"), float("inf"), 10**500]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                self.write("tg_interaction_health.json", {
                    "schema_version": 1,
                    "telemetry": self.sample(queue_ms=value, video_bytes=value, video_cache_hit=value),
                })
                sample = self.get()["telegram"]["samples"][0]
                self.assertIsNone(sample["queue_ms"])
                self.assertIsNone(sample["video_bytes"])
                if type(value) is bool:
                    self.assertIs(sample["video_cache_hit"], value)
                else:
                    self.assertIsNone(sample["video_cache_hit"])
        self.write("tg_interaction_health.json", {
            "schema_version": 1,
            "telemetry": self.sample(total_ms=3_600_001, observed_at=-1),
        })
        sample = self.get()["telegram"]["samples"][0]
        self.assertIsNone(sample["total_ms"])
        self.assertIsNone(sample["observed_at"])

    def test_untrusted_trace_identifiers_are_dropped(self):
        for value in (None, True, [], "private-contact", "00000000-0000-1000-8000-000000000001"):
            with self.subTest(value=value):
                self.write("wa_delivery_timing.json", {
                    "schema_version": 1, "samples": [self.sample(trace_id=value)],
                })
                self.assertEqual({"available": False, "samples": []}, self.get()["whatsapp"])

    def test_malformed_snapshots_fail_closed(self):
        for payload in ([], None, {"schema_version": True}, {"schema_version": "1"},
                        {"schema_version": 2}, {"schema_version": 1, "samples": {}},
                        {"schema_version": 1, "samples": [None, [], "private-fixture"]}):
            with self.subTest(payload=payload):
                self.write("wa_delivery_timing.json", payload)
                self.assertFalse(self.get()["whatsapp"]["available"])
        path = self.data / "wa_delivery_timing.json"
        for content in (b"{", b"\xff", b"[" * 1500, b" " * (panel._DELIVERY_TIMING_MAX_SNAPSHOT_BYTES + 1)):
            with self.subTest(size=len(content)):
                path.write_bytes(content)
                self.assertFalse(self.get()["whatsapp"]["available"])
        path.unlink()
        path.mkdir()
        self.assertFalse(self.get()["whatsapp"]["available"])

    def test_external_file_link_is_rejected_before_opening_it(self):
        external = self.root / "other-brand.json"
        external.write_text(json.dumps({"schema_version": 1, "samples": [self.sample()]}), encoding="utf-8")
        path = self.data / "wa_delivery_timing.json"
        try:
            os.symlink(external, path)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows does not allow creating this fixture symlink")
            raise
        real_open = os.open

        def reject_external_open(candidate, *args, **kwargs):
            if Path(candidate) == external or Path(candidate) == path:
                raise AssertionError("Redirected timing file must not be opened")
            return real_open(candidate, *args, **kwargs)

        with patch.object(os, "open", reject_external_open):
            self.assertFalse(self.get()["whatsapp"]["available"])

    def test_regular_replacement_during_open_is_not_read(self):
        path = self.write("wa_delivery_timing.json", {"schema_version": 1, "samples": [self.sample()]})
        replacement = self.root / "replacement.json"
        replacement.write_text("private-fixture", encoding="utf-8")
        real_open = os.open

        def replace_then_open(candidate, flags, *args, **kwargs):
            self.assertEqual(path, Path(candidate))
            os.replace(replacement, path)
            return real_open(candidate, flags, *args, **kwargs)

        with (
            patch.object(os, "open", side_effect=replace_then_open) as opened,
            patch.object(os, "read", side_effect=AssertionError("Replacement must not be read")),
        ):
            self.assertFalse(self.get()["whatsapp"]["available"])
        opened.assert_called_once()

    def test_symlink_replacement_during_open_is_not_read(self):
        path = self.write("wa_delivery_timing.json", {"schema_version": 1, "samples": [self.sample()]})
        external = self.root / "other-brand.json"
        external.write_text("private-fixture", encoding="utf-8")
        probe = self.root / "symlink-permission-probe"
        try:
            os.symlink(external, probe)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows does not allow creating this fixture symlink")
            raise
        probe.unlink()
        real_open = os.open

        def replace_then_open(candidate, flags, *args, **kwargs):
            self.assertEqual(path, Path(candidate))
            path.unlink()
            os.symlink(external, path)
            return real_open(candidate, flags, *args, **kwargs)

        with (
            patch.object(os, "open", side_effect=replace_then_open) as opened,
            patch.object(os, "read", side_effect=AssertionError("Redirected file must not be read")),
        ):
            self.assertFalse(self.get()["whatsapp"]["available"])
        opened.assert_called_once()

    def test_fifo_replacement_during_open_cannot_block_or_be_read(self):
        if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
            self.skipTest("FIFO fixture requires a POSIX filesystem")
        path = self.write("wa_delivery_timing.json", {"schema_version": 1, "samples": [self.sample()]})
        replacement = self.root / "replacement-fifo"
        os.mkfifo(replacement)
        real_open = os.open

        def replace_then_open(candidate, flags, *args, **kwargs):
            self.assertEqual(path, Path(candidate))
            self.assertTrue(flags & os.O_NONBLOCK)
            os.replace(replacement, path)
            return real_open(candidate, flags, *args, **kwargs)

        with (
            patch.object(os, "open", side_effect=replace_then_open) as opened,
            patch.object(os, "read", side_effect=AssertionError("FIFO must not be read")),
        ):
            self.assertFalse(self.get()["whatsapp"]["available"])
        opened.assert_called_once()

    def test_redirected_data_directory_is_rejected_before_reading_snapshots(self):
        redirected = self.root / "redirected-data"
        try:
            os.symlink(self.data, redirected, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows does not allow creating this fixture symlink")
            raise
        with patch.object(panel, "DATA_DIR", redirected):
            result = self.get()
        self.assertFalse(result["telegram"]["available"])
        self.assertFalse(result["whatsapp"]["available"])

    def test_telegram_health_preserves_held_after_uncertain_video(self):
        self.write("tg_interaction_health.json", {
            "schema_version": 1,
            "delivery": {"peer_resolution": "sent", "video": "uncertain", "text": "held", "audio": "held"},
        })
        with patch.object(panel, "bot_is_running", return_value=False):
            response = self.client.get("/api/tg_interaction_health")
        self.assertEqual(200, response.status_code)
        self.assertEqual({
            "peer_resolution": "sent", "video": "uncertain", "text": "held", "audio": "held",
        }, response.get_json()["delivery"])

    def test_whatsapp_health_preserves_held_after_uncertain_video(self):
        path = self.write("wa_call_health.json", {
            "schema_version": 1, "pipeline": {
                "event": "offer", "outcome": "failed", "reason": "delivery_uncertain",
                "video_delivery": "uncertain", "delivery": "uncertain", "text": "held", "audio": "held",
            },
        })
        with (
            patch.object(panel, "WA_CALL_HEALTH_FILE", path),
            patch.object(panel, "_wa_process_running", return_value=False),
        ):
            response = self.client.get("/api/wa_call_health")
        self.assertEqual(200, response.status_code)
        result = response.get_json()["pipeline"]
        self.assertEqual("uncertain", result["video_delivery"])
        self.assertEqual("uncertain", result["delivery"])
        self.assertEqual("held", result["text"])
        self.assertEqual("held", result["audio"])
