import asyncio
import unittest

from telethon.tl import types

from telegram_activity import telegram_typing_activity


class TelegramActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_typing_is_concurrent_with_work_and_cancelled_after_success(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def client(request):
            calls.append(type(request.action))
            if isinstance(request.action, types.SendMessageTypingAction):
                started.set()
                await release.wait()

        async with telegram_typing_activity(client, object()):
            await started.wait()
            # The body is running while the presence request is still pending.
            self.assertFalse(release.is_set())
        self.assertEqual(calls, [types.SendMessageTypingAction, types.SendMessageCancelAction])
        self.assertFalse(any(t.get_name() == "telegram-typing-activity" for t in asyncio.all_tasks()))

    async def test_body_error_is_preserved_when_presence_and_cleanup_fail(self):
        attempted = asyncio.Event()
        calls = []

        async def client(request):
            calls.append(type(request.action))
            attempted.set()
            raise RuntimeError("synthetic activity failure")

        with self.assertRaisesRegex(ValueError, "body failure"):
            async with telegram_typing_activity(client, object()):
                await attempted.wait()
                raise ValueError("body failure")
        self.assertEqual(calls[-1], types.SendMessageCancelAction)

    async def test_cancelled_work_cleans_up_and_preserves_cancellation(self):
        started = asyncio.Event()
        calls = []

        async def client(request):
            calls.append(type(request.action))
            if isinstance(request.action, types.SendMessageTypingAction):
                started.set()

        async def work():
            async with telegram_typing_activity(client, object()):
                await asyncio.Future()

        task = asyncio.create_task(work())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls[-1], types.SendMessageCancelAction)
        self.assertFalse(any(t.get_name() == "telegram-typing-activity" for t in asyncio.all_tasks()))

    async def test_renewal_is_bounded_even_when_work_keeps_running(self):
        calls = []

        async def client(request):
            calls.append(type(request.action))

        async with telegram_typing_activity(
            client, object(), interval_seconds=0.005,
            request_timeout_seconds=0.01, max_duration_seconds=0.025,
        ):
            await asyncio.sleep(0.06)
            count_after_expiry = len(calls)
            self.assertGreaterEqual(count_after_expiry, 2)
            self.assertLessEqual(count_after_expiry, 6)
            await asyncio.sleep(0.02)
            self.assertEqual(len(calls), count_after_expiry)
        self.assertEqual(calls[-1], types.SendMessageCancelAction)

    async def test_presence_timeout_does_not_cancel_body_or_retry_forever(self):
        calls = []

        async def client(request):
            calls.append(type(request.action))
            await asyncio.Future()

        async with telegram_typing_activity(
            client, object(), interval_seconds=0.005,
            request_timeout_seconds=0.005, max_duration_seconds=0.03,
        ):
            await asyncio.sleep(0.025)
            self.assertEqual(calls, [types.SendMessageTypingAction])
        self.assertEqual(calls, [types.SendMessageTypingAction, types.SendMessageCancelAction])

    async def test_disabled_activity_does_not_touch_client(self):
        async def client(request):
            self.fail("disabled activity called client")

        async with telegram_typing_activity(client, object(), max_duration_seconds=0):
            pass

    async def test_external_cancellation_during_cancel_rpc_is_not_swallowed(self):
        started = asyncio.Event()
        cleaning = asyncio.Event()

        async def client(request):
            if isinstance(request.action, types.SendMessageTypingAction):
                started.set()
            else:
                cleaning.set()
                await asyncio.Future()

        async def work():
            async with telegram_typing_activity(client, object()):
                await started.wait()

        task = asyncio.create_task(work())
        await cleaning.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(any(t.get_name() == "telegram-typing-activity" for t in asyncio.all_tasks()))

    async def test_external_cancellation_while_stopping_publisher_is_not_swallowed(self):
        started = asyncio.Event()
        stopping = asyncio.Event()
        calls = []

        async def client(request):
            calls.append(type(request.action))
            if isinstance(request.action, types.SendMessageTypingAction):
                started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    stopping.set()
                    await asyncio.Future()

        async def work():
            async with telegram_typing_activity(client, object()):
                await started.wait()

        task = asyncio.create_task(work())
        await stopping.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls[-1], types.SendMessageCancelAction)
