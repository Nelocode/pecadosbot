import os
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as app_module
from telegram_auth import AuthOutcome


class TelegramRoutesTestCase(unittest.TestCase):
    API_ID = 123456
    API_HASH = "0123456789abcdef0123456789abcdef"
    PHONE = "+570000000000"
    ATTEMPT = "opaque-browser-attempt-token"
    CODE = "24680"
    OPERATOR_KEY = "telegram-route-test-key-" + ("K" * 32)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_patcher = patch.object(app_module, "DATA_DIR", Path(temporary.name))
        data_patcher.start()
        self.addCleanup(data_patcher.stop)
        operator_key_patcher = patch.object(
            app_module,
            "_configured_operator_recovery_key",
            return_value=self.OPERATOR_KEY,
        )
        operator_key_patcher.start()
        self.addCleanup(operator_key_patcher.stop)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.csrf = "t" * 48
        self.authorize_browser()
        self.client.environ_base["HTTP_X_CHANNEL_CSRF"] = self.csrf

    def authorize_browser(self):
        with self.client.session_transaction() as browser_session:
            browser_session["channel_csrf"] = self.csrf
            browser_session["channel_admin"] = True
            browser_session["operator_recovery_key_version"] = (
                app_module._operator_recovery_key_version(self.OPERATOR_KEY)
            )
            browser_session["operator_recovery_verified_at"] = time.time()

    def assert_response_does_not_contain_secrets(self, response, *secrets):
        body = response.get_data(as_text=True)
        for secret in secrets:
            self.assertNotIn(str(secret), body)
        data = response.get_json()
        self.assertNotIn("phone", data)
        self.assertNotIn("api_hash", data)
        self.assertNotIn("phone_code_hash", data)

    def test_tg_status_distinguishes_configured_session_and_ready_without_secrets(self):
        cases = (
            (False, False, False, "unconfigured", False),
            (True, False, False, "needs_verification", False),
            (True, True, False, "authorized_offline", True),
            (True, True, True, "ready", True),
        )

        for configured, session, running, state, linked in cases:
            with self.subTest(state=state):
                values = {
                    "TG_API_ID": str(self.API_ID),
                    "TG_API_HASH": self.API_HASH,
                    "TG_PHONE": self.PHONE,
                } if configured else {}
                with (
                    patch.dict(os.environ, values, clear=True),
                    patch.object(app_module, "_read_env_var", return_value=None),
                    patch.object(
                        app_module,
                        "_telegram_session_is_authorized",
                        return_value=session,
                    ),
                    patch.object(app_module, "bot_is_running", return_value=running),
                ):
                    response = self.client.get("/api/tg_status")

                self.assertEqual(200, response.status_code)
                data = response.get_json()
                self.assertEqual(configured, data["configured"])
                self.assertEqual(session, data["session_authorized"])
                self.assertEqual(linked, data["linked"])
                self.assertEqual(running, data["worker_running"])
                self.assertEqual(state, data["state"])
                self.assert_response_does_not_contain_secrets(
                    response, self.API_HASH, self.PHONE
                )

    def test_invalid_link_input_does_not_invoke_manager_persist_or_restart(self):
        manager = Mock()
        with (
            patch.object(app_module, "_telegram_auth", manager),
            patch.object(app_module, "_save_telegram_creds") as save_creds,
            patch.object(app_module, "_write_telegram_authorized_marker") as write_marker,
            patch.object(app_module, "restart_telegram_worker") as restart,
            patch.object(
                app_module, "_telegram_session_is_authorized", return_value=False
            ),
        ):
            response = self.client.post(
                "/api/link_telegram",
                json={
                    "api_id": "not-a-number",
                    "api_hash": "too-short-and-secret",
                    "phone": "not-a-phone",
                },
            )

        self.assertEqual(400, response.status_code)
        self.assertEqual("invalid_input", response.get_json()["error_code"])
        manager.begin.assert_not_called()
        save_creds.assert_not_called()
        write_marker.assert_not_called()
        restart.assert_not_called()
        self.assert_response_does_not_contain_secrets(
            response, "too-short-and-secret", "not-a-phone"
        )

    def test_invalid_code_and_password_do_not_invoke_manager_or_persist(self):
        manager = Mock()
        with (
            patch.object(app_module, "_telegram_auth", manager),
            patch.object(app_module, "_save_telegram_creds") as save_creds,
            patch.object(app_module, "restart_telegram_worker") as restart,
        ):
            code_response = self.client.post(
                "/api/verify_telegram_code", json={"code": "12ab$secret"}
            )
            password_response = self.client.post(
                "/api/verify_telegram_password", json={"password": ""}
            )

        self.assertEqual(400, code_response.status_code)
        self.assertEqual(400, password_response.status_code)
        manager.verify_code.assert_not_called()
        manager.verify_password.assert_not_called()
        save_creds.assert_not_called()
        restart.assert_not_called()
        self.assertNotIn("12ab$secret", code_response.get_data(as_text=True))

    def test_needs_code_response_does_not_expose_internal_challenge(self):
        outcome = AuthOutcome(
            {
                "ok": True,
                "needs_code": True,
                "delivery": "una sesión activa de Telegram",
                "delivery_type": "SentCodeTypeApp",
                "timeout_seconds": 30,
            },
            attempt_token=self.ATTEMPT,
        )
        manager = Mock()
        manager.begin.return_value = outcome

        with (
            patch.object(app_module, "_telegram_auth", manager),
            patch.object(
                app_module, "_telegram_session_is_authorized", return_value=False
            ),
            patch.object(app_module, "_save_telegram_creds") as save_creds,
            patch.object(app_module, "restart_telegram_worker") as restart,
        ):
            response = self.client.post(
                "/api/link_telegram",
                json={
                    "api_id": self.API_ID,
                    "api_hash": self.API_HASH,
                    "phone": self.PHONE,
                },
            )

        self.assertEqual(200, response.status_code)
        data = response.get_json()
        self.assertTrue(data["needs_code"])
        self.assertEqual(30, data["timeout_seconds"])
        manager.begin.assert_called_once_with(
            self.API_ID, self.API_HASH, self.PHONE, None
        )
        save_creds.assert_not_called()
        restart.assert_not_called()
        self.assert_response_does_not_contain_secrets(
            response, self.API_HASH, self.PHONE
        )

    def test_authorized_result_persists_credentials_and_restarts_worker(self):
        outcome = AuthOutcome(
            {"ok": True},
            credentials=(self.API_ID, self.API_HASH, self.PHONE),
        )
        manager = Mock()
        manager.verify_code.return_value = outcome

        with (
            patch.object(app_module, "_telegram_auth", manager),
            patch.object(app_module, "_save_telegram_creds") as save_creds,
            patch.object(app_module, "_write_telegram_authorized_marker") as write_marker,
            patch.object(
                app_module,
                "restart_telegram_worker",
                return_value=(True, "Telegram iniciado"),
            ) as restart,
            patch.dict(os.environ, {}, clear=True),
        ):
            response = self.client.post(
                "/api/verify_telegram_code",
                json={"code": self.CODE, "auth_attempt": self.ATTEMPT},
            )
            self.assertEqual(str(self.API_ID), os.environ.get("TG_API_ID"))
            self.assertEqual(self.API_HASH, os.environ.get("TG_API_HASH"))
            self.assertEqual(self.PHONE, os.environ.get("TG_PHONE"))

        self.assertEqual(200, response.status_code)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["authorized"])
        self.assertTrue(data["worker_starting"])
        with self.client.session_transaction() as browser_session:
            self.assertTrue(browser_session.get("channel_admin"))
            self.assertTrue(browser_session.permanent)
        manager.verify_code.assert_called_once_with(self.CODE, self.ATTEMPT)
        save_creds.assert_called_once_with(
            self.API_ID, self.API_HASH, self.PHONE
        )
        write_marker.assert_called_once_with()
        restart.assert_called_once_with()
        self.assert_response_does_not_contain_secrets(
            response, self.API_HASH, self.PHONE, self.CODE
        )

    def test_restart_requires_the_browser_that_authorized_telegram(self):
        with self.client.session_transaction() as browser_session:
            browser_session.pop("channel_admin", None)
        with patch.object(app_module, "restart_telegram_worker") as restart:
            response = self.client.post("/api/restart_bot")

        self.assertEqual(403, response.status_code)
        self.assertFalse(response.get_json()["ok"])
        restart.assert_not_called()

    def test_linked_telegram_credentials_never_bootstrap_panel_authority(self):
        payload = {
            "api_id": self.API_ID,
            "api_hash": self.API_HASH,
            "phone": self.PHONE,
        }
        with self.client.session_transaction() as browser_session:
            browser_session.pop("channel_admin", None)
        with (
            patch.dict(
                os.environ,
                {
                    "TG_API_ID": str(self.API_ID),
                    "TG_API_HASH": self.API_HASH,
                    "TG_PHONE": self.PHONE.removeprefix("+"),
                },
                clear=True,
            ),
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "_valid_telegram_credentials") as validate,
            patch.object(app_module, "bot_is_running") as running,
            patch.object(app_module, "_telegram_auth") as manager,
            patch.object(app_module, "restart_telegram_worker") as restart,
            patch.object(app_module, "_save_telegram_creds") as save_creds,
            patch.object(app_module, "_write_telegram_authorized_marker") as write_marker,
        ):
            response = self.client.post("/api/link_telegram", json=payload)

        self.assertEqual(403, response.status_code)
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual("admin_required", data["error_code"])
        validate.assert_not_called()
        running.assert_not_called()
        manager.begin.assert_not_called()
        restart.assert_not_called()
        save_creds.assert_not_called()
        write_marker.assert_not_called()
        with self.client.session_transaction() as browser_session:
            self.assertFalse(browser_session.get("channel_admin"))
            self.assertEqual(self.csrf, browser_session.get("channel_csrf"))

    def test_linked_telegram_cannot_restart_from_an_unauthorized_browser(self):
        with self.client.session_transaction() as browser_session:
            browser_session.pop("channel_admin", None)
        with (
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "_valid_telegram_credentials") as validate,
            patch.object(app_module, "bot_is_running") as running,
            patch.object(app_module, "_telegram_auth") as manager,
            patch.object(app_module, "restart_telegram_worker") as restart,
        ):
            response = self.client.post(
                "/api/link_telegram",
                json={"api_id": self.API_ID, "api_hash": self.API_HASH, "phone": self.PHONE},
            )

        self.assertEqual(403, response.status_code)
        self.assertEqual("admin_required", response.get_json()["error_code"])
        validate.assert_not_called()
        running.assert_not_called()
        manager.begin.assert_not_called()
        restart.assert_not_called()

    def test_admin_browser_uses_change_account_instead_of_relinking(self):
        payload = {
            "api_id": self.API_ID,
            "api_hash": self.API_HASH,
            "phone": self.PHONE,
        }
        self.authorize_browser()
        with (
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "bot_is_running", return_value=True),
            patch.object(app_module, "_telegram_auth") as manager,
            patch.object(app_module, "restart_telegram_worker") as restart,
        ):
            response = self.client.post("/api/link_telegram", json=payload)

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["already_authorized"])
        manager.begin.assert_not_called()
        restart.assert_not_called()

    def test_linked_admin_browser_can_restart_an_offline_worker(self):
        payload = {
            "api_id": self.API_ID,
            "api_hash": self.API_HASH,
            "phone": self.PHONE,
        }
        with (
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "bot_is_running", return_value=False),
            patch.object(app_module, "_telegram_auth") as manager,
            patch.object(
                app_module,
                "restart_telegram_worker",
                return_value=(True, "Telegram iniciado"),
            ) as restart,
        ):
            self.authorize_browser()
            response = self.client.post("/api/link_telegram", json=payload)

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["already_authorized"])
        self.assertTrue(response.get_json()["worker_starting"])
        manager.begin.assert_not_called()
        restart.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
