import asyncio
from pathlib import Path
import tempfile
import unittest

from telethon.tl import functions, types

from interaction_state import PersistentInteractionState
from language_detection import detect_language_evidence
from telegram_delivery_journal import TelegramVideoDeliveryUncertain
from telegram_dispatcher import (
    TelegramInteractionDispatcher,
    build_telegram_media_request,
    build_telegram_text_request,
    deliver_telegram_response_components,
    telegram_delivery_random_id,
    telegram_queue_wait_ms,
)


class TelegramInteractionDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_delivers_audio_before_text(self):
        order = []

        async def send_text():
            order.append("text")

        async def send_audio():
            order.append("audio")

        await deliver_telegram_response_components(
            "call",
            send_text=send_text,
            send_audio=send_audio,
        )

        self.assertEqual(["audio", "text"], order)

    async def test_steps_keep_text_before_audio(self):
        for response_key in ("step1", "step2"):
            with self.subTest(response_key=response_key):
                order = []

                async def send_text():
                    order.append("text")

                async def send_audio():
                    order.append("audio")

                await deliver_telegram_response_components(
                    response_key,
                    send_text=send_text,
                    send_audio=send_audio,
                )

                self.assertEqual(["text", "audio"], order)

    async def test_steps_and_call_deliver_video_then_text_then_optional_audio(self):
        for response_key in ("step1", "step2", "call"):
            for with_audio in (True, False):
                with self.subTest(response_key=response_key, with_audio=with_audio):
                    order = []
                    await deliver_telegram_response_components(
                        response_key,
                        send_text=lambda: self._append_async(order, "text"),
                        send_audio=(
                            (lambda: self._append_async(order, "audio"))
                            if with_audio else None
                        ),
                        send_video=lambda: self._append_async(order, "video"),
                    )
                    self.assertEqual(
                        ["video", "text", "audio"] if with_audio else ["video", "text"],
                        order,
                    )

    async def test_video_failure_still_attempts_text_and_audio_and_preserves_first_error(self):
        for response_key in ("step1", "step2", "call"):
            with self.subTest(response_key=response_key):
                order = []
                video_error = RuntimeError("video failed")

                async def send_video():
                    order.append("video")
                    raise video_error

                async def send_audio():
                    order.append("audio")
                    raise RuntimeError("audio also failed")

                with self.assertRaises(RuntimeError) as caught:
                    await deliver_telegram_response_components(
                        response_key,
                        send_text=lambda: self._append_async(order, "text"),
                        send_audio=send_audio,
                        send_video=send_video,
                    )
                self.assertIs(video_error, caught.exception)
                self.assertEqual(["video", "text", "audio"], order)

    async def test_uncertain_video_holds_followups_for_steps_and_call(self):
        for response_key in ("step1", "step2", "call"):
            with self.subTest(response_key=response_key):
                order = []

                async def send_video():
                    order.append("video")
                    raise TelegramVideoDeliveryUncertain("uncertain")

                with self.assertRaises(TelegramVideoDeliveryUncertain):
                    await deliver_telegram_response_components(
                        response_key,
                        send_text=lambda: self._append_async(order, "text"),
                        send_audio=lambda: self._append_async(order, "audio"),
                        send_video=send_video,
                    )
                self.assertEqual(["video"], order)

    @staticmethod
    async def _append_async(target: list, value: str) -> None:
        target.append(value)

    async def test_call_partial_retry_reuses_component_random_ids(self):
        fingerprint = "f" * 64
        peer = types.InputPeerSelf()
        attempts = []
        text_attempts = 0

        async def send_audio():
            request = build_telegram_media_request(
                peer,
                types.InputMediaEmpty(),
                fingerprint,
            )
            attempts.append(("audio", request.random_id))

        async def send_text():
            nonlocal text_attempts
            request = build_telegram_text_request(peer, "respuesta", fingerprint)
            attempts.append(("text", request.random_id))
            text_attempts += 1
            if text_attempts == 1:
                raise RuntimeError("ambiguous text delivery")

        with self.assertRaises(RuntimeError):
            await deliver_telegram_response_components(
                "call",
                send_text=send_text,
                send_audio=send_audio,
            )
        await deliver_telegram_response_components(
            "call",
            send_text=send_text,
            send_audio=send_audio,
        )

        self.assertEqual(
            ["audio", "text", "audio", "text"],
            [component for component, _random_id in attempts],
        )
        self.assertEqual(attempts[0][1], attempts[2][1])
        self.assertEqual(attempts[1][1], attempts[3][1])
        self.assertNotEqual(attempts[0][1], attempts[1][1])

    async def test_call_audio_failure_still_attempts_text_and_propagates(self):
        order = []

        async def send_audio():
            order.append("audio")
            raise RuntimeError("audio failed")

        async def send_text():
            order.append("text")

        with self.assertRaisesRegex(RuntimeError, "audio failed"):
            await deliver_telegram_response_components(
                "call",
                send_text=send_text,
                send_audio=send_audio,
            )

        self.assertEqual(["audio", "text"], order)

    async def test_same_chat_delivers_step1_completely_before_step2(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PersistentInteractionState(Path(directory) / "state.json")
            order = []
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            queue_times = []

            async def send_response(_chat_id, response_key, _language, _fingerprint):
                queue_times.append(telegram_queue_wait_ms.get())
                order.append(f"{response_key}:start")
                if response_key == "step1":
                    first_started.set()
                    await release_first.wait()
                order.append(f"{response_key}:end")

            dispatcher = TelegramInteractionDispatcher(state, send_response)
            first = asyncio.create_task(dispatcher.dispatch(
                chat_id=1,
                event_id="message:1",
                kind="content",
            ))
            await first_started.wait()
            second = asyncio.create_task(dispatcher.dispatch(
                chat_id=1,
                event_id="message:2",
                kind="content",
            ))
            await asyncio.sleep(0)

            self.assertEqual(["step1:start"], order)
            release_first.set()
            await asyncio.gather(first, second)
            self.assertEqual(
                ["step1:start", "step1:end", "step2:start", "step2:end"],
                order,
            )
            self.assertEqual(2, len(queue_times))
            self.assertTrue(all(duration >= 0 for duration in queue_times))
            self.assertEqual(0.0, telegram_queue_wait_ms.get())

    async def test_provisional_spanish_is_replaced_by_detected_customer_text(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PersistentInteractionState(Path(directory) / "state.json")
            deliveries = []

            async def send_response(_peer, key, language, _fingerprint):
                deliveries.append((key, language))

            dispatcher = TelegramInteractionDispatcher(state, send_response)
            await dispatcher.dispatch(
                chat_id=1,
                event_id="message:image",
                kind="content",
                provisional_language="es",
            )
            await dispatcher.dispatch(
                chat_id=1,
                event_id="message:text",
                kind="content",
                detected_language="en",
                language_evidence=detect_language_evidence(
                    "Are you available now?"
                ),
                provisional_language="es",
            )

            self.assertEqual([("step1", "es"), ("step2", "en")], deliveries)

    async def test_provisional_spanish_is_replaced_by_strong_natural_french(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PersistentInteractionState(Path(directory) / "state.json")
            deliveries = []

            async def send_response(_peer, key, language, _fingerprint):
                deliveries.append((key, language))

            dispatcher = TelegramInteractionDispatcher(state, send_response)
            await dispatcher.dispatch(
                chat_id=1,
                event_id="message:image",
                kind="content",
                provisional_language="es",
            )
            evidence = detect_language_evidence(
                "Salut, je cherche une fille disponible"
            )
            decision = await dispatcher.dispatch(
                chat_id=1,
                event_id="message:french",
                kind="content",
                detected_language="fr",
                language_evidence=evidence,
                provisional_language="es",
            )

            self.assertTrue(evidence["strong"])
            self.assertEqual("fr", decision.language)
            self.assertEqual([("step1", "es"), ("step2", "fr")], deliveries)
            contact = next(iter(state._contacts.values()))
            self.assertEqual("detected", contact["language_source"])
            self.assertIsNone(contact["language_candidate"])

    async def test_strong_spanish_evidence_overrides_confirmed_french(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PersistentInteractionState(Path(directory) / "state.json")
            state.register(
                contact_id=1,
                event_id="message:french",
                kind="content",
                language_evidence=detect_language_evidence("bonjour"),
            )
            deliveries = []

            async def send_response(_peer, key, language, _fingerprint):
                deliveries.append((key, language))

            dispatcher = TelegramInteractionDispatcher(state, send_response)
            evidence = detect_language_evidence(
                "que chicas están disponibles por Rubí"
            )
            decision = await dispatcher.dispatch(
                chat_id=1,
                event_id="message:spanish",
                kind="content",
                detected_language="es",
                language_evidence=evidence,
                provisional_language="es",
            )

            self.assertEqual("es", decision.language)
            self.assertEqual([("step2", "es")], deliveries)
            contact = next(iter(state._contacts.values()))
            self.assertEqual("detected", contact["language_source"])
            self.assertIsNone(contact["language_candidate"])

    async def test_duplicate_does_not_send_a_second_response(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PersistentInteractionState(Path(directory) / "state.json")
            deliveries = []

            async def send_response(*args):
                deliveries.append(args)

            dispatcher = TelegramInteractionDispatcher(state, send_response)
            await dispatcher.dispatch(chat_id=1, event_id="call:1", kind="call")
            duplicate = await dispatcher.dispatch(chat_id=1, event_id="call:1", kind="call")

            self.assertTrue(duplicate.duplicate)
            self.assertEqual(1, len(deliveries))

    async def test_every_distinct_call_uses_call_even_after_prior_interactions(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PersistentInteractionState(Path(directory) / "state.json")
            deliveries = []

            async def send_response(_peer, response_key, _language, _fingerprint):
                deliveries.append(response_key)

            dispatcher = TelegramInteractionDispatcher(state, send_response)
            await dispatcher.dispatch(
                chat_id=1,
                event_id="message:1",
                kind="content",
            )
            first_call = await dispatcher.dispatch(
                chat_id=1,
                event_id="call:700",
                kind="call",
            )
            second_call = await dispatcher.dispatch(
                chat_id=1,
                event_id="call:701",
                kind="call",
            )
            duplicate_fallback = await dispatcher.dispatch(
                chat_id=1,
                event_id="call:701",
                kind="call",
            )
            following_content = await dispatcher.dispatch(
                chat_id=1,
                event_id="message:2",
                kind="content",
            )

            self.assertEqual("call", first_call.response_key)
            self.assertEqual("call", second_call.response_key)
            self.assertTrue(duplicate_fallback.duplicate)
            self.assertEqual("step2", following_content.response_key)
            self.assertEqual(["step1", "call", "call", "step2"], deliveries)

    async def test_failed_delivery_does_not_consume_call_and_fallback_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = PersistentInteractionState(state_path)
            attempts = []

            async def send_response(peer, response_key, _language, fingerprint):
                attempts.append((peer, response_key, fingerprint))
                if len(attempts) == 1:
                    raise RuntimeError("delivery failed")

            dispatcher = TelegramInteractionDispatcher(state, send_response)
            with self.assertRaises(RuntimeError):
                await dispatcher.dispatch(
                    chat_id=1,
                    event_id="call:77",
                    kind="call",
                    reply_peer="input-peer",
                )

            pending_after_reload = PersistentInteractionState(state_path).preview(
                contact_id=1,
                event_id="call:77",
                kind="call",
            )
            self.assertFalse(pending_after_reload.duplicate)
            self.assertEqual("call", pending_after_reload.response_key)

            recovered = await dispatcher.dispatch(
                chat_id=1,
                event_id="call:77",
                kind="call",
                reply_peer="input-peer",
            )
            duplicate = await dispatcher.dispatch(
                chat_id=1,
                event_id="call:77",
                kind="call",
                reply_peer="input-peer",
            )

            self.assertEqual("call", recovered.response_key)
            self.assertTrue(duplicate.duplicate)
            duplicate_after_reload = PersistentInteractionState(state_path).preview(
                contact_id=1,
                event_id="call:77",
                kind="call",
            )
            self.assertTrue(duplicate_after_reload.duplicate)
            self.assertEqual(2, len(attempts))
            self.assertEqual(attempts[0], attempts[1])
            fingerprint = attempts[0][2]
            self.assertEqual(64, len(fingerprint))
            self.assertNotIn("call:77", fingerprint)
            self.assertNotEqual("1", fingerprint)
            self.assertEqual(
                telegram_delivery_random_id(fingerprint, "text"),
                telegram_delivery_random_id(attempts[1][2], "text"),
            )

            primary_request = build_telegram_text_request(
                types.InputPeerSelf(),
                "respuesta",
                attempts[0][2],
            )
            retry_request = build_telegram_text_request(
                types.InputPeerSelf(),
                "respuesta",
                attempts[0][2],
            )
            fallback_request = build_telegram_text_request(
                types.InputPeerSelf(),
                "respuesta",
                attempts[1][2],
            )
            self.assertIsInstance(primary_request, functions.messages.SendMessageRequest)
            self.assertEqual(
                primary_request.random_id,
                retry_request.random_id,
            )
            self.assertEqual(
                primary_request.random_id,
                fallback_request.random_id,
            )

    async def test_delivery_ids_are_stable_per_component_and_distinct_between_components(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PersistentInteractionState(Path(directory) / "state.json")
            fingerprints = []

            async def send_response(_peer, _response_key, _language, fingerprint):
                fingerprints.append(fingerprint)

            dispatcher = TelegramInteractionDispatcher(state, send_response)
            await dispatcher.dispatch(
                chat_id=451,
                event_id="message:9001",
                kind="content",
            )

            fingerprint = fingerprints[0]
            text_id = telegram_delivery_random_id(fingerprint, "text")
            audio_id = telegram_delivery_random_id(fingerprint, "audio")
            video_id = telegram_delivery_random_id(fingerprint, "video")
            self.assertEqual(text_id, telegram_delivery_random_id(fingerprint, "text"))
            self.assertEqual(video_id, telegram_delivery_random_id(fingerprint, "video"))
            self.assertNotEqual(text_id, audio_id)
            self.assertNotEqual(text_id, video_id)
            self.assertNotEqual(audio_id, video_id)
            self.assertGreaterEqual(text_id, -(2**63))
            self.assertLessEqual(text_id, 2**63 - 1)
            self.assertGreaterEqual(audio_id, -(2**63))
            self.assertLessEqual(audio_id, 2**63 - 1)
            self.assertGreaterEqual(video_id, -(2**63))
            self.assertLessEqual(video_id, 2**63 - 1)

            text_request = build_telegram_text_request(
                types.InputPeerSelf(),
                "respuesta",
                fingerprint,
            )
            audio_request = build_telegram_media_request(
                types.InputPeerSelf(),
                types.InputMediaEmpty(),
                fingerprint,
            )
            video_request = build_telegram_media_request(
                types.InputPeerSelf(),
                types.InputMediaEmpty(),
                fingerprint,
                component="video",
            )
            self.assertIsInstance(text_request, functions.messages.SendMessageRequest)
            self.assertIsInstance(audio_request, functions.messages.SendMediaRequest)
            self.assertIsInstance(video_request, functions.messages.SendMediaRequest)
            self.assertEqual(text_id, text_request.random_id)
            self.assertEqual(audio_id, audio_request.random_id)
            self.assertEqual(video_id, video_request.random_id)


if __name__ == "__main__":
    unittest.main()
