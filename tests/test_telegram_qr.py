import asyncio
import base64
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app as app_module
from telegram_auth import AuthOutcome, TelegramAuthManager
from telegram_qr import qr_image_data_url
from tests.admin_session import grant_operator_admin, install_operator_key
from tests.test_telegram_auth import FakeClient, Factory, SessionPasswordNeededError


class PasswordHashInvalidError(Exception):
    pass


class FakeQr:
    def __init__(self, client):
        self.client = client
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)
        self.revision = 0
        self.wait_count = 0
        self.event = asyncio.Event()
        self.error = None

    @property
    def url(self):
        return "tg://login?token=" + "test_token_" * 3 + str(self.revision)

    async def wait(self):
        self.client._record_loop()
        self.wait_count += 1
        await self.event.wait()
        self.event.clear()
        if self.error:
            error, self.error = self.error, None
            raise error
        if self.client.require_password:
            raise SessionPasswordNeededError("private details")
        self.client.authorized = True
        return self.client.user

    async def recreate(self):
        self.client._record_loop()
        self.revision += 1
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)


class QrClient(FakeClient):
    def __init__(self, *, phone="570000000000", **kwargs):
        super().__init__(**kwargs)
        self.user = SimpleNamespace(phone=phone, bot=False)
        self.qr = None
        self.logout_count = 0

    async def qr_login(self):
        self._record_loop()
        self.qr = FakeQr(self)
        return self.qr

    async def get_me(self):
        self._record_loop()
        return self.user

    async def log_out(self):
        self._record_loop()
        self.logout_count += 1
        self.authorized = False

    async def sign_in(self, **kwargs):
        if kwargs.get("password") == "wrong-password":
            raise PasswordHashInvalidError("private details")
        return await super().sign_in(**kwargs)


