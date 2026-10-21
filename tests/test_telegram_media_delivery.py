"""Production sender functions with offline uploads and idempotent fake RPCs."""
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from telethon import errors
from telethon.tl import functions, types

from interaction_state import PersistentInteractionState
from telegram_audio_branding import brand_audio_attributes, build_branded_audio_media
from telegram_dispatcher import (
    TelegramInteractionDispatcher, build_telegram_media_request,
    build_telegram_text_request, deliver_telegram_response_components,
)


def sender_functions():
    source = Path(__file__).resolve().parents[1] / "bot.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"send_response", "_retry_telegram_operation", "_upload_file_cached", "_evict_upload_cache"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    if {node.name for node in nodes} != names:
        raise AssertionError("Production sender functions were not found")
    namespace = {
        "asyncio": SimpleNamespace(**vars(asyncio)), "logging": logging, "Path": Path,
        "types": types, "errors": errors, "telegram_identity_verified": True,
        # Scaled budgets keep the real upload-vs-RPC scheduling test fast.
        "TELEGRAM_SEND_RETRIES": 3, "TELEGRAM_SEND_TIMEOUT_SECONDS": 0.015,
        "TELEGRAM_UPLOAD_TIMEOUT_SECONDS": 0.2,
        "_uploaded_media_cache": {}, "load_messages_fresh": Mock(),
        "build_telegram_media_request": build_telegram_media_request,
        "build_telegram_text_request": build_telegram_text_request,
        "deliver_telegram_response_components": deliver_telegram_response_components,
        "brand_audio_attributes": brand_audio_attributes,
        "build_branded_audio_media": build_branded_audio_media,
        # Fixtures need no codec or provider; real SDK media/request types remain in use.
        "utils": SimpleNamespace(get_attributes=lambda filename, **kwargs: (
            [types.DocumentAttributeFilename(Path(filename).name)],
            "video/mp4" if filename.endswith(".mp4") else "audio/mpeg",
        )),
    }
    namespace["asyncio"].sleep = AsyncMock()
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


class FakeProvider:
    def __init__(self):
        self.requests = []
        self.uploads = []
        self.remote_messages = {}
        self.upload_action = None
        self.rpc_action = None

    async def get_input_entity(self, peer):
        return types.InputPeerSelf()

    async def upload_file(self, filename):
        self.uploads.append(Path(filename).name)
        if self.upload_action:
            await self.upload_action(filename)
        return types.InputFile(id=len(self.uploads), parts=1, name=Path(filename).name, md5_checksum="0" * 32)

    def acknowledge(self, request):
        # Model Telegram's documented deduplication by random_id.
        self.remote_messages.setdefault(request.random_id, request)
        return SimpleNamespace(id=request.random_id)

    async def __call__(self, request):
        self.requests.append(request)
        if self.rpc_action:
            return await self.rpc_action(request)
        return self.acknowledge(request)


class TelegramMediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.prepare_fixture()

    def prepare_fixture(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.worker = sender_functions()
        self.provider = FakeProvider()
        self.health = {}
        self.health_history = []
        self.audio_path = root / "audios" / "fixture.mp3"
        self.video_path = root / "videos" / "fixture.mp4"
        self.cover_path = root / "cover.jpg"
        self.audio_path.parent.mkdir()
        self.video_path.parent.mkdir()
        self.video_path.write_bytes(b"synthetic fixture, never sent")

        def update_interaction_health(**kwargs):
            self.health.update(kwargs)

        def update_delivery_health(component, state):
            self.health["delivery"][component] = state
            self.health_history.append((component, state))

        self.worker.update({
            "client": self.provider, "AUDIO_DIR": self.audio_path.parent,
            "VIDEO_DIR": self.video_path.parent, "AUDIO_COVER_PATH": self.cover_path,
            "AUDIO_BRANDING_DEFAULTS_FILE": root / "absent-defaults.json",
            "AUDIO_BRANDING_SETTINGS_FILE": root / "absent-settings.json",
            "get_response_components": lambda *_: ("", "", self.video_path.name),
            "update_interaction_health": update_interaction_health,
            "update_delivery_health": update_delivery_health,
        })
        self.state = PersistentInteractionState(root / "interaction-state.json")
        self.dispatcher = TelegramInteractionDispatcher(self.state, self.worker["send_response"])

    async def dispatch(self, event="fixture-event"):
        return await self.dispatcher.dispatch(chat_id=1, event_id=event, kind="content")

    def preview(self, event="fixture-event"):
        return self.state.preview(contact_id=1, event_id=event, kind="content")

    def configure_audio(self, *, cover=False):
        self.audio_path.write_bytes(b"synthetic audio, never sent")
        if cover:
            self.cover_path.write_bytes(b"synthetic cover, never sent")
        self.worker["get_response_components"] = lambda *_: ("", self.audio_path.name, "")

    async def test_slow_upload_uses_separate_budget_then_advances_once(self):
        async def slow_upload(_filename):
            self.assertFalse(self.provider.requests)
            self.assertEqual("step1", self.preview().response_key)
            await asyncio.sleep(0.05)  # Longer than RPC budget, below upload budget.

        self.provider.upload_action = slow_upload
        decision = await self.dispatch()
        duplicate = await self.dispatch()
        self.assertEqual(1, decision.phase)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(["fixture.mp4"], self.provider.uploads)
        self.assertEqual(1, len(self.provider.requests))
        self.assertEqual("sent", self.health["delivery"]["video"])
        self.assertIn(("video", "uploading"), self.health_history)

    async def test_upload_timeout_never_sends_or_advances(self):
        async def blocked_upload(_filename):
            await asyncio.Event().wait()

        self.provider.upload_action = blocked_upload
        self.worker["TELEGRAM_UPLOAD_TIMEOUT_SECONDS"] = 0.015
        with self.assertRaisesRegex(RuntimeError, "telegram_video_upload_failed"):
            await self.dispatch()
        self.assertFalse(self.provider.requests)
        self.assertEqual(["fixture.mp4"], self.provider.uploads)
        self.assertFalse(self.worker["_uploaded_media_cache"])
        self.assertEqual("failed", self.health["delivery"]["video"])
        self.assertEqual("step1", self.preview().response_key)

    async def test_lost_ack_reuses_handle_and_random_id_despite_cache_eviction(self):
        async def lose_first_ack(request):
            result = self.provider.acknowledge(request)
            if len(self.provider.requests) == 1:
                self.worker["_uploaded_media_cache"].clear()
                await asyncio.Event().wait()
            return result

        self.provider.rpc_action = lose_first_ack
        await self.dispatch()
        self.assertEqual(2, len(self.provider.requests))
        first, second = self.provider.requests
        self.assertEqual(first.random_id, second.random_id)
        self.assertIs(first.media.file, second.media.file)
        self.assertEqual(["fixture.mp4"], self.provider.uploads)
        self.assertEqual(1, len(self.provider.remote_messages))
        self.assertIn(("video", "uncertain"), self.health_history)
        self.assertEqual("sent", self.health["delivery"]["video"])

    async def test_unacknowledged_delivery_stays_uncertain_and_does_not_advance(self):
        async def lose_every_ack(request):
            self.provider.acknowledge(request)
            await asyncio.Event().wait()

        self.provider.rpc_action = lose_every_ack
        with self.assertRaisesRegex(RuntimeError, "telegram_video_delivery_failed"):
            await self.dispatch()
        self.assertEqual(3, len(self.provider.requests))
        self.assertEqual(1, len({request.random_id for request in self.provider.requests}))
        self.assertEqual(1, len(self.provider.remote_messages))
        self.assertEqual(["fixture.mp4"], self.provider.uploads)
        self.assertEqual("uncertain", self.health["delivery"]["video"])
        self.assertEqual("step1", self.preview().response_key)

    async def test_video_lost_ack_still_delivers_text_audio_and_retry_commits_without_duplicates(self):
        self.audio_path.write_bytes(b"synthetic audio, never sent")
        self.worker["get_response_components"] = lambda *_: (
            "fixture text", self.audio_path.name, self.video_path.name,
        )

        def component(request):
            if isinstance(request, functions.messages.SendMessageRequest):
                return "text"
            return "video" if request.media.mime_type == "video/mp4" else "audio"

        async def lose_video_ack(request):
            result = self.provider.acknowledge(request)
            if component(request) == "video":
                await asyncio.Event().wait()
            return result

        self.provider.rpc_action = lose_video_ack
        with self.assertRaisesRegex(RuntimeError, "telegram_video_delivery_failed"):
            await self.dispatch()
        self.assertEqual(
            ["video", "video", "video", "text", "audio"],
            [component(request) for request in self.provider.requests],
        )
        self.assertEqual(
            {"video": "uncertain", "text": "sent", "audio": "sent"},
            {key: self.health["delivery"][key] for key in ("video", "text", "audio")},
        )
        self.assertFalse(self.preview().duplicate)
        self.assertEqual("step1", self.preview().response_key)
        self.assertEqual(3, len(self.provider.remote_messages))
        first_ids = {component(request): request.random_id for request in self.provider.requests}
        self.assertEqual(3, len(set(first_ids.values())))

        self.provider.rpc_action = None
        decision = await self.dispatch()
        self.assertEqual(1, decision.phase)
        self.assertTrue(self.preview().duplicate)
        self.assertEqual(
            ["video", "text", "audio"],
            [component(request) for request in self.provider.requests[-3:]],
        )
        self.assertEqual(
            first_ids,
            {component(request): request.random_id for request in self.provider.requests[-3:]},
        )
        self.assertEqual(3, len(self.provider.remote_messages))
        self.assertEqual(["fixture.mp4", "fixture.mp3"], self.provider.uploads)
        request_count = len(self.provider.requests)
        self.assertTrue((await self.dispatch()).duplicate)
        self.assertEqual(request_count, len(self.provider.requests))

    async def test_uncertainty_survives_later_rejection_or_prepare_failure(self):
        async def ambiguous_then_rejected(request):
            if len(self.provider.requests) == 1:
                self.provider.acknowledge(request)
                await asyncio.Event().wait()
            self.video_path.unlink()
            raise errors.BadRequestError(request, "FILE_PART_INVALID")

        self.provider.rpc_action = ambiguous_then_rejected
        with self.assertRaisesRegex(RuntimeError, "telegram_video_upload_failed"):
            await self.dispatch()
        self.assertEqual("uncertain", self.health["delivery"]["video"])
        self.assertEqual("step1", self.preview().response_key)
        uncertainty = self.health_history.index(("video", "uncertain"))
        self.assertTrue(all(state == "uncertain" for component, state in self.health_history[uncertainty:] if component == "video"))

    async def test_sdk_timeouts_and_exhausted_retry_errors_keep_cover_and_handle(self):
        for error_type in (errors.TimeoutError, errors.ServerError, ValueError):
            with self.subTest(error=error_type.__name__):
                self.prepare_fixture()
                self.configure_audio(cover=True)

                async def fail_first(request):
                    if len(self.provider.requests) == 1:
                        if error_type is ValueError:
                            raise ValueError("Request was unsuccessful after SDK retries")
                        if error_type is errors.ServerError:
                            raise errors.ServerError(request, "INTERNAL")
                        raise errors.TimeoutError(request)
                    return self.provider.acknowledge(request)

                self.provider.rpc_action = fail_first
                await self.dispatch()
                self.assertEqual(2, len(self.provider.requests))
                first, second = self.provider.requests
                self.assertEqual(first.random_id, second.random_id)
                self.assertIs(first.media.file, second.media.file)
                self.assertIsNotNone(first.media.thumb)
                self.assertIs(first.media.thumb, second.media.thumb)
                self.assertEqual(["fixture.mp3", "cover.jpg"], self.provider.uploads)
                self.assertIn(("audio", "uncertain"), self.health_history)

    async def test_cover_rejection_falls_back_without_changing_random_id(self):
        self.configure_audio(cover=True)

        async def reject_cover(request):
            if request.media.thumb is not None:
                raise errors.PhotoInvalidDimensionsError(request)
            return self.provider.acknowledge(request)

        self.provider.rpc_action = reject_cover
        await self.dispatch()
        first, second = self.provider.requests
        self.assertEqual(first.random_id, second.random_id)
        self.assertIs(first.media.file, second.media.file)
        self.assertIsNone(second.media.thumb)
        self.assertEqual(1, len(self.provider.remote_messages))
        self.assertEqual("sent", self.health["delivery"]["audio"])

    async def test_lost_ack_after_cover_rejection_retries_without_cover_and_same_id(self):
        self.configure_audio(cover=True)

        async def reject_cover_then_lose_ack(request):
            if len(self.provider.requests) == 1:
                raise errors.PhotoInvalidDimensionsError(request)
            result = self.provider.acknowledge(request)
            if len(self.provider.requests) == 2:
                await asyncio.Event().wait()
            return result

        self.provider.rpc_action = reject_cover_then_lose_ack
        await self.dispatch()
        self.assertEqual(3, len(self.provider.requests))
        self.assertEqual(1, len({request.random_id for request in self.provider.requests}))
        self.assertEqual([True, False, False], [request.media.thumb is not None for request in self.provider.requests])
        self.assertEqual(["fixture.mp3", "cover.jpg"], self.provider.uploads)
        self.assertEqual(1, len(self.provider.remote_messages))
        self.assertIn(("audio", "uncertain"), self.health_history)
        self.assertEqual("sent", self.health["delivery"]["audio"])

    async def test_identity_is_checked_before_upload_and_again_before_rpc(self):
        self.worker["telegram_identity_verified"] = False
        with self.assertRaisesRegex(RuntimeError, "identity is not verified"):
            await self.worker["_upload_file_cached"](self.video_path)
        self.assertFalse(self.provider.uploads)
        self.worker["telegram_identity_verified"] = True

        async def revoke_identity(_filename):
            self.worker["telegram_identity_verified"] = False

        self.provider.upload_action = revoke_identity
        with self.assertRaisesRegex(RuntimeError, "identity is not verified"):
            await self.dispatch()
        self.assertFalse(self.provider.requests)
        self.assertEqual("step1", self.preview().response_key)

    async def test_cancellation_during_upload_or_rpc_never_advances(self):
        for phase in ("upload", "rpc"):
            with self.subTest(phase=phase):
                self.prepare_fixture()
                started = asyncio.Event()

                async def wait_until_cancelled(_value):
                    started.set()
                    await asyncio.Event().wait()

                if phase == "upload":
                    self.provider.upload_action = wait_until_cancelled
                else:
                    self.provider.rpc_action = wait_until_cancelled
                task = asyncio.create_task(self.dispatch())
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual("step1", self.preview().response_key)
                self.assertEqual("failed" if phase == "upload" else "uncertain", self.health["delivery"]["video"])
                self.assertEqual(0 if phase == "upload" else 1, len(self.provider.requests))

    async def test_missing_named_audio_does_not_suppress_video_or_confirm_partial_delivery(self):
        self.worker["get_response_components"] = lambda *_: ("fixture text", self.audio_path.name, self.video_path.name)
        with self.assertRaises(FileNotFoundError):
            await self.dispatch()
        self.assertEqual("missing", self.health["delivery"]["audio"])
        self.assertEqual("sent", self.health["delivery"]["video"])
        self.assertEqual("step1", self.preview().response_key)
        first_ids = [request.random_id for request in self.provider.requests]
        self.assertEqual(2, len(first_ids))

        self.audio_path.write_bytes(b"synthetic replacement, never sent")
        await self.dispatch()
        self.assertTrue(self.preview().duplicate)
        self.assertEqual(3, len(self.provider.remote_messages))
        self.assertEqual(first_ids[0], self.provider.requests[2].random_id)
        self.assertEqual(first_ids[1], self.provider.requests[3].random_id)
