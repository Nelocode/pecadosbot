"""Bounded, best-effort typing signals; never delay work to simulate typing."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import math

from telethon.tl import functions, types


@asynccontextmanager
async def telegram_typing_activity(
    client,
    peer,
    *,
    interval_seconds: float = 4,
    request_timeout_seconds: float = 1,
    max_duration_seconds: float = 300,
):
    """Run typing concurrently and cancel it on every exit path.

    The body does not wait for a presence RPC or an artificial typing delay.
    Presence has its own bounded lifetime and failures never replace the body's
    result, including cancellation. Cleanup attempts a cancel action, but cannot
    promise that a disconnected remote client will receive that action.
    """

    if (
        not all(math.isfinite(value) and value > 0 for value in (
            interval_seconds, request_timeout_seconds, max_duration_seconds,
        ))
    ):
        yield
        return

    interval = max(0.001, interval_seconds)
    request_timeout = min(request_timeout_seconds, max_duration_seconds)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_duration_seconds
    started = False

    async def publish():
        nonlocal started
        while loop.time() < deadline:
            started = True
            try:
                await asyncio.wait_for(
                    client(functions.messages.SetTypingRequest(
                        peer=peer, action=types.SendMessageTypingAction(),
                    )),
                    timeout=min(request_timeout, max(0.001, deadline - loop.time())),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not keep retrying a refused or unavailable activity signal.
                return
            remaining = deadline - loop.time()
            if remaining > 0:
                await asyncio.sleep(min(interval, remaining))

    task = asyncio.create_task(publish(), name="telegram-typing-activity")
    try:
        yield
    finally:
        owner = asyncio.current_task()
        cancellations_before_cleanup = owner.cancelling()
        cancelled_during_cleanup = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            cancelled_during_cleanup = owner.cancelling() > cancellations_before_cleanup
        except Exception:
            pass
        if started:
            try:
                await asyncio.wait_for(
                    client(functions.messages.SetTypingRequest(
                        peer=peer, action=types.SendMessageCancelAction(),
                    )),
                    timeout=min(request_timeout, 0.5),
                )
            except asyncio.CancelledError:
                cancelled_during_cleanup |= owner.cancelling() > cancellations_before_cleanup
            except Exception:
                pass
        if cancelled_during_cleanup:
            raise asyncio.CancelledError