class TelegramQrTestCase(unittest.TestCase):
    API_ID = 123456
    API_HASH = "0123456789abcdef0123456789abcdef"
    PHONE = "+570000000000"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.session_file = str(Path(self.temporary.name) / "qr_session")
        self.client = QrClient()
        self.factory = Factory(self.client)
        self.manager = TelegramAuthManager(self.session_file, client_factory=self.factory)

    def tearDown(self):
        pending = self.manager._pending
        if pending:
            self.manager.cancel(pending["attempt_token"])
        loop, thread = self.manager._loop, self.manager._thread
        if loop:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            loop.close()
        self.temporary.cleanup()

    def begin(self):
        return self.manager.begin_qr(self.API_ID, self.API_HASH, self.PHONE)

    def signal(self, error=None):
        async def trigger():
            self.client.qr.error = error
            self.client.qr.event.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return AuthOutcome({"ok": True})
        self.manager._submit(trigger())

    def test_listener_is_running_before_qr_is_returned_and_no_code_is_sent(self):
        begun = self.begin()
        self.assertTrue(begun.public["needs_qr"])
        self.assertTrue(begun.attempt_token)
        self.assertEqual(self.client.qr.url, begun.qr_url)
        self.assertNotIn(begun.qr_url, repr(begun))
        self.assertNotIn("token=", json.dumps(begun.public))
        self.assertEqual(1, self.client.qr.wait_count)
        self.assertEqual(0, self.client.send_count)

    def test_confirmation_is_consumed_once_on_the_same_loop(self):
        begun = self.begin()
        self.signal()
        result = self.manager.qr_status(begun.attempt_token)
        replay = self.manager.qr_status(begun.attempt_token)
        self.assertEqual((self.API_ID, self.API_HASH, self.PHONE), result.credentials)
        self.assertEqual({"ok": True}, result.public)
        self.assertEqual("invalid_auth_attempt", replay.public["error_code"])
        self.assertEqual(1, len(set(self.client.loop_ids)))
        self.assertEqual(0, self.client.logout_count)
        self.assertFalse(self.client.connected)

    def test_expired_qr_is_renewed_with_the_same_browser_attempt(self):
        begun = self.begin()
        old_url = begun.qr_url
        self.signal(asyncio.TimeoutError())
        renewed = self.manager.qr_status(begun.attempt_token)
        self.assertTrue(renewed.public["needs_qr"])
        self.assertNotEqual(old_url, renewed.qr_url)
        self.assertEqual(2, self.client.qr.wait_count)
        self.assertEqual(1, len(self.factory.calls))

    def test_other_browser_cannot_read_cancel_or_replace_qr(self):
        begun = self.begin()
        for token in (None, "another-browser", {"malformed": True}):
            denied = self.manager.qr_status(token)
            self.assertEqual("invalid_auth_attempt", denied.public["error_code"])
            self.assertIsNone(denied.qr_url)
            self.assertIsNone(denied.credentials)
        foreign = self.manager.begin_qr(self.API_ID, self.API_HASH, self.PHONE)
        self.assertEqual("auth_in_progress", foreign.public["error_code"])
        self.assertFalse(self.manager.cancel("another-browser").public["ok"])
        self.assertTrue(self.manager.qr_status(begun.attempt_token).public["needs_qr"])
        self.assertEqual(1, len(self.factory.calls))

    def test_concurrent_starts_create_only_one_client(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: self.begin(), range(2)))
        self.assertEqual(1, sum(bool(outcome.qr_url) for outcome in outcomes))
        self.assertEqual(1, len(self.factory.calls))

    def test_cancel_after_acceptance_revokes_and_removes_local_session(self):
        begun = self.begin()
        Path(self.session_file + ".session").write_text("test-session-secret")
        self.signal()
        self.manager.cancel(begun.attempt_token)
        self.assertEqual(1, self.client.logout_count)
        self.assertFalse(Path(self.session_file + ".session").exists())
        self.assertFalse(self.client.connected)
        self.assertFalse(self.manager.has_pending())

    def test_abandoned_authorization_expires_without_saving_credentials(self):
        self.begin()
        self.signal()
        self.manager._pending["expires_at"] = time.monotonic() - 1
        self.assertFalse(self.manager.has_pending())
        self.assertEqual(1, self.client.logout_count)

    def test_cancelling_before_scan_stops_the_waiter_and_blocks_code_bypass(self):
        begun = self.begin()
        task = self.manager._pending["qr_task"]
        self.assertEqual("wrong_auth_method", self.manager.verify_code("24680", begun.attempt_token).public["error_code"])
        self.assertEqual("wrong_auth_method", self.manager.verify_password("test-password", begun.attempt_token).public["error_code"])
        self.manager.cancel(begun.attempt_token)
        self.assertTrue(task.cancelled())
        self.assertFalse(self.client.connected)
        self.assertEqual(0, self.client.sign_in_count)

    def test_wrong_account_is_rejected_and_revoked_without_echoing_phone(self):
        self.client.user.phone = "571111111111"
        begun = self.begin()
        self.signal()
        result = self.manager.qr_status(begun.attempt_token)
        self.assertEqual("qr_account_mismatch", result.public["error_code"])
        self.assertIsNone(result.credentials)
        self.assertEqual(1, self.client.logout_count)
        self.assertNotIn(self.client.user.phone, json.dumps(result.public))
        self.assertFalse(self.manager.has_pending())

    def test_qr_2fa_keeps_attempt_until_correct_password(self):
        self.client.require_password = True
        begun = self.begin()
        self.signal()
        needs_password = self.manager.qr_status(begun.attempt_token)
        self.assertTrue(needs_password.public["needs_password"])
        rejected = self.manager.verify_password("wrong-password", begun.attempt_token)
        self.assertEqual("invalid_password", rejected.public["error_code"])
        result = self.manager.verify_password("correct-test-password", begun.attempt_token)
        self.assertEqual((self.API_ID, self.API_HASH, self.PHONE), result.credentials)
        self.assertEqual(0, self.client.logout_count)
        self.assertFalse(self.manager.has_pending())

    def test_wrong_account_after_2fa_is_also_rejected(self):
        self.client.require_password = True
        self.client.user.phone = "571111111111"
        begun = self.begin()
        self.signal()
        self.manager.qr_status(begun.attempt_token)
        result = self.manager.verify_password("correct-test-password", begun.attempt_token)
        self.assertEqual("qr_account_mismatch", result.public["error_code"])
        self.assertIsNone(result.credentials)
        self.assertEqual(1, self.client.logout_count)

    def test_switch_from_code_to_qr_and_back_cancels_the_previous_listener(self):
        code = self.manager.begin(self.API_ID, self.API_HASH, self.PHONE)
        qr = self.manager.begin_qr(self.API_ID, self.API_HASH, self.PHONE, code.attempt_token)
        task = self.manager._pending["qr_task"]
        restarted = self.manager.begin(self.API_ID, self.API_HASH, self.PHONE, qr.attempt_token)
        self.assertTrue(restarted.public["needs_code"])
        self.assertTrue(task.done())
        self.assertFalse(self.manager.qr_status(qr.attempt_token).public["ok"])

    def test_existing_authorized_session_is_never_replaced_by_qr(self):
        self.client.authorized = True
        self.client.user.phone = "571111111111"
        result = self.begin()
        self.assertEqual("qr_account_mismatch", result.public["error_code"])
        self.assertIsNone(self.client.qr)
        self.assertEqual(0, self.client.logout_count)


