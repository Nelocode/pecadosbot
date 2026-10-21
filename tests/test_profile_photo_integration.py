"""Panel callbacks queue a photo only for a newly verified account."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app as app_module
import telegram_profile_picture as pictures
from telegram_auth import AuthOutcome
from telegram_profile_picture import REQUEST_FILE


class FakeTelegramPhotoClient:
    def __init__(self):
        self.upload_file = AsyncMock(return_value="uploaded-fixture")
        self.apply = AsyncMock(return_value=SimpleNamespace(photo=SimpleNamespace(id=42)))
        self.get_profile_photos = AsyncMock(return_value=[SimpleNamespace(id=42)])

    async def __call__(self, request):
        return await self.apply(request)


class ProfilePhotoIntegrationTests(unittest.TestCase):
    PHONE = "+570000000001"
    API_ID = 123456
    API_HASH = "a" * 32

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data_dir = self.root / "data"
        self.logo = self.root / "pecadosvip.png"
        self.logo.write_bytes(app_module.PROFILE_LOGO_PATH.read_bytes())
        for name, value in (("DATA_DIR", self.data_dir), ("PROFILE_LOGO_PATH", self.logo)):
            patcher = patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("_save_telegram_creds", "_write_telegram_authorized_marker"):
            patcher = patch.object(app_module, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        restart = patch.object(app_module, "restart_telegram_worker", return_value=(True, "ready"))
        self.restart = restart.start()
        self.addCleanup(restart.stop)
        spy = patch.object(app_module, "request_profile_picture", wraps=app_module.request_profile_picture)
        self.request_photo = spy.start()
        self.addCleanup(spy.stop)

    def outcome(self, **public):
        return AuthOutcome({"ok": True, **public}, (self.API_ID, self.API_HASH, self.PHONE))

    def finish_auth(self, outcome):
        with app_module.app.test_request_context("/api/verify_telegram_code", method="POST"):
            response = app_module._finish_telegram_auth(outcome)
            authorized = bool(app_module.session.get("channel_admin"))
        return response, authorized

    def finish_switch(self, outcome, promoted):
        with app_module.app.test_request_context("/api/switch_telegram/code", method="POST"):
            with patch.object(app_module, "_promote_telegram_candidate", return_value=promoted) as promote:
                response = app_module._finish_telegram_switch(outcome)
        return response, promote

    def poll_photo_once(self, client, *, phone=None):
        """Run the real consumer for one iteration with a mocked provider."""
        me = SimpleNamespace(phone=phone or self.PHONE, bot=False, id=123)

        async def consume():
            with (
                patch.object(pictures, "_photo_request", return_value="photo-request-fixture"),
                patch.object(pictures.asyncio, "sleep", side_effect=asyncio.CancelledError),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await pictures.poll_profile_picture_requests(
                        client, me, self.data_dir, self.logo, canonical_image_path=self.logo,
                    )

        asyncio.run(consume())

    def assert_public_without_identity(self, response):
        encoded = json.dumps(response)
        for private in (self.PHONE, self.PHONE.lstrip("+"), self.API_HASH,
                        hashlib.sha256(self.PHONE.lstrip("+").encode()).hexdigest(),
                        hashlib.sha256(self.logo.read_bytes()).hexdigest()):
            self.assertNotIn(private, encoded)

    def test_verified_link_with_started_worker_queues_real_account_bound_request(self):
        response, authorized = self.finish_auth(self.outcome())
        self.assertTrue(response["ok"])
        self.assertTrue(response["authorized"])
        self.assertTrue(authorized)
        self.assertEqual("pending", response["profile_photo"]["status"])
        self.request_photo.assert_called_once_with(self.data_dir, self.PHONE, self.logo, new_link=True)
        request = json.loads((self.data_dir / REQUEST_FILE).read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(self.PHONE.lstrip("+").encode()).hexdigest(), request["account_hash"])
        self.assertEqual(hashlib.sha256(self.logo.read_bytes()).hexdigest(), request["image_hash"])
        self.assert_public_without_identity(response)

    def test_pending_or_failed_auth_without_verified_credentials_never_queues(self):
        for public in ({"ok": True, "needs_code": True}, {"ok": True, "needs_qr": True},
                       {"ok": False, "needs_password": True}, {"ok": False, "error": "invalid_code"}):
            with self.subTest(public=public):
                outcome = AuthOutcome(public, attempt_token="opaque-test-attempt")
                response, authorized = self.finish_auth(outcome)
                self.assertEqual(public, {key: value for key, value in response.items() if key != "auth_attempt"})
                self.assertFalse(authorized)
                self.assertNotIn("profile_photo", response)
        self.request_photo.assert_not_called()
        self.restart.assert_not_called()
        self._save_telegram_creds.assert_not_called()
        self._write_telegram_authorized_marker.assert_not_called()
        self.assertFalse((self.data_dir / REQUEST_FILE).exists())

    def test_already_authorized_callback_does_not_schedule_profile_mutation(self):
        response, authorized = self.finish_auth(self.outcome(already_authorized=True))
        self.assertTrue(response["ok"])
        self.assertTrue(authorized)
        self.assertNotIn("profile_photo", response)
        self.request_photo.assert_not_called()
        self.assertFalse((self.data_dir / REQUEST_FILE).exists())

    def test_failed_worker_keeps_verified_auth_and_pending_photo_for_recovery(self):
        self.restart.return_value = (False, "worker unavailable")
        response, authorized = self.finish_auth(self.outcome())
        self.assertFalse(response["ok"])
        self.assertTrue(response["authorized"])
        self.assertTrue(authorized)
        self.assertFalse(response["worker_starting"])
        self.assertEqual("pending", response["profile_photo"]["status"])
        self.request_photo.assert_called_once_with(self.data_dir, self.PHONE, self.logo, new_link=True)
        self.assertTrue((self.data_dir / REQUEST_FILE).is_file())
        self._write_telegram_authorized_marker.assert_called_once_with()

    def test_recovered_worker_consumes_original_request_without_requeue_or_duplicate_photo(self):
        self.restart.return_value = (False, "worker unavailable")
        response, _ = self.finish_auth(self.outcome())
        self.assertFalse(response["ok"])
        self.assertEqual("pending", response["profile_photo"]["status"])
        request_path = self.data_dir / REQUEST_FILE
        original_request = json.loads(request_path.read_text(encoding="utf-8"))

        self.restart.return_value = (True, "ready")
        browser = app_module.app.test_client()
        csrf = "t" * 48
        with browser.session_transaction() as browser_session:
            browser_session["channel_csrf"] = csrf
            browser_session["channel_admin"] = True
        with (
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "_can_manage_channels", return_value=True),
            patch.object(app_module, "bot_is_running", return_value=False),
        ):
            recovered = browser.post("/api/link_telegram", json={}, headers={"X-Channel-CSRF": csrf})
        self.assertEqual(200, recovered.status_code)
        self.assertTrue(recovered.get_json()["already_authorized"])
        self.assertTrue(recovered.get_json()["worker_starting"])
        self.request_photo.assert_called_once_with(self.data_dir, self.PHONE, self.logo, new_link=True)
        self.assertEqual(original_request, json.loads(request_path.read_text(encoding="utf-8")))

        client = FakeTelegramPhotoClient()
        self.poll_photo_once(client, phone="+570000000002")
        client.upload_file.assert_not_awaited()
        self.assertFalse((self.data_dir / pictures.RESULTS_DIR).exists())
        self.assertEqual("pending", app_module._telegram_profile_status(self.PHONE)["status"])

        self.poll_photo_once(client)
        self.assertEqual("applied", app_module._telegram_profile_status(self.PHONE)["status"])
        self.poll_photo_once(client)
        client.upload_file.assert_awaited_once()
        client.apply.assert_awaited_once()
        client.get_profile_photos.assert_awaited_once()
        self.assertEqual(original_request, json.loads(request_path.read_text(encoding="utf-8")))
        self.request_photo.assert_called_once()

    def test_another_verified_link_reapplies_instead_of_returning_old_success(self):
        self.finish_auth(self.outcome())
        client = FakeTelegramPhotoClient()
        self.poll_photo_once(client)
        self.assertEqual("applied", app_module._telegram_profile_status(self.PHONE)["status"])
        request_path = self.data_dir / REQUEST_FILE
        original_id = json.loads(request_path.read_text(encoding="utf-8"))["request_id"]

        response, _ = self.finish_auth(self.outcome())
        self.assertEqual("pending", response["profile_photo"]["status"])
        self.assertNotEqual(original_id, json.loads(request_path.read_text(encoding="utf-8"))["request_id"])
        self.assertEqual("pending", app_module._telegram_profile_status(self.PHONE)["status"])
        self.poll_photo_once(client)
        self.assertEqual("applied", app_module._telegram_profile_status(self.PHONE)["status"])
        self.assertEqual(2, client.apply.await_count)
        self.assertEqual(2, client.get_profile_photos.await_count)

    def test_photo_storage_failure_does_not_undo_successful_auth(self):
        self.request_photo.side_effect = OSError("private storage detail")
        response, authorized = self.finish_auth(self.outcome())
        self.assertTrue(response["ok"])
        self.assertTrue(response["authorized"])
        self.assertTrue(authorized)
        self.assertEqual("failed", response["profile_photo"]["status"])
        self.assertNotIn("private storage detail", json.dumps(response))
        self._save_telegram_creds.assert_called_once_with(self.API_ID, self.API_HASH, self.PHONE)
        self._write_telegram_authorized_marker.assert_called_once_with()

    def test_other_brand_override_cannot_be_queued_or_reported_as_official(self):
        other_brand = self.root / "other-brand.png"
        other_brand.write_bytes(b"\x89PNG\r\n\x1a\nother-brand")
        with patch.dict(os.environ, {"TG_PROFILE_PICTURE_PATH": str(other_brand)}):
            response, authorized = self.finish_auth(self.outcome())
            status = app_module._telegram_profile_status(self.PHONE)
        self.assertTrue(response["ok"])
        self.assertTrue(authorized)
        self.assertEqual("failed", response["profile_photo"]["status"])
        self.assertEqual("failed", status["status"])
        self.request_photo.assert_not_called()
        self.assertFalse((self.data_dir / REQUEST_FILE).exists())

    def test_disabled_photo_policy_does_not_affect_auth_success(self):
        with patch.dict(os.environ, {"TG_PROFILE_PICTURE_ENABLED": "0"}):
            response, authorized = self.finish_auth(self.outcome())
        self.assertTrue(response["ok"])
        self.assertTrue(authorized)
        self.assertEqual("disabled", response["profile_photo"]["status"])
        self.request_photo.assert_not_called()

    def test_successful_account_promotion_queues_only_promoted_account(self):
        response, promote = self.finish_switch(self.outcome(), (True, "switched", True, True))
        promote.assert_called_once_with(self.API_ID, self.API_HASH, self.PHONE)
        self.assertTrue(response["switched"])
        self.assertEqual("pending", response["profile_photo"]["status"])
        self.request_photo.assert_called_once_with(self.data_dir, self.PHONE, self.logo, new_link=True)
        self.assert_public_without_identity(response)

    def test_failed_promotion_keeps_previous_account_and_never_queues(self):
        response, _ = self.finish_switch(self.outcome(), (False, "activation failed", True, True))
        self.assertFalse(response["switched"])
        self.assertTrue(response["previous_account_preserved"])
        self.assertTrue(response["previous_worker_ready"])
        self.assertNotIn("profile_photo", response)
        self.request_photo.assert_not_called()

    def test_pending_switch_does_not_promote_or_queue(self):
        response, promote = self.finish_switch(AuthOutcome({"ok": True, "needs_code": True}),
                                              (True, "unused", True, True))
        self.assertTrue(response["needs_code"])
        promote.assert_not_called()
        self.request_photo.assert_not_called()

    def test_photo_error_after_promotion_does_not_turn_switch_into_failure(self):
        self.request_photo.side_effect = OSError("private detail")
        response, _ = self.finish_switch(self.outcome(), (True, "switched", True, True))
        self.assertTrue(response["ok"])
        self.assertTrue(response["authorized"])
        self.assertTrue(response["switched"])
        self.assertEqual("failed", response["profile_photo"]["status"])

    def test_telegram_status_is_scoped_and_does_not_expose_request_hashes(self):
        self.finish_auth(self.outcome())
        status = app_module._telegram_profile_status(self.PHONE)
        self.assertEqual("pending", status["status"])
        self.assertLessEqual(set(status), {"status", "message", "updated_at"})
        self.assert_public_without_identity(status)
        unrelated = app_module._telegram_profile_status("+570000000002")
        self.assertEqual("not_requested", unrelated["status"])
        self.assert_public_without_identity(unrelated)

    def get_channels(self, *, can_manage, linked):
        """Exercise the real GET route and status readers with local fixtures."""
        wa_auth = self.root / "wa_auth_fixture"
        if linked:
            wa_auth.mkdir(exist_ok=True)
            (wa_auth / "fixture.json").write_text("{}", encoding="utf-8")
        identity = self.root / "wa_identity.json"
        wa_account_hash = hashlib.sha256(b"570000000001@s.whatsapp.net").hexdigest()
        identity.write_text(json.dumps({
            "account_hash": wa_account_hash,
            "phone": self.PHONE,
            "raw_error": "private provider detail",
            "phone_hint": "****0001",
        }), encoding="utf-8")
        self.data_dir.mkdir(exist_ok=True)
        (self.data_dir / "wa_profile_picture_state.json").write_text(json.dumps({
            "version": 1,
            "accounts": {wa_account_hash: {
                "status": "applied",
                "imageHash": hashlib.sha256(self.logo.read_bytes()).hexdigest(),
                "verification": "provider_acknowledged",
                "raw_error": "private provider detail",
            }},
        }), encoding="utf-8")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"TG_PHONE": self.PHONE}))
            for name, value in (("WA_AUTH_DIR", wa_auth), ("WA_IDENTITY_FILE", identity)):
                stack.enter_context(patch.object(app_module, name, value))
            for name, value in {
                "_can_manage_channels": can_manage,
                "_telegram_session_is_authorized": linked,
                "bot_is_running": linked,
                "_wa_process_running": linked,
                "_wa_connection_open": linked,
                "_read_wa_call_health": {},
                "_reap_expired_wa_switch": None,
                "_load_wa_switch_operation": None,
                "_telegram_recovery_pending": False,
                "_whatsapp_recovery_pending": False,
            }.items():
                stack.enter_context(patch.object(app_module, name, return_value=value))
            return app_module.app.test_client().get("/api/channels")

    def test_channels_endpoint_exposes_photo_only_to_manager_of_linked_accounts(self):
        self.finish_auth(self.outcome())
        response = self.get_channels(can_manage=True, linked=True)
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertTrue(payload["can_manage"])
        self.assertEqual("pending", payload["telegram"]["profile_photo"]["status"])
        self.assertEqual("applied", payload["whatsapp"]["profile_photo"]["status"])
        self.assertEqual("no-store, private", response.headers["Cache-Control"])
        self.assert_public_without_identity(payload)
        body = response.get_data(as_text=True)
        for private in ("account_hash", "imageHash", "image_hash", "raw_error", "private provider detail",
                        hashlib.sha256(b"570000000001@s.whatsapp.net").hexdigest()):
            self.assertNotIn(private, body)

    def test_channels_endpoint_hides_photo_state_from_non_manager(self):
        self.finish_auth(self.outcome())
        response = self.get_channels(can_manage=False, linked=True)
        payload = response.get_json()
        self.assertFalse(payload["can_manage"])
        for channel in ("telegram", "whatsapp"):
            self.assertIsNone(payload[channel]["profile_photo"])
        self.assert_public_without_identity(payload)

    def test_channels_endpoint_does_not_show_previous_photo_for_unlinked_accounts(self):
        self.finish_auth(self.outcome())
        response = self.get_channels(can_manage=True, linked=False)
        payload = response.get_json()
        for channel in ("telegram", "whatsapp"):
            self.assertFalse(payload[channel]["linked"])
            self.assertIsNone(payload[channel]["profile_photo"])

    def test_public_logo_endpoint_serves_exact_official_png_without_account_access(self):
        with (
            patch.object(app_module, "_telegram_session_is_authorized") as tg_session,
            patch.object(app_module, "_read_safe_identity") as identity,
            patch.object(app_module, "_can_manage_channels") as admin,
        ):
            response = app_module.app.test_client().get("/api/profile_photo/logo")
        self.assertEqual(200, response.status_code)
        self.assertEqual("image/png", response.mimetype)
        self.assertEqual(self.logo.read_bytes(), response.get_data())
        tg_session.assert_not_called()
        identity.assert_not_called()
        admin.assert_not_called()
        self.request_photo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
