import asyncio
import json
import threading
import time
import unittest
from types import SimpleNamespace

from telegram_auth import TelegramAuthManager, _error_outcome, describe_sent_code


class SentCodeTypeApp:
    def __init__(self, length=5):
        self.length = length


class SentCodeTypeSms:
    def __init__(self, length=5):
        self.length = length


class CodeTypeSms:
    pass


class UnknownDeliveryType:
    def __init__(self, length=99):
        self.length = length


class FakeSentCode:
    def __init__(
        self,
        *,
        delivery_type=None,
        next_type=None,
        timeout=30,
        phone_code_hash="challenge-secret",
    ):
        self.type = delivery_type if delivery_type is not None else SentCodeTypeApp()
        self.next_type = next_type
        self.timeout = timeout
        self.phone_code_hash = phone_code_hash


class SessionPasswordNeededError(Exception):
    pass


class PhoneCodeInvalidError(Exception):
    pass


class ApiIdInvalidError(Exception):
    pass


class FloodWaitError(Exception):
    def __init__(self, message, seconds):
        super().__init__(message)
        self.seconds = seconds


class SecretServerError(Exception):
    pass


class FakeClient:
    def __init__(
        self,
        *,
        sent_code=None,
        require_password=False,
        invalid_code_once=False,
        send_error=None,
        block_send=False,
    ):
        self.sent_code = sent_code or FakeSentCode()
        self.require_password = require_password
        self.invalid_code_once = invalid_code_once
        self.send_error = send_error
        self.block_send = block_send
        self.authorized = False
        self.connected = False
        self.disconnect_count = 0
        self.send_count = 0
        self.sign_in_count = 0
        self.loop_ids = []
        self.sign_in_arguments = []
        self.user = SimpleNamespace(id=123, phone="570000000000", bot=False)
        self.get_me_error = None

    def _record_loop(self):
        self.loop_ids.append(id(asyncio.get_running_loop()))

    async def connect(self):
        self._record_loop()
        self.connected = True

    async def disconnect(self):
        self._record_loop()
        self.connected = False
        self.disconnect_count += 1

    async def is_user_authorized(self):
        self._record_loop()
        return self.authorized

    async def get_me(self):
        self._record_loop()
        if self.get_me_error:
            raise self.get_me_error
        return self.user

    async def send_code_request(self, phone):
        self._record_loop()
        self.send_count += 1
        if self.send_error is not None:
            raise self.send_error
        if self.block_send:
            await asyncio.Event().wait()
        return self.sent_code

    async def sign_in(self, **kwargs):
        self._record_loop()
        self.sign_in_count += 1
        self.sign_in_arguments.append(kwargs)
        if "code" in kwargs:
            if self.invalid_code_once:
                self.invalid_code_once = False
                raise PhoneCodeInvalidError("raw code details must not escape")
            if self.require_password:
                raise SessionPasswordNeededError("2FA secret details must not escape")
            self.authorized = True
            return
        if "password" in kwargs:
            self.authorized = True


class Factory:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, session_file, api_id, api_hash):
        self.calls.append((session_file, api_id, api_hash))
        return self.client