class TelegramQrRoutesTestCase(unittest.TestCase):
    API_HASH = "0123456789abcdef0123456789abcdef"
    PHONE = "+570000000000"
    ATTEMPT = "test-attempt-token"
    QR_URL = "tg://login?token=" + "fake_token_" * 4

    def setUp(self):
        operator_key = install_operator_key(self)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        grant_operator_admin(self.client, operator_key, csrf="q" * 48)
        self.client.environ_base["HTTP_X_CHANNEL_CSRF"] = "q" * 48
        for name in ("_telegram_session_is_authorized", "_telegram_recovery_pending"):
            patcher = patch.object(app_module, name, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_start_returns_only_a_local_image_and_never_persists_unconfirmed_creds(self):
        manager = Mock()
        manager.begin_qr.return_value = AuthOutcome(
            {"ok": True, "needs_qr": True, "expires_in": 30},
            attempt_token=self.ATTEMPT, qr_url=self.QR_URL,
        )
        with (patch.object(app_module, "_telegram_auth", manager),
              patch.object(app_module, "_save_telegram_creds") as save,
              patch.object(app_module, "restart_telegram_worker") as restart):
            response = self.client.post("/api/telegram_qr/start", json={
                "api_id": 123456, "api_hash": self.API_HASH, "phone": self.PHONE,
            })
        self.assertEqual(200, response.status_code)
        data = response.get_json()
        self.assertEqual(self.ATTEMPT, data["auth_attempt"])
        self.assertTrue(data["qr_image"].startswith("data:image/svg+xml;base64,"))
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertNotIn(self.QR_URL, response.get_data(as_text=True))
        self.assertNotIn(self.API_HASH, response.get_data(as_text=True))
        self.assertNotIn(self.PHONE, response.get_data(as_text=True))
        save.assert_not_called()
        restart.assert_not_called()

    def test_qr_endpoints_require_operator_and_csrf(self):
        with patch.object(app_module, "_telegram_auth") as manager:
            for endpoint in ("/api/telegram_qr/start", "/api/telegram_qr/status"):
                response = self.client.post(endpoint, json={}, headers={"X-Channel-CSRF": "wrong"})
                self.assertEqual(403, response.status_code)
            with self.client.session_transaction() as browser:
                browser.pop("channel_admin", None)
            for endpoint in ("/api/telegram_qr/start", "/api/telegram_qr/status"):
                self.assertEqual(403, self.client.post(endpoint, json={}).status_code)
            manager.begin_qr.assert_not_called()
            manager.qr_status.assert_not_called()

    def test_existing_session_or_recovery_prevents_start(self):
        with (patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
              patch.object(app_module, "_telegram_auth") as manager):
            response = self.client.post("/api/telegram_qr/start", json={})
        self.assertEqual(409, response.status_code)
        manager.begin_qr.assert_not_called()

    def test_invalid_input_does_not_request_a_qr(self):
        with patch.object(app_module, "_telegram_auth") as manager:
            response = self.client.post("/api/telegram_qr/start", json={"api_id": "invalid"})
        self.assertEqual(400, response.status_code)
        manager.begin_qr.assert_not_called()

    def test_status_commits_only_verified_credentials_through_existing_finish(self):
        manager = Mock()
        manager.qr_status.return_value = AuthOutcome(
            {"ok": True}, credentials=(123456, self.API_HASH, self.PHONE),
        )
        with (patch.object(app_module, "_telegram_auth", manager),
              patch.object(app_module, "_save_telegram_creds") as save,
              patch.object(app_module, "_write_telegram_authorized_marker") as marker,
              patch.object(app_module, "restart_telegram_worker", return_value=(True, "ready")) as restart,
              patch.dict("os.environ", {}, clear=True)):
            response = self.client.post("/api/telegram_qr/status", json={"auth_attempt": self.ATTEMPT})
        self.assertTrue(response.get_json()["authorized"])
        save.assert_called_once_with(123456, self.API_HASH, self.PHONE)
        marker.assert_called_once_with()
        restart.assert_called_once_with()
        manager.qr_status.assert_called_once_with(self.ATTEMPT)

    def test_image_is_a_self_contained_svg_and_rejects_external_urls(self):
        image = qr_image_data_url(self.QR_URL)
        svg = base64.b64decode(image.split(",", 1)[1]).decode("utf-8")
        self.assertIn("<svg", svg)
        self.assertIn("<path", svg)
        self.assertNotIn("<script", svg)
        self.assertNotIn("href=", svg)
        for url in ("https://example.com/qr", "javascript:alert(1)", "tg://login?token=short"):
            with self.assertRaises(ValueError):
                qr_image_data_url(url)


if __name__ == "__main__":
    unittest.main()
