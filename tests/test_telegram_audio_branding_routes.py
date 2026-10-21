import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from tests.admin_session import grant_operator_admin, install_operator_key


class TelegramAudioBrandingRoutesTests(unittest.TestCase):
    CSRF = "b" * 48

    def setUp(self):
        self.operator_key = install_operator_key(self)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings_file = Path(self.temporary.name) / "telegram_audio_branding.json"

        settings_patcher = patch.object(
            app_module,
            "TG_AUDIO_BRANDING_SETTINGS_FILE",
            self.settings_file,
        )
        settings_patcher.start()
        self.addCleanup(settings_patcher.stop)

        environment_patcher = patch.dict(
            os.environ,
            {"TG_AUDIO_TITLE": "", "TG_AUDIO_PERFORMER": ""},
        )
        environment_patcher.start()
        self.addCleanup(environment_patcher.stop)

    def authorize_browser(self):
        grant_operator_admin(
            self.client,
            self.operator_key,
            csrf=self.CSRF,
        )

    def headers(self, token=None):
        return {"X-Channel-CSRF": token or self.CSRF}

    def test_get_returns_packaged_pecadosvip_value_without_internal_paths(self):
        response = self.client.get("/api/telegram_audio_branding")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"ok": True, "title": "PecadosVIP", "performer": "PecadosVIP"},
            response.get_json(),
        )
        self.assertEqual("no-store", response.headers["Cache-Control"])

    def test_write_requires_admin_browser_and_csrf(self):
        anonymous = self.client.post(
            "/api/telegram_audio_branding",
            json={"title": "Sin permiso", "performer": "No autorizado"},
            headers=self.headers(),
        )
        self.assertEqual(403, anonymous.status_code)
        self.assertEqual("admin_required", anonymous.get_json()["error_code"])
        self.assertFalse(self.settings_file.exists())

        self.authorize_browser()
        missing_csrf = self.client.post(
            "/api/telegram_audio_branding",
            json={"title": "Sin permiso", "performer": "Sin CSRF"},
        )
        self.assertEqual(403, missing_csrf.status_code)
        self.assertEqual("csrf_invalid", missing_csrf.get_json()["error_code"])
        self.assertFalse(self.settings_file.exists())

    def test_write_trims_persists_and_does_not_restart_workers(self):
        self.authorize_browser()
        with (
            patch.object(app_module, "restart_telegram_worker") as restart_telegram,
            patch.object(app_module, "restart_wa_bot") as restart_whatsapp,
        ):
            response = self.client.post(
                "/api/telegram_audio_branding",
                json={
                    "title": "  Experiencia Madrid  ",
                    "performer": "  Caché Madrid Centro  ",
                },
                headers=self.headers(),
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("Experiencia Madrid", response.get_json()["title"])
        self.assertEqual("Caché Madrid Centro", response.get_json()["performer"])
        self.assertEqual(
            {"title": "Experiencia Madrid", "performer": "Caché Madrid Centro"},
            json.loads(self.settings_file.read_text(encoding="utf-8")),
        )
        restart_telegram.assert_not_called()
        restart_whatsapp.assert_not_called()

    def test_invalid_and_empty_updates_do_not_replace_previous_setting(self):
        self.settings_file.write_text(
            json.dumps(
                {"title": "Las Fiesteras", "performer": "Caché Madrid"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        original = self.settings_file.read_bytes()
        self.authorize_browser()

        for payload in (
            {},
            {"title": "Madrid\nTG_API_HASH=injected", "performer": "Caché Madrid"},
            {"title": "Las Fiesteras", "performer": "Madrid\nTG_API_HASH=injected"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/telegram_audio_branding",
                    json=payload,
                    headers=self.headers(),
                )
                self.assertEqual(400, response.status_code)
                self.assertFalse(response.get_json()["ok"])
                self.assertEqual(original, self.settings_file.read_bytes())

    def test_partial_update_preserves_other_effective_value(self):
        self.authorize_browser()
        performer_only = self.client.post(
            "/api/telegram_audio_branding",
            json={"performer": "Agencia Madrid"},
            headers=self.headers(),
        )
        self.assertEqual(200, performer_only.status_code)
        self.assertEqual("PecadosVIP", performer_only.get_json()["title"])

        title_only = self.client.post(
            "/api/telegram_audio_branding",
            json={"title": "Título actualizado"},
            headers=self.headers(),
        )
        self.assertEqual(200, title_only.status_code)
        self.assertEqual("Agencia Madrid", title_only.get_json()["performer"])

    def test_get_filters_known_legacy_settings_without_rewriting_them(self):
        original = json.dumps({"title": "Las Fiesteras", "performer": "Caché Madrid"}).encode()
        self.settings_file.write_bytes(original)
        response = self.client.get("/api/telegram_audio_branding")
        self.assertEqual({"ok": True, "title": "PecadosVIP", "performer": "PecadosVIP"}, response.get_json())
        self.assertEqual(original, self.settings_file.read_bytes())

    def test_save_rejects_known_legacy_brand_and_preserves_previous_settings(self):
        original = json.dumps({"title": "Propio", "performer": "Agencia propia"}).encode()
        self.settings_file.write_bytes(original)
        self.authorize_browser()
        for payload in ({"title": "Las Fiesteras"}, {"performer": "Caché Madrid"}):
            with self.subTest(payload=payload):
                response = self.client.post("/api/telegram_audio_branding", json=payload, headers=self.headers())
                self.assertEqual(400, response.status_code)
                self.assertIn("PecadosVIP", response.get_json()["error"])
                self.assertEqual(original, self.settings_file.read_bytes())


if __name__ == "__main__":
    unittest.main()
