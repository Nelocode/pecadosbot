"""Execute actual worker functions without importing its client/session setup."""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from interaction_state import PersistentInteractionState
from telegram_auth import telegram_user_matches_phone
from telegram_dispatcher import TelegramInteractionDispatcher


PHONE = "+570000000000"


def worker_functions():
    source = Path(__file__).resolve().parents[1] / "bot.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {
        "main", "handle_message", "handle_phone_call", "handle_missed_call_service",
        "process_interaction", "reject_incoming_call", "send_response",
        "poll_recent_missed_calls", "_retry_telegram_operation", "_wait_for_telegram_identity",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    module = ast.Module(body=nodes, type_ignores=[])
    namespace = {
        # Avoid changing asyncio.sleep globally when exercising reconnects.
        "asyncio": SimpleNamespace(**vars(asyncio)), "logging": Mock(wraps=logging), "uuid": uuid,
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "telegram_user_matches_phone": telegram_user_matches_phone,
        "telegram_identity_verified": False, "self_user_id": None, "PHONE": PHONE,
        "telegram_identity_attempt": None,
        "TELEGRAM_IDENTITY_TIMEOUT_SECONDS": 0.05,
        "TELEGRAM_DISCONNECT_TIMEOUT_SECONDS": 0.05,
        "DATA_DIR": "unused-test-data", "PROFILE_PICTURE_PATH": "unused-test-logo",
        "PROFILE_PICTURE_CANONICAL_PATH": "unused-canonical-logo",
        "AUTHORIZED_MARKER_FILE": Mock(), "HEALTH_FILE": Mock(),
        "write_authorized_marker": Mock(), "write_identity": Mock(), "write_health": Mock(),
        "update_interaction_health": Mock(), "TELEGRAM_SEND_RETRIES": 1,
    }
    namespace["asyncio"].sleep = AsyncMock()
    exec(compile(module, str(source), "exec"), namespace)
    return namespace