class TelegramAuthTestCase(unittest.TestCase):
    API_ID = 123456
    API_HASH = "api-hash-do-not-expose"
    PHONE = "+570000000000"
    CODE = "24680"
    PASSWORD = "two-factor-password-do-not-expose"

    def setUp(self):
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            try:
                token = manager._pending.get("attempt_token") if manager._pending else None
                manager.cancel(token)
            except Exception:
                pass
            loop = manager._loop
            thread = manager._thread
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=1)

    def make_manager(self, client, **kwargs):
        manager = TelegramAuthManager(
            "test-session",
            client_factory=Factory(client),
            **kwargs,
        )
        self.managers.append(manager)
        return manager

    def assert_public_does_not_contain(self, public, *secrets):
        encoded = json.dumps(public, ensure_ascii=False, sort_keys=True)
        for secret in secrets:
            self.assertNotIn(str(secret), encoded)

    def test_begin_and_code_verification_use_one_event_loop(self):
        client = FakeClient()
        manager = self.make_manager(client)

        begun = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
        verified = manager.verify_code(self.CODE, begun.attempt_token)

        self.assertTrue(begun.public["ok"])
        self.assertTrue(begun.public["needs_code"])
        self.assertEqual({"ok": True}, verified.public)
        self.assertEqual(
            (self.API_ID, self.API_HASH, self.PHONE), verified.credentials
        )
        self.assertEqual(1, len(set(client.loop_ids)))
        self.assertGreaterEqual(len(client.loop_ids), 6)
        self.assertEqual(1, client.disconnect_count)

    def test_has_pending_tracks_only_an_active_challenge(self):
        client = FakeClient()
        manager = self.make_manager(client)

        self.assertFalse(manager.has_pending())
        begun = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
        self.assertTrue(manager.has_pending())
        manager.verify_code(self.CODE, begun.attempt_token)
        self.assertFalse(manager.has_pending())

    def test_existing_session_requires_matching_user_identity(self):
        for user in (
            SimpleNamespace(phone="571111111111", bot=False),
            SimpleNamespace(phone=None, bot=False),
            SimpleNamespace(phone="570000000000", bot=True),
            None,
        ):
            with self.subTest(user=user):
                client = FakeClient()
                client.authorized = True
                client.user = user
                manager = self.make_manager(client)
                result = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
                self.assertFalse(result.public["ok"])
                self.assertEqual("account_mismatch", result.public["error_code"])
                self.assertIsNone(result.credentials)
                self.assertFalse(client.connected)
                self.assertTrue(client.authorized)  # No logout of the other account.
                self.assertEqual(0, client.send_count)
                self.assert_public_does_not_contain(result.public, self.PHONE, self.API_HASH, "571111111111")

    def test_existing_matching_session_can_be_reused(self):
        client = FakeClient()
        client.authorized = True
        manager = self.make_manager(client)
        result = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
        self.assertTrue(result.public["already_authorized"])
        self.assertEqual((self.API_ID, self.API_HASH, self.PHONE), result.credentials)
        self.assertFalse(client.connected)

    def test_code_and_password_require_matching_identity_after_authorization(self):
        for needs_password in (False, True):
            for phone, bot in (("571111111111", False), (None, False), ("570000000000", True)):
                with self.subTest(needs_password=needs_password, phone=phone, bot=bot):
                    client = FakeClient(require_password=needs_password)
                    client.user = SimpleNamespace(phone=phone, bot=bot)
                    manager = self.make_manager(client)
                    begun = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
                    result = manager.verify_code(self.CODE, begun.attempt_token)
                    if needs_password:
                        self.assertTrue(result.public["needs_password"])
                        result = manager.verify_password(self.PASSWORD, begun.attempt_token)
                    self.assertEqual("account_mismatch", result.public["error_code"])
                    self.assertIsNone(result.credentials)
                    self.assertFalse(manager.has_pending())
                    self.assertFalse(client.connected)
                    self.assertTrue(client.authorized)

    def test_get_me_failure_never_returns_verified_credentials(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                client = FakeClient()
                client.authorized = existing
                client.get_me_error = SecretServerError("private account detail")
                manager = self.make_manager(client)
                result = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
                if not existing:
                    result = manager.verify_code(self.CODE, result.attempt_token)
                self.assertFalse(result.public["ok"])
                self.assertIsNone(result.credentials)
                self.assertFalse(client.connected)
                self.assert_public_does_not_contain(result.public, "private account detail", self.PHONE)
    def test_expiry_callback_disconnects_an_abandoned_challenge(self):
        client = FakeClient()
        manager = self.make_manager(client)
        begun = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
        manager._pending["expires_at"] = time.monotonic() - 1

        manager._submit(manager._expire_pending(begun.attempt_token))

        self.assertFalse(manager.has_pending())
        self.assertFalse(client.connected)
        self.assertEqual(1, client.disconnect_count)

    def test_pending_survives_code_step_until_2fa_finishes_on_same_loop(self):
        client = FakeClient(require_password=True)
        manager = self.make_manager(client)

        begun = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
        code_result = manager.verify_code(self.CODE, begun.attempt_token)

        self.assertEqual(
            {"ok": False, "needs_password": True}, code_result.public
        )
        self.assertTrue(client.connected)
        self.assertEqual(0, client.disconnect_count)

        password_result = manager.verify_password(self.PASSWORD, begun.attempt_token)

        self.assertEqual({"ok": True}, password_result.public)
        self.assertEqual(
            (self.API_ID, self.API_HASH, self.PHONE), password_result.credentials
        )
        self.assertEqual(1, len(set(client.loop_ids)))
        self.assertEqual(1, client.disconnect_count)
        self.assert_public_does_not_contain(
            code_result.public,
            self.CODE,
            self.PASSWORD,
            self.API_HASH,
            self.PHONE,
            client.sent_code.phone_code_hash,
        )

    def test_invalid_code_does_not_destroy_pending_challenge(self):
        client = FakeClient(invalid_code_once=True)
        manager = self.make_manager(client)
        begun = manager.begin(self.API_ID, self.API_HASH, self.PHONE)

        invalid = manager.verify_code("wrong-code-secret", begun.attempt_token)
        recovered = manager.verify_code(self.CODE, begun.attempt_token)

        self.assertEqual("invalid_code", invalid.public["error_code"])
        self.assertTrue(recovered.public["ok"])
        self.assertEqual(2, client.sign_in_count)
        self.assertEqual(1, client.disconnect_count)
        self.assert_public_does_not_contain(
            invalid.public, "wrong-code-secret", self.API_HASH, self.PHONE
        )

    def test_attempt_token_prevents_cross_browser_verify_and_cancel(self):
        client = FakeClient()
        manager = self.make_manager(client)
        begun = manager.begin(self.API_ID, self.API_HASH, self.PHONE)

        denied_verify = manager.verify_code(self.CODE, "other-browser-token")
        malformed_verify = manager.verify_code(self.CODE, {"not": "a string"})
        denied_cancel = manager.cancel("other-browser-token")

        self.assertEqual("invalid_auth_attempt", denied_verify.public["error_code"])
        self.assertEqual("invalid_auth_attempt", malformed_verify.public["error_code"])
        self.assertEqual("invalid_auth_attempt", denied_cancel.public["error_code"])
        self.assertEqual(0, client.sign_in_count)
        self.assertTrue(client.connected)

        allowed_cancel = manager.cancel(begun.attempt_token)
        self.assertTrue(allowed_cancel.public["ok"])
        self.assertFalse(client.connected)

    def test_public_outcomes_never_expose_challenge_or_credentials(self):
        challenge = "phone-code-hash-must-stay-private"
        client = FakeClient(sent_code=FakeSentCode(phone_code_hash=challenge))
        manager = self.make_manager(client)

        begun = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
        verified = manager.verify_code(self.CODE, begun.attempt_token)

        self.assertNotIn("phone_code_hash", begun.public)
        self.assertNotIn("credentials", verified.public)
        self.assert_public_does_not_contain(
            begun.public, challenge, self.API_HASH, self.PHONE, self.CODE
        )
        self.assert_public_does_not_contain(
            verified.public, challenge, self.API_HASH, self.PHONE, self.CODE
        )

    def test_describe_sent_code_reports_delivery_next_channel_and_timeout(self):
        result = describe_sent_code(
            FakeSentCode(
                delivery_type=SentCodeTypeApp(length=5),
                next_type=CodeTypeSms(),
                timeout=47,
            )
        )

        self.assertEqual("una sesión activa de Telegram", result["delivery"])
        self.assertEqual("SentCodeTypeApp", result["delivery_type"])
        self.assertEqual("SMS", result["next_delivery"])
        self.assertEqual(47, result["timeout_seconds"])
        self.assertEqual(5, result["code_length"])

    def test_describe_sent_code_sanitizes_invalid_timeout_and_length(self):
        result = describe_sent_code(
            FakeSentCode(
                delivery_type=UnknownDeliveryType(length=99),
                timeout="not-a-number-or-secret",
            )
        )

        self.assertEqual("el canal elegido por Telegram", result["delivery"])
        self.assertEqual(0, result["timeout_seconds"])
        self.assertNotIn("code_length", result)
        self.assertNotIn("not-a-number-or-secret", json.dumps(result))

    def test_second_begin_is_suppressed_until_resend_window_opens(self):
        sent = FakeSentCode(timeout=45)
        client = FakeClient(sent_code=sent)
        factory = Factory(client)
        manager = TelegramAuthManager(
            "test-session", client_factory=factory, pending_ttl=120
        )
        self.managers.append(manager)

        first = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
        second = manager.begin(999999, "different-secret", "+571111111111")

        self.assertTrue(first.public["needs_code"])
        self.assertTrue(second.public["request_in_progress"])
        self.assertGreaterEqual(second.public["retry_after"], 0)
        self.assertLessEqual(second.public["retry_after"], 45)
        self.assertEqual(1, client.send_count)
        self.assertEqual(1, len(factory.calls))
        self.assert_public_does_not_contain(
            second.public,
            self.API_HASH,
            self.PHONE,
            "different-secret",
            "+571111111111",
            sent.phone_code_hash,
        )

    def test_request_timeout_is_publicly_described_without_internal_details(self):
        client = FakeClient(block_send=True)
        manager = self.make_manager(client, request_timeout=0.05)

        result = manager.begin(self.API_ID, self.API_HASH, self.PHONE)

        self.assertFalse(result.public["ok"])
        self.assertEqual("request_timeout", result.public["error_code"])
        self.assertIn("tardó demasiado", result.public["error"])
        self.assert_public_does_not_contain(
            result.public, self.API_HASH, self.PHONE, client.sent_code.phone_code_hash
        )
        self.assertFalse(client.connected)
        self.assertEqual(1, client.disconnect_count)

    def test_flood_wait_extends_resend_window_without_another_rpc(self):
        client = FakeClient(sent_code=FakeSentCode(timeout=1))
        manager = self.make_manager(client)

        first = manager.begin(self.API_ID, self.API_HASH, self.PHONE)
        self.assertTrue(first.public["needs_code"])
        manager._pending["resend_at"] = time.monotonic() - 1
        client.send_error = FloodWaitError("details must not escape", seconds=901)

        flooded = manager.begin(
            self.API_ID, self.API_HASH, self.PHONE, first.attempt_token
        )
        suppressed = manager.begin(
            self.API_ID, self.API_HASH, self.PHONE, first.attempt_token
        )

        self.assertEqual("flood_wait", flooded.public["error_code"])
        self.assertEqual(901, flooded.public["retry_after"])
        self.assertTrue(suppressed.public["request_in_progress"])
        self.assertGreaterEqual(suppressed.public["retry_after"], 900)
        self.assertGreater(manager._pending["expires_at"], manager._pending["resend_at"])
        self.assertEqual(2, client.send_count)

    def test_unknown_and_known_errors_are_sanitized(self):
        leaked = "api_hash=very-secret phone=+570001112233 code=99887"

        unknown = _error_outcome(SecretServerError(leaked))
        known = _error_outcome(ApiIdInvalidError(leaked))
        flood = _error_outcome(FloodWaitError(leaked, seconds=73))

        self.assertEqual("telegram_error", unknown.public["error_code"])
        self.assertEqual("invalid_api_credentials", known.public["error_code"])
        self.assertEqual("flood_wait", flood.public["error_code"])
        self.assertEqual(73, flood.public["retry_after"])
        for outcome in (unknown, known, flood):
            self.assert_public_does_not_contain(outcome.public, leaked, "very-secret", "99887")


if __name__ == "__main__":
    unittest.main()
