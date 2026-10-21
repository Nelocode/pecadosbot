"""Inherited configuration cannot redirect the panel's WA worker to another brand."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import app as app_module


class InstanceWorkerPathsTests(unittest.TestCase):
    def test_container_validates_data_before_loading_settings_or_copying_defaults(self):
        source = (Path(__file__).parents[1] / "entrypoint.sh").read_text(encoding="utf-8")
        guard = source.index('resolve_instance_data_directory("/app")')
        first_data_access = source.index('mkdir -p /app/data')
        self.assertLess(guard, first_data_access)
        self.assertIn("|| exit 1", source[guard:first_data_access])
        self.assertNotIn("/tmp/bot_", source)

    def test_panel_overwrites_inherited_data_paths_without_revealing_operator_key(self):
        inherited = {
            name: "/another-brand/data" for name in (
                "BOT_DIR", "WA_AUTH_DIR", "WA_QR_PATH", "WA_HEALTH_FILE",
                "WA_IDENTITY_FILE", "WA_SAFETY_HEALTH_FILE", "WA_SAFETY_CONTROL_FILE",
                "WA_INTERACTION_STATE_FILE", "WA_VOICE_NOTE_CACHE_DIR",
                "WA_PROFILE_PICTURE_STATE_FILE",
            )
        }
        inherited[app_module.PANEL_ADMIN_RECOVERY_KEY_ENV] = "fixture-key"
        with patch.dict(os.environ, inherited, clear=True):
            result = app_module._channel_worker_environment()
        self.assertNotIn(app_module.PANEL_ADMIN_RECOVERY_KEY_ENV, result)
        self.assertEqual(str(app_module.BASE_DIR), result["BOT_DIR"])
        self.assertEqual(str(app_module.WA_AUTH_DIR), result["WA_AUTH_DIR"])
        self.assertEqual(str(app_module.BASE_DIR / "wa_qr.png"), result["WA_QR_PATH"])
        for key in inherited.keys() - {app_module.PANEL_ADMIN_RECOVERY_KEY_ENV, "BOT_DIR", "WA_QR_PATH"}:
            self.assertTrue(Path(result[key]).is_relative_to(app_module.DATA_DIR), key)

    def test_candidate_keeps_its_own_paths_and_never_inherits_main_credentials_directory(self):
        with patch.dict(os.environ, {"WA_AUTH_DIR": "/another-brand/auth"}, clear=True):
            result = app_module._channel_worker_environment({
                "WA_AUTH_DIR": str(app_module.WA_SWITCH_AUTH_DIR),
                "WA_IDENTITY_FILE": str(app_module.WA_SWITCH_IDENTITY_FILE),
                "WA_LINK_ONLY": "1",
            })
        self.assertEqual(str(app_module.WA_SWITCH_AUTH_DIR), result["WA_AUTH_DIR"])
        self.assertEqual(str(app_module.WA_SWITCH_IDENTITY_FILE), result["WA_IDENTITY_FILE"])
        self.assertEqual("1", result["WA_LINK_ONLY"])


if __name__ == "__main__":
    unittest.main()
