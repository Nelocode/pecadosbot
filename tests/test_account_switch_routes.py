import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from tests.admin_session import grant_operator_admin, install_operator_key


class AccountSwitchRoutesTestCase(unittest.TestCase):
    CSRF = "c" * 48

    def setUp(self):
        self.operator_key = install_operator_key(self)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

        self.data_dir = Path(self.temporary.name) / "data"
        self.wa_auth_dir = self.data_dir / "wa_auth"
        self.wa_identity_file = self.data_dir / "wa_identity.json"
        self.wa_switch_dir = self.data_dir / "wa_switch"

        replacements = {
            "DATA_DIR": self.data_dir,
            "WA_CALL_HEALTH_FILE": self.data_dir / "wa_call_health.json",
            "WA_AUTH_DIR": self.wa_auth_dir,
            "WA_IDENTITY_FILE": self.wa_identity_file,
            "WA_SWITCH_DIR": self.wa_switch_dir,
            "WA_SWITCH_AUTH_DIR": self.wa_switch_dir / "candidate_auth",
            "WA_SWITCH_QR_FILE": self.wa_switch_dir / "qr.png",
            "WA_SWITCH_HEALTH_FILE": self.wa_switch_dir / "health.json",
            "WA_SWITCH_IDENTITY_FILE": self.wa_switch_dir / "identity.json",
            "WA_SWITCH_PID_FILE": self.wa_switch_dir / "worker.pid",
            "WA_SWITCH_OPERATION_FILE": self.wa_switch_dir / "operation.json",
            "WA_SWITCH_RECOVERY_ROOT": self.data_dir / ".wa_switch_recovery",
        }
        for name, value in replacements.items():
            patcher = patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def authorize_browser(self):
        grant_operator_admin(
            self.client,
            self.operator_key,
            csrf=self.CSRF,
        )

    def mutation_headers(self, token=None):
        return {"X-Channel-CSRF": token or self.CSRF}

    def seed_active_whatsapp(self, contents="old-account-session"):
        self.wa_auth_dir.mkdir(parents=True, exist_ok=True)
        credentials = self.wa_auth_dir / "creds.json"
        credentials.write_text(contents, encoding="utf-8")
        return credentials

    def test_channel_mutations_require_admin_browser_and_valid_csrf(self):
        with (
            patch.object(app_module, "restart_wa_bot") as restart,
            patch.object(app_module, "_start_wa_process") as start_switch,
        ):
            anonymous = self.client.post(
                "/api/restart_wa_bot",
                headers=self.mutation_headers(),
            )
            self.assertEqual(403, anonymous.status_code)
            self.assertEqual("admin_required", anonymous.get_json()["error_code"])
            anonymous_switch = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )
            self.assertEqual(403, anonymous_switch.status_code)
            self.assertEqual("admin_required", anonymous_switch.get_json()["error_code"])

            self.authorize_browser()
            missing_csrf = self.client.post("/api/restart_wa_bot")
            invalid_csrf = self.client.post(
                "/api/restart_wa_bot",
                headers=self.mutation_headers("x" * 48),
            )

        self.assertEqual(403, missing_csrf.status_code)
        self.assertEqual("csrf_invalid", missing_csrf.get_json()["error_code"])
        self.assertEqual(403, invalid_csrf.status_code)
        self.assertEqual("csrf_invalid", invalid_csrf.get_json()["error_code"])
        restart.assert_not_called()
        start_switch.assert_not_called()
        self.assertFalse(self.wa_switch_dir.exists())

    def test_restart_whatsapp_preserves_active_auth_directory(self):
        credentials = self.seed_active_whatsapp()
        self.authorize_browser()

        with (
            patch.object(app_module, "_stop_wa_process") as stop,
            patch.object(app_module, "_start_wa_process", return_value=4321) as start,
        ):
            response = self.client.post(
                "/api/restart_wa_bot",
                headers=self.mutation_headers(),
            )

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))
        stop.assert_called_once_with(self.data_dir / "wa_bot.pid")
        self.assertEqual(self.wa_auth_dir, start.call_args.kwargs["auth_dir"])
        self.assertFalse(start.call_args.kwargs["link_only"])

    def test_restart_rejects_a_session_that_requires_a_new_qr(self):
        credentials = self.seed_active_whatsapp()
        self.authorize_browser()

        with (
            patch.object(
                app_module,
                "_read_wa_call_health",
                return_value={"reauth_required": True, "disconnect_reason": "logged_out"},
            ),
            patch.object(app_module, "restart_wa_bot") as restart,
        ):
            response = self.client.post(
                "/api/restart_wa_bot",
                headers=self.mutation_headers(),
            )

        self.assertEqual(409, response.status_code)
        self.assertEqual("reauth_required", response.get_json()["error_code"])
        self.assertIn("Volver a vincular", response.get_json()["error"])
        restart.assert_not_called()
        self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))

    def test_open_connection_wins_over_stale_logout_health_when_restarting(self):
        self.seed_active_whatsapp()
        self.authorize_browser()
        stale_health = {
            "connection": "open",
            "reauth_required": True,
            "disconnect_reason": "logged_out",
        }
        with (
            patch.object(app_module, "_read_wa_call_health", return_value=stale_health),
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "restart_wa_bot", return_value=9753) as restart,
        ):
            response = self.client.post(
                "/api/restart_wa_bot",
                headers=self.mutation_headers(),
            )

        self.assertEqual(200, response.status_code)
        restart.assert_called_once_with()

    def test_whatsapp_status_requires_an_open_socket_not_only_a_live_pid(self):
        self.seed_active_whatsapp()
        with (
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(
                app_module,
                "_read_wa_call_health",
                return_value={"connection": "closed"},
            ),
        ):
            self.assertFalse(app_module.wa_is_running())

        with (
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(
                app_module,
                "_read_wa_call_health",
                return_value={"connection": "open"},
            ),
        ):
            self.assertTrue(app_module.wa_is_running())

    def test_legacy_reset_endpoint_is_gone_and_cannot_delete_session(self):
        credentials = self.seed_active_whatsapp()

        with patch.object(app_module, "_stop_wa_process") as stop:
            response = self.client.post("/api/reset_wa")

        self.assertEqual(410, response.status_code)
        self.assertEqual(
            "use_transactional_switch",
            response.get_json()["error_code"],
        )
        self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))
        stop.assert_not_called()

    def test_whatsapp_switch_start_and_cancel_leave_old_account_active(self):
        credentials = self.seed_active_whatsapp()
        self.authorize_browser()

        with (
            patch.object(app_module, "_start_wa_process", return_value=9876) as start,
            patch.object(app_module, "_stop_wa_process") as stop,
        ):
            started = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )

            self.assertEqual(202, started.status_code)
            self.assertEqual("preparing", started.get_json()["state"])
            self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))
            self.assertTrue(app_module.WA_SWITCH_AUTH_DIR.is_dir())

            operation = json.loads(
                app_module.WA_SWITCH_OPERATION_FILE.read_text(encoding="utf-8")
            )
            self.assertIn("token_hash", operation)
            self.assertNotIn("token", operation)
            with self.client.session_transaction() as browser_session:
                attempt_token = browser_session.get("wa_switch_token")
            self.assertIsInstance(attempt_token, str)
            self.assertNotIn(attempt_token, app_module.WA_SWITCH_OPERATION_FILE.read_text(encoding="utf-8"))

            cancelled = self.client.post(
                "/api/switch_wa/cancel",
                headers=self.mutation_headers(),
            )

        self.assertEqual(200, cancelled.status_code)
        self.assertTrue(cancelled.get_json()["ok"])
        self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))
        self.assertFalse(self.wa_switch_dir.exists())
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("wa_switch_token", browser_session)
        self.assertEqual(app_module.WA_SWITCH_AUTH_DIR, start.call_args.kwargs["auth_dir"])
        self.assertTrue(start.call_args.kwargs["link_only"])
        stop.assert_called_with(app_module.WA_SWITCH_PID_FILE)

    def test_switch_requires_explicit_confirmation_before_starting_worker(self):
        self.authorize_browser()
        with patch.object(app_module, "_start_wa_process") as start:
            response = self.client.post(
                "/api/switch_wa",
                json={"confirm": False},
                headers=self.mutation_headers(),
            )
            invalid_payload = self.client.post(
                "/api/switch_wa",
                json=[],
                headers=self.mutation_headers(),
            )

        self.assertEqual(400, response.status_code)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(400, invalid_payload.status_code)
        start.assert_not_called()

    def test_claim_requires_admin_csrf_and_explicit_confirmation(self):
        anonymous = self.client.post(
            "/api/switch_wa/claim",
            json={"confirm": True},
            headers=self.mutation_headers(),
        )
        self.assertEqual(403, anonymous.status_code)
        self.assertEqual("admin_required", anonymous.get_json()["error_code"])

        self.authorize_browser()
        missing_csrf = self.client.post(
            "/api/switch_wa/claim",
            json={"confirm": True},
        )
        not_confirmed = self.client.post(
            "/api/switch_wa/claim",
            json={"confirm": False},
            headers=self.mutation_headers(),
        )
        invalid_payload = self.client.post(
            "/api/switch_wa/claim",
            json=[],
            headers=self.mutation_headers(),
        )
        self.assertEqual(403, missing_csrf.status_code)
        self.assertEqual("csrf_invalid", missing_csrf.get_json()["error_code"])
        self.assertEqual(400, not_confirmed.status_code)
        self.assertEqual(400, invalid_payload.status_code)

    def test_pending_recovery_blocks_another_whatsapp_switch(self):
        self.seed_active_whatsapp()
        self.authorize_browser()
        recovery = app_module.WA_SWITCH_RECOVERY_ROOT / "preserved"
        recovery.mkdir(parents=True)
        (recovery / "creds.json").write_text("backup", encoding="utf-8")

        with patch.object(app_module, "_start_wa_process") as start:
            response = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )

        self.assertEqual(409, response.status_code)
        self.assertEqual("recovery_required", response.get_json()["error_code"])
        start.assert_not_called()
        self.assertEqual("backup", (recovery / "creds.json").read_text(encoding="utf-8"))
        self.assertFalse(self.wa_switch_dir.exists())

    def test_logged_out_state_is_visible_and_points_to_reauthentication(self):
        self.seed_active_whatsapp()
        with (
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "bot_is_running", return_value=True),
            patch.object(app_module, "_wa_process_running", return_value=False),
            patch.object(app_module, "_wa_connection_open", return_value=False),
            patch.object(
                app_module,
                "_read_wa_call_health",
                return_value={"reauth_required": True, "disconnect_reason": "logged_out"},
            ),
            patch.object(app_module, "_load_wa_switch_operation", return_value=None),
        ):
            anonymous = self.client.get("/api/channels")
            self.authorize_browser()
            administrator = self.client.get("/api/channels")

        for response in (anonymous, administrator):
            self.assertEqual(200, response.status_code)
            self.assertEqual("no-store, private", response.headers["Cache-Control"])
            whatsapp = response.get_json()["whatsapp"]
            self.assertTrue(whatsapp["linked"])
            self.assertFalse(whatsapp["ready"])
            self.assertFalse(whatsapp["worker_running"])
            self.assertTrue(whatsapp["reauth_required"])
            self.assertEqual("reauth_required", whatsapp["state"])

    def test_stale_logout_health_does_not_hide_an_unlinked_account(self):
        with (
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "bot_is_running", return_value=True),
            patch.object(app_module, "_wa_process_running", return_value=False),
            patch.object(app_module, "_wa_connection_open", return_value=False),
            patch.object(
                app_module,
                "_read_wa_call_health",
                return_value={"reauth_required": True, "disconnect_reason": "logged_out"},
            ),
            patch.object(app_module, "_load_wa_switch_operation", return_value=None),
        ):
            response = self.client.get("/api/channels")

        whatsapp = response.get_json()["whatsapp"]
        self.assertFalse(whatsapp["linked"])
        self.assertFalse(whatsapp["reauth_required"])
        self.assertEqual("unlinked", whatsapp["state"])

    def test_open_connection_wins_over_stale_logout_health_in_channel_state(self):
        self.seed_active_whatsapp()
        self.authorize_browser()
        with (
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "bot_is_running", return_value=True),
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "_wa_connection_open", return_value=True),
            patch.object(
                app_module,
                "_read_wa_call_health",
                return_value={"reauth_required": True, "disconnect_reason": "logged_out"},
            ),
            patch.object(app_module, "_load_wa_switch_operation", return_value=None),
        ):
            response = self.client.get("/api/channels")

        whatsapp = response.get_json()["whatsapp"]
        self.assertTrue(whatsapp["ready"])
        self.assertFalse(whatsapp["reauth_required"])
        self.assertEqual("ready", whatsapp["state"])

    def test_foreign_active_switch_is_not_mistaken_for_an_owned_qr(self):
        credentials = self.seed_active_whatsapp()
        self.authorize_browser()
        self.wa_switch_dir.mkdir(parents=True)
        app_module._save_wa_switch_operation({
            "version": 1,
            "token_hash": app_module._wa_switch_token_digest("another-browser"),
            "started_at": time.time(),
            "status": "preparing",
        })
        app_module.WA_SWITCH_QR_FILE.write_bytes(b"foreign-qr")

        with (
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "_wa_connection_open", return_value=False),
            patch.object(app_module, "_start_wa_process") as start,
        ):
            response = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )
            channels = self.client.get("/api/channels")
            qr = self.client.get("/api/switch_wa/qr")

        self.assertEqual(409, response.status_code)
        payload = response.get_json()
        self.assertEqual("switch_in_progress", payload["error_code"])
        self.assertFalse(payload["owned_by_this_browser"])
        self.assertGreater(payload["retry_after"], 0)
        self.assertEqual("switching_elsewhere", channels.get_json()["whatsapp"]["state"])
        self.assertEqual(404, qr.status_code)
        start.assert_not_called()
        self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))

    def test_owned_active_switch_can_resume_polling(self):
        self.authorize_browser()
        token = "current-browser"
        self.wa_switch_dir.mkdir(parents=True)
        app_module._save_wa_switch_operation({
            "version": 1,
            "token_hash": app_module._wa_switch_token_digest(token),
            "started_at": time.time(),
            "status": "preparing",
        })
        with self.client.session_transaction() as browser_session:
            browser_session["wa_switch_token"] = token

        with (
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "_start_wa_process") as start,
        ):
            response = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )

        self.assertEqual(409, response.status_code)
        self.assertTrue(response.get_json()["owned_by_this_browser"])
        self.assertEqual("switching", response.get_json()["state"])
        start.assert_not_called()

    def test_dead_foreign_switch_is_replaced_without_waiting_for_ttl(self):
        credentials = self.seed_active_whatsapp()
        self.authorize_browser()
        self.wa_switch_dir.mkdir(parents=True)
        app_module.WA_SWITCH_AUTH_DIR.mkdir()
        (app_module.WA_SWITCH_AUTH_DIR / "creds.json").write_text("dead-candidate", encoding="utf-8")
        app_module.WA_SWITCH_QR_FILE.write_bytes(b"stale-qr")
        app_module._save_wa_switch_operation({
            "version": 1,
            "token_hash": app_module._wa_switch_token_digest("dead-browser"),
            "started_at": time.time(),
            "status": "preparing",
        })

        with (
            patch.object(app_module, "_wa_process_running", return_value=False),
            patch.object(app_module, "_start_wa_process", return_value=2468) as start,
            patch.object(app_module, "_schedule_wa_switch_expiry"),
        ):
            response = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )

        self.assertEqual(202, response.status_code)
        self.assertFalse(app_module.WA_SWITCH_QR_FILE.exists())
        self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))
        start.assert_called_once()
        self.assertTrue(start.call_args.kwargs["link_only"])
        operation = app_module._load_wa_switch_operation()
        with self.client.session_transaction() as browser_session:
            token = browser_session["wa_switch_token"]
        self.assertEqual(app_module._wa_switch_token_digest(token), operation["token_hash"])

    def test_live_foreign_switch_expires_then_allows_a_clean_retry(self):
        credentials = self.seed_active_whatsapp()
        self.authorize_browser()
        self.wa_switch_dir.mkdir(parents=True)
        app_module.WA_SWITCH_QR_FILE.write_bytes(b"old-qr")
        app_module._save_wa_switch_operation({
            "version": 1,
            "token_hash": app_module._wa_switch_token_digest("foreign-browser"),
            "started_at": time.time(),
            "status": "preparing",
        })

        with (
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "_start_wa_process") as start,
        ):
            blocked = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )
        self.assertEqual(409, blocked.status_code)
        start.assert_not_called()

        with (
            patch.object(app_module, "WA_SWITCH_TIMEOUT_SECONDS", 0),
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "_start_wa_process", return_value=8642) as restart,
            patch.object(app_module, "_schedule_wa_switch_expiry"),
        ):
            retried = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )

        self.assertEqual(202, retried.status_code)
        restart.assert_called_once()
        self.assertFalse(app_module.WA_SWITCH_QR_FILE.exists())
        self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))
        with self.client.session_transaction() as browser_session:
            current_token = browser_session["wa_switch_token"]
        self.assertEqual(
            app_module._wa_switch_token_digest(current_token),
            app_module._load_wa_switch_operation()["token_hash"],
        )

    def test_claim_keeps_the_original_deadline(self):
        operation = {
            "version": 1,
            "token_hash": app_module._wa_switch_token_digest("claimed"),
            "started_at": 1_000.0,
            "status": "preparing",
        }
        app_module._cancel_wa_switch_expiry()
        with (
            patch.object(app_module.time, "time", return_value=1_120.0),
            patch.object(app_module.threading, "Timer") as timer_factory,
        ):
            app_module._schedule_wa_switch_expiry(operation)

        delay = timer_factory.call_args.args[0]
        self.assertAlmostEqual(60.0, delay, places=2)
        timer_factory.return_value.start.assert_called_once_with()
        app_module._cancel_wa_switch_expiry()

    def test_admin_can_claim_a_foreign_qr_without_touching_active_auth(self):
        credentials = self.seed_active_whatsapp()
        self.authorize_browser()
        previous_token = "previous-browser"
        previous_client = app_module.app.test_client()
        grant_operator_admin(
            previous_client,
            self.operator_key,
            csrf=self.CSRF,
        )
        with previous_client.session_transaction() as browser_session:
            browser_session["wa_switch_token"] = previous_token

        self.wa_switch_dir.mkdir(parents=True)
        app_module.WA_SWITCH_QR_FILE.write_bytes(b"candidate-qr")
        original_started_at = time.time()
        app_module._save_wa_switch_operation({
            "version": 1,
            "token_hash": app_module._wa_switch_token_digest(previous_token),
            "started_at": original_started_at,
            "status": "preparing",
        })

        with (
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "_wa_connection_open", return_value=False),
            patch.object(app_module, "_schedule_wa_switch_expiry") as schedule,
        ):
            claimed = self.client.post(
                "/api/switch_wa/claim",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )
            current_qr = self.client.get("/api/switch_wa/qr")
            previous_status = previous_client.get("/api/switch_wa/status")

        self.assertEqual(200, claimed.status_code)
        self.assertTrue(claimed.get_json()["ok"])
        self.assertTrue(claimed.get_json()["qr_ready"])
        self.assertEqual(app_module._wa_qr_revision(), claimed.get_json()["qr_revision"])
        self.assertEqual(200, current_qr.status_code)
        current_qr.close()
        self.assertEqual(404, previous_status.status_code)
        self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))
        operation = app_module._load_wa_switch_operation()
        with self.client.session_transaction() as browser_session:
            current_token = browser_session["wa_switch_token"]
        self.assertEqual(app_module._wa_switch_token_digest(current_token), operation["token_hash"])
        self.assertNotEqual(app_module._wa_switch_token_digest(previous_token), operation["token_hash"])
        self.assertEqual(original_started_at, operation["started_at"])
        schedule.assert_called_once()

    def test_status_revision_changes_only_when_qr_content_changes(self):
        self.authorize_browser()
        token = "current-browser"
        self.wa_switch_dir.mkdir(parents=True)
        app_module.WA_SWITCH_QR_FILE.write_bytes(b"first-qr")
        app_module._save_wa_switch_operation({
            "version": 1,
            "token_hash": app_module._wa_switch_token_digest(token),
            "started_at": time.time(),
            "status": "preparing",
        })
        with self.client.session_transaction() as browser_session:
            browser_session["wa_switch_token"] = token

        with (
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "_wa_connection_open", return_value=False),
        ):
            first = self.client.get("/api/switch_wa/status").get_json()
            second = self.client.get("/api/switch_wa/status").get_json()
            app_module.WA_SWITCH_QR_FILE.write_bytes(b"second-qr")
            changed = self.client.get("/api/switch_wa/status").get_json()

        self.assertTrue(first["qr_ready"])
        self.assertEqual("awaiting_qr", first["state"])
        self.assertEqual(first["qr_revision"], second["qr_revision"])
        self.assertNotEqual(first["qr_revision"], changed["qr_revision"])
        self.assertRegex(first["qr_revision"], r"^[0-9a-f]{24}$")

    def test_status_is_read_only_and_commit_promotes_once(self):
        self.seed_active_whatsapp()
        self.authorize_browser()
        with patch.object(app_module, "_start_wa_process", return_value=9876):
            started = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )
        self.assertEqual(202, started.status_code)

        def promote_and_finish_operation():
            # La promoción real elimina el intento y su token de navegador. El
            # doble POST debe encontrar el cambio ya consumido, no promoverlo
            # una segunda vez.
            app_module._cleanup_wa_switch_candidate()
            return True, "cambiada", {}, True, True

        with (
            patch.object(app_module, "_wa_connection_open", return_value=True),
            patch.object(
                app_module,
                "_promote_wa_candidate",
                side_effect=promote_and_finish_operation,
            ) as promote,
        ):
            first_status = self.client.get("/api/switch_wa/status")
            second_status = self.client.get("/api/switch_wa/status")
            self.assertEqual("scanned", first_status.get_json()["state"])
            self.assertEqual("scanned", second_status.get_json()["state"])
            promote.assert_not_called()

            committed = self.client.post(
                "/api/switch_wa/commit",
                headers=self.mutation_headers(),
            )
            repeated_commit = self.client.post(
                "/api/switch_wa/commit",
                headers=self.mutation_headers(),
            )

        self.assertEqual(200, committed.status_code)
        self.assertEqual("ready", committed.get_json()["state"])
        self.assertEqual(404, repeated_commit.status_code)
        promote.assert_called_once_with()

    def test_abandoned_whatsapp_candidate_expires_without_polling(self):
        credentials = self.seed_active_whatsapp()
        self.authorize_browser()
        with (
            patch.object(app_module, "WA_SWITCH_TIMEOUT_SECONDS", 0.05),
            patch.object(app_module, "_start_wa_process", return_value=9876),
        ):
            started = self.client.post(
                "/api/switch_wa",
                json={"confirm": True},
                headers=self.mutation_headers(),
            )
            self.assertEqual(202, started.status_code)
            deadline = time.monotonic() + 1
            while self.wa_switch_dir.exists() and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(self.wa_switch_dir.exists())
        self.assertEqual("old-account-session", credentials.read_text(encoding="utf-8"))

    def test_channels_summary_does_not_expose_credentials_or_raw_identifiers(self):
        telegram_api_id = "87654321"
        telegram_api_hash = "0123456789abcdef0123456789abcdef"
        telegram_phone = "+573001234567"
        whatsapp_secret = "noise-key-material-should-never-leave-disk"
        whatsapp_phone = "573009876543"

        self.seed_active_whatsapp(
            json.dumps({"noiseKey": whatsapp_secret, "me": {"id": whatsapp_phone}})
        )
        self.wa_identity_file.write_text(
            json.dumps({"display_name": "Cuenta comercial", "phone_hint": "••••6543"}),
            encoding="utf-8",
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tg_identity.json").write_text(
            json.dumps({"display_name": "Telegram comercial", "username": "marca"}),
            encoding="utf-8",
        )
        self.authorize_browser()

        with (
            patch.dict(
                os.environ,
                {
                    "TG_API_ID": telegram_api_id,
                    "TG_API_HASH": telegram_api_hash,
                    "TG_PHONE": telegram_phone,
                },
                clear=True,
            ),
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "bot_is_running", return_value=False),
            patch.object(app_module, "wa_is_running", return_value=False),
            patch.object(app_module, "_wa_connection_open", return_value=False),
            patch.object(app_module, "_load_wa_switch_operation", return_value=None),
        ):
            response = self.client.get("/api/channels")

        self.assertEqual(200, response.status_code)
        data = response.get_json()
        body = response.get_data(as_text=True)
        for secret in (
            telegram_api_id,
            telegram_api_hash,
            telegram_phone,
            whatsapp_secret,
            whatsapp_phone,
            "creds.json",
            "noiseKey",
        ):
            self.assertNotIn(secret, body)

        self.assertEqual(
            {"ok", "csrf", "can_manage", "telegram", "whatsapp"},
            set(data),
        )
        self.assertFalse({"api_id", "api_hash", "phone", "token"} & set(data["telegram"]))
        self.assertFalse({"credentials", "jid", "phone", "token"} & set(data["whatsapp"]))
        self.assertEqual("••••4567", data["telegram"]["phone_hint"])
        self.assertEqual("••••6543", data["whatsapp"]["phone_hint"])

    def test_telegram_interaction_health_is_allowlisted_and_identifier_free(self):
        secret_id = "573001234567"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tg_interaction_health.json").write_text(
            json.dumps({
                "schema_version": 1,
                "worker_revision": "00000000-0000-4000-8000-000000000001",
                "connection": "open",
                "raw_phone_revision": "00000000-0000-4000-8000-000000000002",
                "phone_subtype": "discarded",
                "phone_revisions": {
                    "requested": "00000000-0000-4000-8000-000000000004",
                    "discarded": "00000000-0000-4000-8000-000000000005",
                    "caller": secret_id,
                },
                "call_reject_revision": "00000000-0000-4000-8000-000000000006",
                "call_reject_status": "sent",
                "service_call_status": "processed",
                "service_peer_source": "update_entities",
                "missed_call_poll": "healthy",
                "classified_revision": "00000000-0000-4000-8000-000000000003",
                "last_kind": "call",
                "last_response": "call",
                "delivery": {
                    "peer_resolution": "sent",
                    "text": "sent",
                    "audio": "uploading",
                    "video": "uncertain",
                },
                "caller_id": secret_id,
                "message": "private content",
            }),
            encoding="utf-8",
        )

        with patch.object(app_module, "bot_is_running", return_value=True):
            response = self.client.get("/api/tg_interaction_health")

        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        self.assertNotIn(secret_id, body)
        self.assertNotIn("private content", body)
        data = response.get_json()
        self.assertEqual("discarded", data["phone_subtype"])
        self.assertEqual(
            "00000000-0000-4000-8000-000000000004",
            data["phone_revisions"]["requested"],
        )
        self.assertEqual(
            "00000000-0000-4000-8000-000000000005",
            data["phone_revisions"]["discarded"],
        )
        self.assertNotIn("caller", data["phone_revisions"])
        self.assertEqual("sent", data["call_reject_status"])
        self.assertEqual(
            "00000000-0000-4000-8000-000000000006",
            data["call_reject_revision"],
        )
        self.assertEqual("processed", data["service_call_status"])
        self.assertEqual("update_entities", data["service_peer_source"])
        self.assertEqual("healthy", data["missed_call_poll"])
        self.assertEqual("uploading", data["delivery"]["audio"])
        self.assertEqual("uncertain", data["delivery"]["video"])


if __name__ == "__main__":
    unittest.main()
