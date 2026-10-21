import os
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telethon.tl import types

from telegram_audio_branding import (
    AUDIO_PERFORMER,
    AUDIO_TITLE,
    brand_audio_attributes,
    build_branded_audio_media,
    resolve_audio_branding,
    resolve_audio_cover_path,
    save_audio_branding_settings,
)


class TelegramAudioBrandingTests(unittest.TestCase):
    def setUp(self):
        self.branding_environment = patch.dict(
            os.environ,
            {
                "TG_AUDIO_TITLE": "",
                "TG_AUDIO_PERFORMER": "",
                "TG_AUDIO_COVER_PATH": "",
            },
        )
        self.branding_environment.start()

    def tearDown(self):
        self.branding_environment.stop()

    def test_pecadosvip_packaged_defaults_and_panel_override_have_clear_precedence(self):
        packaged = Path(__file__).parents[1] / "telegram_audio_branding.defaults.json"
        self.assertEqual(
            ("PecadosVIP", "PecadosVIP"),
            resolve_audio_branding(environ={}, defaults_path=packaged),
        )
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "telegram_audio_branding.json"
            with patch.dict(
                os.environ,
                {
                    "TG_AUDIO_TITLE": "Título del servidor",
                    "TG_AUDIO_PERFORMER": "Agencia del servidor",
                },
            ):
                save_audio_branding_settings(
                    settings,
                    title="  Título del panel  ",
                    performer="  Caché Madrid Centro  ",
                )
                title, performer = resolve_audio_branding(
                    defaults_path=packaged,
                    settings_path=settings,
                )
        self.assertEqual("Título del panel", title)
        self.assertEqual("Caché Madrid Centro", performer)

    def test_invalid_panel_setting_does_not_replace_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "telegram_audio_branding.json"
            save_audio_branding_settings(
                settings,
                title="Título anterior",
                performer="Agencia anterior",
            )
            original = settings.read_bytes()
            for invalid in ("", "x" * 81, "Madrid\nTG_API_HASH=injected", "Madrid\x00x"):
                with self.subTest(invalid=repr(invalid)):
                    with self.assertRaises(ValueError):
                        save_audio_branding_settings(
                            settings,
                            title=invalid,
                            performer="Caché Madrid",
                        )
                    self.assertEqual(original, settings.read_bytes())

    def test_invalid_environment_labels_fall_back_to_pecadosvip_defaults(self):
        self.assertEqual(
            ("PecadosVIP", "PecadosVIP"),
            resolve_audio_branding(
                environ={
                    "TG_AUDIO_TITLE": "Madrid\ninyectado",
                    "TG_AUDIO_PERFORMER": "x" * 81,
                }
            ),
        )

    def test_known_legacy_environment_labels_fall_back_without_mutating_environment(self):
        environ = {"TG_AUDIO_TITLE": "  LAS  FIESTERAS ", "TG_AUDIO_PERFORMER": "caché madrid"}
        before = dict(environ)
        self.assertEqual(("PecadosVIP", "PecadosVIP"), resolve_audio_branding(environ=environ))
        self.assertEqual(before, environ)

    def test_known_legacy_files_fall_back_without_rewriting_persistent_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            original = json.dumps({"title": "Las Fiesteras", "performer": "Caché Madrid"}).encode()
            settings.write_bytes(original)
            self.assertEqual(("PecadosVIP", "PecadosVIP"), resolve_audio_branding(
                environ={}, defaults_path=settings, settings_path=settings,
            ))
            self.assertEqual(original, settings.read_bytes())

    def test_legacy_setting_does_not_hide_unrelated_environment_customization(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({"title": "Las Fiesteras", "performer": "Caché Madrid"}), encoding="utf-8")
            self.assertEqual(("Título propio", "Agencia Madrid"), resolve_audio_branding(
                environ={"TG_AUDIO_TITLE": "Título propio", "TG_AUDIO_PERFORMER": "Agencia Madrid"},
                settings_path=settings,
            ))

    def test_save_rejects_exact_legacy_brand_without_overwriting_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            save_audio_branding_settings(settings, title="Propio", performer="Agencia Madrid")
            original = settings.read_bytes()
            for title, performer in (("Las Fiesteras", "Propio"), ("Propio", "  CACHÉ MADRID ")):
                with self.subTest(title=title, performer=performer):
                    with self.assertRaisesRegex(ValueError, "PecadosVIP"):
                        save_audio_branding_settings(settings, title=title, performer=performer)
                    self.assertEqual(original, settings.read_bytes())

    def test_branding_settings_are_reloaded_for_each_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "telegram_audio_branding.json"
            save_audio_branding_settings(settings, title="Primero", performer="Madrid Uno")
            first = brand_audio_attributes([], filename="es_msg1.mp3", environ={}, settings_path=settings)
            save_audio_branding_settings(settings, title="Segundo", performer="Madrid Dos")
            second = brand_audio_attributes([], filename="es_msg2.mp3", environ={}, settings_path=settings)
        first_audio = next(item for item in first if isinstance(item, types.DocumentAttributeAudio))
        second_audio = next(item for item in second if isinstance(item, types.DocumentAttributeAudio))
        self.assertEqual(("Primero", "Madrid Uno"), (first_audio.title, first_audio.performer))
        self.assertEqual(("Segundo", "Madrid Dos"), (second_audio.title, second_audio.performer))

    def test_cover_path_can_be_selected_per_deployment(self):
        base_dir = Path("/app")
        self.assertEqual(
            base_dir / "assets" / "audio-cover.jpg",
            resolve_audio_cover_path(base_dir, environ={}),
        )
        self.assertEqual(
            base_dir / "assets" / "madrid-cover.jpg",
            resolve_audio_cover_path(
                base_dir,
                environ={"TG_AUDIO_COVER_PATH": "assets/madrid-cover.jpg"},
            ),
        )

    def test_preserves_duration_and_filename_while_enforcing_brand(self):
        attributes = [
            types.DocumentAttributeFilename(file_name="es_msg2.mp3"),
            types.DocumentAttributeAudio(
                duration=21,
                voice=True,
                title="old",
                performer="old",
            ),
        ]

        branded = brand_audio_attributes(attributes, filename="ignored.mp3")

        filename = next(
            item for item in branded
            if isinstance(item, types.DocumentAttributeFilename)
        )
        audio = next(
            item for item in branded
            if isinstance(item, types.DocumentAttributeAudio)
        )
        self.assertEqual("es_msg2.mp3", filename.file_name)
        self.assertEqual(21, audio.duration)
        self.assertFalse(audio.voice)
        self.assertEqual(AUDIO_TITLE, audio.title)
        self.assertEqual(AUDIO_PERFORMER, audio.performer)

    def test_adds_filename_and_zero_duration_when_probe_has_no_audio_metadata(self):
        branded = brand_audio_attributes([], filename="fr_call.mp3")

        self.assertTrue(any(
            isinstance(item, types.DocumentAttributeFilename)
            and item.file_name == "fr_call.mp3"
            for item in branded
        ))
        audio = next(
            item for item in branded
            if isinstance(item, types.DocumentAttributeAudio)
        )
        self.assertEqual(0, audio.duration)
        self.assertFalse(audio.voice)

    def test_media_payload_contains_cover_without_becoming_a_document(self):
        audio_file = types.InputFile(
            id=1,
            parts=1,
            name="es_msg2.mp3",
            md5_checksum="",
        )
        cover_file = types.InputFile(
            id=2,
            parts=1,
            name="audio-cover.jpg",
            md5_checksum="",
        )
        attributes = brand_audio_attributes([], filename="es_msg2.mp3")

        media = build_branded_audio_media(
            uploaded_file=audio_file,
            uploaded_thumb=cover_file,
            mime_type="audio/mpeg",
            attributes=attributes,
        )

        self.assertIs(audio_file, media.file)
        self.assertIs(cover_file, media.thumb)
        self.assertEqual("audio/mpeg", media.mime_type)
        self.assertFalse(media.force_file)

    def test_packaged_cover_is_a_small_jpeg(self):
        cover = Path(__file__).parents[1] / "assets" / "audio-cover.jpg"
        payload = cover.read_bytes()

        self.assertLessEqual(len(payload), 20_000)
        self.assertTrue(payload.startswith(b"\xff\xd8"))
        self.assertTrue(payload.endswith(b"\xff\xd9"))

    def test_packaged_cover_matches_reviewed_official_logo_derivative(self):
        assets = Path(__file__).parents[1] / "assets"
        self.assertEqual(
            "0413728e94269b3e433fa46ea014a75d7afbb6cbe63f3dc551346b7d3ea2f3c8",
            hashlib.sha256((assets / "pecadosvip-profile-logo.png").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            "ed668aa3761d2d782a95469cede220767b5ee8241b89dbf69e1a484e656d5696",
            hashlib.sha256((assets / "audio-cover.jpg").read_bytes()).hexdigest(),
        )

    def test_known_legacy_cover_content_is_blocked_even_if_renamed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "renamed-cover.jpg"
            candidate.write_bytes(b"legacy fixture")
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            with patch("telegram_audio_branding._LEGACY_COVER_SHA256", frozenset({digest})):
                resolved = resolve_audio_cover_path(root, environ={"TG_AUDIO_COVER_PATH": str(candidate)})
            self.assertEqual(root / "assets" / "audio-cover.jpg", resolved)
            self.assertEqual(b"legacy fixture", candidate.read_bytes())

    def test_unrelated_cover_override_remains_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "own-cover.jpg"
            candidate.write_bytes(b"unrelated fixture")
            self.assertEqual(candidate, resolve_audio_cover_path(
                root, environ={"TG_AUDIO_COVER_PATH": "own-cover.jpg"},
            ))

    def test_real_worker_reads_both_branding_files_for_each_audio(self):
        source = (Path(__file__).parents[1] / "bot.py").read_text(encoding="utf-8")
        self.assertIn("defaults_path=AUDIO_BRANDING_DEFAULTS_FILE", source)
        self.assertIn("settings_path=AUDIO_BRANDING_SETTINGS_FILE", source)


if __name__ == "__main__":
    unittest.main()
