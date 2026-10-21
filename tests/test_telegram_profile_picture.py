"""Offline tests: no real Telegram client, credentials, or session is loaded."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import telegram_profile_picture as pictures


PHONE = "+57 300 000 0001"
OTHER_PHONE = "+57 300 000 0002"
PNG = b"\x89PNG\r\n\x1a\nexisting-logo-fixture"


class FakeClient:
    def __init__(self):
        self.upload_file = AsyncMock(return_value="uploaded-file")
        self.apply = AsyncMock(return_value=SimpleNamespace(photo=SimpleNamespace(id=42)))
        self.get_profile_photos = AsyncMock(return_value=[SimpleNamespace(id=42)])

    async def __call__(self, request):
        return await self.apply(request)


class TelegramProfilePictureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.image = self.root / "logo.png"
        self.image.write_bytes(PNG)
        self.me = SimpleNamespace(phone="573000000001", id=123, bot=False)
        self.client = FakeClient()
        self.env = patch.dict(os.environ, {"TG_PROFILE_PICTURE_ENABLED": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.photo_request = patch.object(pictures, "_photo_request", side_effect=lambda uploaded: ("photo", uploaded))
        self.photo_request.start()
        self.addCleanup(self.photo_request.stop)

    def queue(self, phone=PHONE):
        return pictures.request_profile_picture(self.data, phone, self.image)

    def read_request(self):
        return json.loads((self.data / pictures.REQUEST_FILE).read_text(encoding="utf-8"))

    def result_path(self, request=None):
        return self.data / pictures.RESULTS_DIR / ((request or self.read_request())["request_id"] + ".json")

    async def process(self, **kwargs):
        return await pictures.process_profile_picture(self.client, self.me, self.data, self.image, **kwargs)

    async def test_success_requires_provider_photo_and_current_photo_match(self):
        self.assertEqual(self.queue()["status"], "pending")
        request = self.read_request()
        self.assertEqual(request["account_hash"], hashlib.sha256(b"573000000001").hexdigest())
        self.assertEqual(request["image_hash"], hashlib.sha256(PNG).hexdigest())
        result = await self.process()
        self.assertEqual(result["status"], "applied")
        self.client.get_profile_photos.assert_awaited_once_with(self.me, limit=1)
        self.assertEqual(pictures.public_profile_picture_status(self.data, PHONE, self.image)["status"], "applied")
        self.assertTrue(set(result) <= {"status", "message", "updated_at"})
        self.assertNotIn("573000000001", json.dumps(result))
        self.assertEqual(self.queue()["status"], "applied")
        self.assertEqual(self.read_request()["request_id"], request["request_id"])
        await self.process()
        self.client.apply.assert_awaited_once()

    async def test_no_request_leaves_existing_personal_account_untouched(self):
        self.assertEqual((await self.process())["status"], "not_requested")
        self.client.upload_file.assert_not_awaited()
        self.assertFalse(self.data.exists())

    async def test_wrong_account_does_not_consume_request(self):
        self.queue(OTHER_PHONE)
        self.assertEqual((await self.process())["status"], "not_requested")
        self.assertFalse(self.result_path().exists())
        self.client.upload_file.assert_not_awaited()
        self.assertEqual(pictures.public_profile_picture_status(self.data, PHONE, self.image)["status"], "not_requested")

    async def test_absent_phone_and_bot_accounts_do_not_upload(self):
        self.queue()
        for me in (SimpleNamespace(phone=None), SimpleNamespace(phone=self.me.phone, bot=True)):
            status = await pictures.process_profile_picture(self.client, me, self.data, self.image)
            self.assertEqual(status["status"], "not_requested")
        self.client.upload_file.assert_not_awaited()

    async def test_upload_error_is_failed_and_sanitized_without_retry(self):
        self.queue()
        self.client.upload_file.side_effect = RuntimeError("private phone, secret path and token")
        result = await self.process()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", json.dumps(result))
        self.client.apply.assert_not_awaited()
        await self.process()
        self.client.upload_file.assert_awaited_once()

    async def test_provider_write_error_is_unverified_without_retry(self):
        self.queue()
        self.client.apply.side_effect = RuntimeError("connection lost after write")
        self.assertEqual((await self.process())["status"], "unverified")
        self.assertEqual((await self.process())["status"], "unverified")
        self.client.apply.assert_awaited_once()

    async def test_timeout_does_not_retry_even_after_new_callback_or_other_account(self):
        self.queue()

        async def hang(request):
            await asyncio.Event().wait()

        self.client.apply.side_effect = hang
        self.assertEqual((await self.process(timeout_seconds=0.01))["status"], "unverified")
        self.assertEqual((await self.process())["status"], "unverified")
        self.assertEqual(self.queue()["status"], "unverified")
        self.queue(OTHER_PHONE)
        self.assertEqual(self.queue()["status"], "unverified")
        self.assertEqual(pictures.public_profile_picture_status(self.data, PHONE, self.image)["status"], "unverified")
        self.assertEqual(pictures.public_profile_picture_status(self.data, OTHER_PHONE, self.image)["status"], "pending")
        self.client.apply.assert_awaited_once()

    async def test_photo_verification_mismatch_is_not_success(self):
        self.queue()
        self.client.get_profile_photos.return_value = [SimpleNamespace(id=99)]
        self.assertEqual((await self.process())["status"], "unverified")

    async def test_missing_provider_photo_id_is_not_success(self):
        self.queue()
        self.client.apply.return_value = SimpleNamespace(photo=None)
        self.assertEqual((await self.process())["status"], "unverified")
        self.client.get_profile_photos.assert_not_awaited()

    async def test_new_request_during_upload_is_preserved_and_old_not_applied(self):
        self.queue()
        old_request = self.read_request()

        async def replace_request(image):
            self.queue(OTHER_PHONE)
            return "uploaded"

        self.client.upload_file.side_effect = replace_request
        self.assertEqual((await self.process())["status"], "failed")
        self.assertNotEqual(self.read_request()["request_id"], old_request["request_id"])
        self.assertEqual(pictures.public_profile_picture_status(self.data, OTHER_PHONE, self.image)["status"], "pending")
        self.client.apply.assert_not_awaited()

    async def test_new_request_during_apply_is_not_overwritten_by_old_result(self):
        self.queue()
        old_request = self.read_request()

        async def replace_request(request):
            self.queue(OTHER_PHONE)
            return SimpleNamespace(photo=SimpleNamespace(id=42))

        self.client.apply.side_effect = replace_request
        self.assertEqual((await self.process())["status"], "applied")
        self.assertNotEqual(self.read_request()["request_id"], old_request["request_id"])
        self.assertEqual(pictures.public_profile_picture_status(self.data, OTHER_PHONE, self.image)["status"], "pending")
        self.assertEqual(json.loads(self.result_path(old_request).read_text())["status"], "applied")

    async def test_image_changed_after_request_does_not_upload(self):
        self.queue()
        self.image.write_bytes(PNG + b"other-brand")
        self.assertEqual((await self.process())["status"], "failed")
        self.client.upload_file.assert_not_awaited()

    async def test_upload_uses_exact_hashed_snapshot_if_file_changes(self):
        self.queue()

        async def replace_file(image):
            self.image.write_bytes(PNG + b"other-brand")
            self.assertEqual(image.read(), PNG)
            return "uploaded"

        self.client.upload_file.side_effect = replace_file
        self.assertEqual((await self.process())["status"], "applied")

    async def test_expired_request_never_uploads(self):
        with patch.object(pictures.time, "time", return_value=time.time() - 601):
            self.queue()
        self.assertEqual((await self.process())["status"], "failed")
        self.client.upload_file.assert_not_awaited()

    async def test_disabled_does_not_request_or_upload(self):
        with patch.dict(os.environ, {"TG_PROFILE_PICTURE_ENABLED": "0"}):
            self.assertEqual(self.queue()["status"], "disabled")
            self.assertEqual((await self.process())["status"], "disabled")
            self.assertFalse(self.data.exists())
        self.client.upload_file.assert_not_awaited()

    async def test_recent_claim_is_applying_but_old_or_corrupt_claim_is_unverified(self):
        self.queue()
        path = self.result_path()
        path.parent.mkdir()
        for content, expected in (
            (json.dumps({"status": "applying", "updated_at": time.time()}), "applying"),
            (json.dumps({"status": "applying", "updated_at": time.time() - 46}), "unverified"),
            ("{", "unverified"),
        ):
            path.write_text(content)
            self.assertEqual((await self.process())["status"], expected)
        self.client.upload_file.assert_not_awaited()

    async def test_two_workers_claim_once(self):
        self.queue()
        started = asyncio.Event()
        proceed = asyncio.Event()

        async def blocked_upload(image):
            started.set()
            await proceed.wait()
            return "uploaded"

        self.client.upload_file.side_effect = blocked_upload
        first = asyncio.create_task(self.process())
        await started.wait()
        self.assertEqual((await self.process())["status"], "applying")
        proceed.set()
        self.assertEqual((await first)["status"], "applied")
        self.client.apply.assert_awaited_once()

    async def test_cancellation_leaves_unverified_claim(self):
        self.queue()
        started = asyncio.Event()

        async def blocked_apply(request):
            started.set()
            await asyncio.Event().wait()

        self.client.apply.side_effect = blocked_apply
        task = asyncio.create_task(self.process())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self.process())["status"], "unverified")
        self.client.apply.assert_awaited_once()

    async def test_result_persistence_failure_keeps_claim_and_never_retries(self):
        self.queue()
        with patch.object(pictures, "_atomic_json", side_effect=OSError("disk full")):
            self.assertEqual((await self.process())["status"], "unverified")
        self.assertEqual((await self.process())["status"], "applying")
        self.client.apply.assert_awaited_once()

    async def test_poll_rejects_legacy_override_and_stops_on_cancel(self):
        self.queue()
        canonical = self.root / "canonical.png"
        canonical.write_bytes(PNG + b"canonical")
        with patch.object(pictures, "process_profile_picture", new_callable=AsyncMock) as process:
            with patch.object(pictures.asyncio, "sleep", side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await pictures.poll_profile_picture_requests(self.client, self.me, self.data, self.image, canonical_image_path=canonical)
            process.assert_not_awaited()

    async def test_corrupt_previous_result_blocks_replacement(self):
        self.queue()
        path = self.result_path()
        path.parent.mkdir()
        path.write_text("{")
        self.assertEqual(self.queue(OTHER_PHONE)["status"], "unverified")
        self.assertEqual((await self.process())["status"], "unverified")
        self.client.upload_file.assert_not_awaited()

    async def test_corrupt_record_shapes_do_not_escape_public_api(self):
        self.queue()
        path = self.result_path()
        path.parent.mkdir()
        for record, expected in (
            ({"status": []}, "unverified"),
            ({"status": {}}, "unverified"),
            ({"status": "failed", "reason": []}, "failed"),
            ({"status": "unverified", "reason": {}}, "unverified"),
            ({"status": "applying", "updated_at": []}, "unverified"),
        ):
            path.write_text(json.dumps(record))
            status = pictures.public_profile_picture_status(self.data, PHONE, self.image)
            self.assertEqual(status["status"], expected)
            self.assertIsInstance(status["message"], str)
            self.assertEqual((await self.process())["status"], expected)
            pictures._prune_results(self.data, "")
        self.client.upload_file.assert_not_awaited()

    async def test_applied_account_remains_deduplicated_after_linking_other_account(self):
        self.queue()
        await self.process()
        self.queue(OTHER_PHONE)
        self.assertEqual(self.queue()["status"], "applied")
        self.assertEqual(pictures.public_profile_picture_status(self.data, PHONE, self.image)["status"], "applied")
        self.client.apply.assert_awaited_once()

    def test_concurrent_enqueue_cannot_replace_request(self):
        self.queue()
        request = self.read_request()
        with pictures._queue_lock(self.data):
            self.assertEqual(self.queue(OTHER_PHONE)["status"], "failed")
            self.assertEqual(self.read_request()["request_id"], request["request_id"])
        self.assertEqual(self.queue(OTHER_PHONE)["status"], "pending")

    def test_result_history_is_bounded_without_pruning_uncertain_claims(self):
        self.queue()
        directory = self.data / pictures.RESULTS_DIR
        directory.mkdir()
        for index in range(pictures.RESULT_LIMIT):
            path = directory / (f"{index:032x}.json")
            path.write_text(json.dumps({"status": "unverified", "account_hash": "other"}))
        current = self.read_request()
        self.assertEqual(self.queue(OTHER_PHONE)["status"], "failed")
        self.assertEqual(self.read_request()["request_id"], current["request_id"])
        self.assertEqual(len(list(directory.glob("*.json"))), pictures.RESULT_LIMIT)

    async def test_pruning_preserves_current_applied_claim_when_request_write_fails(self):
        self.queue()
        await self.process()
        active_path = self.result_path()
        os.utime(active_path, (1, 1))
        for index in range(pictures.RESULT_LIMIT):
            path = active_path.parent / (f"{index:032x}.json")
            path.write_text(json.dumps({"status": "applied", "account_hash": "other"}))
        with patch.object(pictures, "_atomic_json", side_effect=OSError("disk full")):
            self.assertEqual(self.queue(OTHER_PHONE)["status"], "failed")
        self.assertTrue(active_path.exists())
        self.assertEqual((await self.process())["status"], "applied")
        self.client.apply.assert_awaited_once()

    def test_invalid_account_or_missing_image_never_queues(self):
        self.assertEqual(self.queue("@personal-user")["status"], "failed")
        self.image.unlink()
        self.assertEqual(self.queue()["status"], "failed")
        self.assertFalse(self.data.exists())


if __name__ == "__main__":
    unittest.main()
