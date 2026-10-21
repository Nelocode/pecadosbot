"""Offline panel promotion and account-scoped WhatsApp photo requests."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import app as app_module
from profile_photo_status import whatsapp_profile_picture_status


class WhatsAppProfileLinkRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.switch = self.data / "wa_switch"
        self.logo = self.root / "official.png"
        self.logo.write_bytes(app_module.PROFILE_LOGO_PATH.read_bytes())
        self.account = hashlib.sha256(b"570000000001@s.whatsapp.net").hexdigest()
        self.other = hashlib.sha256(b"570000000002@s.whatsapp.net").hexdigest()
        values = {
            "BASE_DIR": self.root, "DATA_DIR": self.data, "PROFILE_LOGO_PATH": self.logo,
            "WA_AUTH_DIR": self.data / "wa_auth", "WA_IDENTITY_FILE": self.data / "wa_identity.json",
            "WA_CALL_HEALTH_FILE": self.data / "wa_call_health.json",
            "WA_SWITCH_DIR": self.switch, "WA_SWITCH_AUTH_DIR": self.switch / "candidate_auth",
            "WA_SWITCH_QR_FILE": self.switch / "qr.png", "WA_SWITCH_HEALTH_FILE": self.switch / "health.json",
            "WA_SWITCH_IDENTITY_FILE": self.switch / "identity.json", "WA_SWITCH_PID_FILE": self.switch / "worker.pid",
            "WA_SWITCH_OPERATION_FILE": self.switch / "operation.json", "WA_SWITCH_RECOVERY_ROOT": self.data / "recovery",
        }
        for name, value in values.items():
            context = patch.object(app_module, name, value)
            context.start()
            self.addCleanup(context.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.data.mkdir()
        self.write_identity(app_module.WA_IDENTITY_FILE, self.account)

    def write_identity(self, path, account):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"account_hash": account, "display_name": "Fixture"}), encoding="utf-8")

    @property
    def request_file(self):
        return self.data / "wa_profile_picture_request.json"

    def status(self):
        return whatsapp_profile_picture_status(app_module.WA_IDENTITY_FILE,
                                               self.data / "wa_profile_picture_state.json",
                                               self.logo, self.logo)

    def promote(self, ready=True):
        for directory in (app_module.WA_AUTH_DIR, app_module.WA_SWITCH_AUTH_DIR):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "fixture.json").write_text("{}")
        self.write_identity(app_module.WA_SWITCH_IDENTITY_FILE, self.account)
        with ExitStack() as stack:
            stack.enter_context(app_module.app.test_request_context("/"))
            for name, value in {
                "_load_wa_switch_operation": {"status": "preparing"}, "_wa_switch_owned": True,
                "_whatsapp_recovery_pending": False, "_wa_connection_open": True,
                "_wa_process_running": True, "_stop_wa_process": None,
                "restart_wa_bot": 123 if ready else None, "_wait_until": ready,
                "_cleanup_wa_switch_candidate": None,
            }.items():
                stack.enter_context(patch.object(app_module, name, return_value=value))
            return app_module._promote_wa_candidate()

    def test_successful_promotion_requests_exact_account_only_after_worker_ready(self):
        with patch.object(app_module, "_queue_wa_profile_picture", wraps=app_module._queue_wa_profile_picture) as queue:
            result = self.promote()
        self.assertTrue(result[0])
        queue.assert_called_once_with(self.account)
        request = json.loads(self.request_file.read_text())
        self.assertEqual(request["brand"], "pecadosvip")
        self.assertEqual(request["accountHash"], self.account)
        self.assertEqual(request["imageHash"], hashlib.sha256(self.logo.read_bytes()).hexdigest())
        self.assertEqual(request["expiresAt"] - request["requestedAt"], 600_000)
        self.assertEqual(result[2]["profile_photo"]["status"], "pending")
        self.assertNotIn(self.account, json.dumps(result))

    def test_failed_promotion_and_restore_never_queue_photo(self):
        with patch.object(app_module, "_queue_wa_profile_picture") as queue:
            result = self.promote(ready=False)
        self.assertFalse(result[0])
        queue.assert_not_called()
        self.assertFalse(self.request_file.exists())

    def test_queue_failure_remains_visible_without_undoing_successful_promotion(self):
        with patch.object(app_module, "_queue_wa_profile_picture", return_value={"status": "failed", "message": "Foto no preparada."}):
            result = self.promote()
        self.assertTrue(result[0])
        self.assertIn("Foto no preparada.", result[1])
        self.assertEqual(result[2]["profile_photo"]["status"], "failed")

    def test_mismatched_or_missing_identity_cannot_request(self):
        for account in (None, self.other):
            self.assertEqual(app_module._queue_wa_profile_picture(account)["status"], "failed")
        self.assertFalse(self.request_file.exists())

    def test_disabled_or_other_brand_asset_cannot_request(self):
        with patch.dict(os.environ, {"WA_PROFILE_PICTURE_ENABLED": "0"}):
            self.assertEqual(app_module._queue_wa_profile_picture(self.account)["status"], "disabled")
        other = self.root / "other.png"
        other.write_bytes(b"other-brand")
        with patch.dict(os.environ, {"WA_PROFILE_PICTURE_PATH": str(other)}):
            self.assertEqual(app_module._queue_wa_profile_picture(self.account)["status"], "failed")
        self.assertFalse(self.request_file.exists())

    def test_status_shows_pending_only_for_bound_account_and_official_logo(self):
        app_module._queue_wa_profile_picture(self.account)
        self.assertEqual(self.status()["status"], "pending")
        self.write_identity(app_module.WA_IDENTITY_FILE, self.other)
        self.assertEqual(self.status()["status"], "unknown")
        self.assertNotIn(self.account, json.dumps(self.status()))

    def test_expired_or_wrong_brand_request_reports_failure_without_success(self):
        app_module._queue_wa_profile_picture(self.account)
        request = json.loads(self.request_file.read_text())
        request["requestedAt"] = int(time.time() * 1000) - 700_000
        request["expiresAt"] = request["requestedAt"] + 600_000
        self.request_file.write_text(json.dumps(request))
        self.assertEqual(self.status()["status"], "failed")
        request["brand"] = "las-fiesteras"
        self.request_file.write_text(json.dumps(request))
        self.assertEqual(self.status()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