class TelegramWorkerIdentityTests(unittest.IsolatedAsyncioTestCase):
    class UnusableEvent:
        def __getattr__(self, name):
            raise AssertionError("Unverified event was accessed: " + name)

    async def assert_inert(self, worker):
        # Poison objects fail if an unverified event even starts resolving peers.
        event = self.UnusableEvent()
        for name in ("handle_message", "handle_phone_call", "handle_missed_call_service"):
            await worker[name](event)
        await worker["process_interaction"](chat_id=1, event_id="test", kind="content")
        self.assertEqual("identity_unverified", await worker["reject_incoming_call"](event))
        operation = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "not verified"):
            await worker["_retry_telegram_operation"]("test", operation)
        operation.assert_not_awaited()
        with self.assertRaisesRegex(RuntimeError, "not verified"):
            await worker["send_response"](1, "step1", "es", "test")

    def client(self):
        return SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(), catch_up=AsyncMock(), run_until_disconnected=AsyncMock(),
        )

    async def pending_events(self, worker):
        tasks = [
            asyncio.create_task(worker[name](self.UnusableEvent()))
            for name in ("handle_message", "handle_phone_call", "handle_missed_call_service")
        ]
        await asyncio.sleep(0)
        self.assertTrue(all(not task.done() for task in tasks))
        self.assertFalse(worker["telegram_identity_verified"])
        worker["write_identity"].assert_not_called()
        return tasks

    def install_background_workers(self, worker):
        async def wait_until_cancelled(*args, **kwargs):
            await asyncio.Event().wait()

        worker["heartbeat"] = wait_until_cancelled
        worker["poll_recent_missed_calls"] = wait_until_cancelled
        worker["poll_profile_picture_requests"] = wait_until_cancelled

    def assert_revoked(self, worker):
        self.assertFalse(worker["telegram_identity_verified"])
        self.assertIsNone(worker["telegram_identity_attempt"])
        self.assertIsNone(worker["self_user_id"])

    async def run_main(self, worker):
        # Worker recovery catches Exception; fixture assertions must neither
        # silently pass as network errors nor retry indefinitely on regression.
        async with asyncio.timeout(2) as deadline:
            await worker["main"]()
        self.assertFalse(deadline.expired(), "Worker fixture exceeded its deadline")
        for level in ("warning", "error"):
            for call in getattr(worker["logging"], level).call_args_list:
                self.assertNotIn("AssertionError", call.args)

    async def test_handlers_and_delivery_are_inert_until_identity_is_verified(self):
        worker = worker_functions()
        await self.assert_inert(worker)
        await worker["poll_recent_missed_calls"]()

    async def test_wrong_missing_and_bot_identity_never_activate_worker(self):
        for user in (
            SimpleNamespace(id=123, phone="571111111111", bot=False),
            SimpleNamespace(id=123, phone=None, bot=False),
            SimpleNamespace(id=123, phone="570000000000", bot=True),
            None,
        ):
            with self.subTest(user=user):
                worker = worker_functions()
                client = self.client()
                worker["client"] = client
                pending = []
                attempts = []

                async def connect():
                    attempts.append(worker["telegram_identity_attempt"])
                    pending.extend(await self.pending_events(worker))

                async def get_me():
                    pending.extend(await self.pending_events(worker))
                    return user

                async def disconnect():
                    self.assert_revoked(worker)
                    await asyncio.gather(*pending)
                    await self.assert_inert(worker)

                client.connect.side_effect = connect
                client.get_me.side_effect = get_me
                client.disconnect.side_effect = disconnect
                await self.run_main(worker)
                self.assertFalse(attempts[0].result())
                client.catch_up.assert_not_awaited()
                client.run_until_disconnected.assert_not_awaited()
                worker["write_authorized_marker"].assert_not_called()
                worker["write_identity"].assert_not_called()
                worker["AUTHORIZED_MARKER_FILE"].unlink.assert_not_called()
                worker["write_health"].assert_called_once_with(False)
                client.disconnect.assert_awaited_once()
                self.assert_revoked(worker)

    async def test_message_during_get_me_waits_then_is_delivered_once_despite_catchup_duplicate(self):
        worker = worker_functions()
        client = self.client()
        worker["client"] = client
        me = SimpleNamespace(id=123, phone="570000000000", bot=False)
        pending = []
        accessed = []
        sent = AsyncMock()
        self.install_background_workers(worker)

        class MessageEvent:
            def __getattribute__(event, name):
                if not worker["telegram_identity_verified"]:
                    raise AssertionError("Message accessed before identity validation: " + name)
                accessed.append(name)
                return object.__getattribute__(event, name)

            async def get_input_chat(event):
                return 1

            message = object()
            chat_id = 1
            is_private = True

        event = MessageEvent()
        worker["new_message_interaction"] = Mock(return_value=SimpleNamespace(
            contact_id=1, event_id="fixture-message", kind="content", text="", reply_peer=1,
        ))

        async def get_me():
            pending.append(asyncio.create_task(worker["handle_message"](event)))
            await asyncio.sleep(0)
            self.assertFalse(pending[0].done())
            self.assertEqual([], accessed)
            sent.assert_not_awaited()
            worker["write_identity"].assert_not_called()
            return me

        async def catch_up():
            self.assertTrue(worker["telegram_identity_verified"])
            self.assertEqual(123, worker["self_user_id"])
            # Replaying the same Telegram event must keep the real dispatcher's
            # persistent deduplication, even alongside the retained callback.
            pending.append(asyncio.create_task(worker["handle_message"](event)))

        async def run_until_disconnected():
            await asyncio.gather(*pending)
            sent.assert_awaited_once()
            raise asyncio.CancelledError

        async def disconnect():
            self.assert_revoked(worker)
            await self.assert_inert(worker)

        client.get_me.side_effect = get_me
        client.catch_up.side_effect = catch_up
        client.disconnect.side_effect = disconnect
        client.run_until_disconnected.side_effect = run_until_disconnected
        with tempfile.TemporaryDirectory() as directory:
            worker["telegram_dispatcher"] = TelegramInteractionDispatcher(
                PersistentInteractionState(Path(directory) / "state.json"), sent,
            )
            await self.run_main(worker)
        self.assertTrue(accessed)
        sent.assert_awaited_once()
        client.catch_up.assert_awaited_once()
        worker["write_authorized_marker"].assert_called_once()
        worker["write_identity"].assert_called_once_with(me)
        self.assert_revoked(worker)

    async def test_unauthorized_connection_discards_events_without_identity_lookup(self):
        worker = worker_functions()
        client = self.client()
        worker["client"] = client
        pending = []

        async def connect():
            pending.extend(await self.pending_events(worker))

        async def disconnect():
            self.assert_revoked(worker)
            await asyncio.gather(*pending)

        client.connect.side_effect = connect
        client.disconnect.side_effect = disconnect
        client.is_user_authorized.return_value = False
        await self.run_main(worker)
        client.get_me.assert_not_awaited()
        client.catch_up.assert_not_awaited()
        worker["write_identity"].assert_not_called()
        worker["AUTHORIZED_MARKER_FILE"].unlink.assert_called_once_with(missing_ok=True)
        self.assert_revoked(worker)

    async def test_error_and_timeout_drop_attempt_before_retry_without_accessing_events(self):
        for phase in ("connect", "is_user_authorized", "get_me"):
            for failure in ("error", "timeout"):
                with self.subTest(phase=phase, failure=failure):
                    worker = worker_functions()
                    client = self.client()
                    worker["client"] = client
                    pending = []
                    attempts = []

                    async def fail():
                        if failure == "timeout":
                            await asyncio.Event().wait()
                        raise RuntimeError("fixture failure")

                    async def connect():
                        if attempts:
                            self.assertFalse(attempts[0].result())
                            self.assertTrue(all(task.done() for task in pending))
                            self.assertIsNot(attempts[0], worker["telegram_identity_attempt"])
                            raise asyncio.CancelledError
                        attempts.append(worker["telegram_identity_attempt"])
                        pending.extend(await self.pending_events(worker))
                        if phase == "connect":
                            await fail()

                    async def disconnect():
                        self.assert_revoked(worker)
                        await asyncio.gather(*pending)

                    client.connect.side_effect = connect
                    if phase != "connect":
                        getattr(client, phase).side_effect = fail
                    client.disconnect.side_effect = disconnect
                    await self.run_main(worker)
                    self.assertEqual(2, client.connect.await_count)
                    client.catch_up.assert_not_awaited()
                    worker["write_identity"].assert_not_called()
                    worker["write_authorized_marker"].assert_not_called()
                    self.assert_revoked(worker)

    async def test_cancel_during_get_me_discards_events_and_disconnects(self):
        worker = worker_functions()
        client = self.client()
        worker["client"] = client
        pending = []
        started = asyncio.Event()

        async def get_me():
            pending.extend(await self.pending_events(worker))
            started.set()
            await asyncio.Event().wait()

        async def disconnect():
            self.assert_revoked(worker)
            await asyncio.gather(*pending)

        client.get_me.side_effect = get_me
        client.disconnect.side_effect = disconnect
        task = asyncio.create_task(self.run_main(worker))
        await started.wait()
        task.cancel()
        await task
        client.connect.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        client.catch_up.assert_not_awaited()
        worker["write_identity"].assert_not_called()
        self.assert_revoked(worker)

    async def test_cancelled_event_does_not_cancel_other_waiters_or_identity_decision(self):
        worker = worker_functions()
        attempt = asyncio.get_running_loop().create_future()
        worker["telegram_identity_attempt"] = attempt
        cancelled = asyncio.create_task(worker["handle_message"](self.UnusableEvent()))
        survivor = asyncio.create_task(worker["_wait_for_telegram_identity"]())
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.assertFalse(attempt.done())
        self.assertFalse(survivor.done())
        worker["telegram_identity_verified"] = True
        attempt.set_result(True)
        self.assertTrue(await survivor)

    async def test_accepted_old_attempt_cannot_authorize_a_new_connection(self):
        worker = worker_functions()
        old_attempt = asyncio.get_running_loop().create_future()
        worker["telegram_identity_attempt"] = old_attempt
        pending = await self.pending_events(worker)
        old_attempt.set_result(True)
        worker["telegram_identity_attempt"] = asyncio.get_running_loop().create_future()
        worker["telegram_identity_verified"] = True
        await asyncio.gather(*pending)
        worker["telegram_identity_attempt"].set_result(True)
        worker["telegram_identity_verified"] = False
        await self.assert_inert(worker)

    async def test_disconnect_error_or_timeout_stops_retries(self):
        for failure in ("error", "timeout"):
            with self.subTest(failure=failure):
                worker = worker_functions()
                client = self.client()
                worker["client"] = client
                pending = []

                async def get_me():
                    pending.extend(await self.pending_events(worker))
                    raise RuntimeError("fixture identity lookup failure")

                async def disconnect():
                    self.assert_revoked(worker)
                    await asyncio.gather(*pending)
                    if failure == "timeout":
                        await asyncio.Event().wait()
                    raise RuntimeError("fixture disconnect failure")

                client.get_me.side_effect = get_me
                client.disconnect.side_effect = disconnect
                await self.run_main(worker)
                client.connect.assert_awaited_once()
                client.catch_up.assert_not_awaited()
                worker["asyncio"].sleep.assert_not_awaited()
                worker["write_identity"].assert_not_called()
                self.assert_revoked(worker)


if __name__ == "__main__":
    unittest.main()
