import asyncio
import base64
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import app as voice_app


class FakeTwilioWebSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, message):
        self.sent.append(json.loads(message))

    async def receive_text(self):
        await asyncio.Event().wait()


class EmptyDeepgramSocket:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class DeepgramConnectionContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_args):
        return False


class FakeTtsSocket:
    def __init__(self):
        self.items = [
            b"\x00\x00" * 480,  # 20 ms, mono linear16 at 24 kHz.
            json.dumps({"type": "SpeechMetadata"}),
        ]
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.items:
            raise StopAsyncIteration
        return self.items.pop(0)

    async def close(self):
        self.closed = True


class StreamingFakeTtsSocket:
    def __init__(self):
        self.sent = []
        self.items = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.items.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send(self, message):
        event = json.loads(message)
        self.sent.append(event)
        if event.get("type") == "Speak":
            await self.items.put(b"\x00\x00" * 480)
        elif event.get("type") == "Flush":
            await self.items.put(json.dumps({"type": "SpeechMetadata"}))

    async def close(self):
        await self.items.put(None)


class FakeAgentStreamResponse:
    def __init__(self):
        self.lines = [
            json.dumps({"type": "delta", "text": "I can help you with that."}),
            json.dumps({"type": "done", "text": "I can help you with that.", "cost": 0.01}),
        ]

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self.lines:
            await asyncio.sleep(0)
            yield line


class FakeAgentStreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class FakeAgentHttp:
    def __init__(self):
        self.stream_requests = []
        self.posts = []

    def stream(self, method, url, **kwargs):
        self.stream_requests.append({"method": method, "url": url, **kwargs})
        return FakeAgentStreamContext(FakeAgentStreamResponse())

    async def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})

        class Response:
            def raise_for_status(self):
                return None

        return Response()

    async def aclose(self):
        return None


class DeepgramTurnSocket:
    def __init__(self, events):
        self.events = events

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return json.dumps(self.events.pop(0))


class TwilioAudioWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_bridge_settings_use_twilio_only_internal_routes(self):
        with patch.dict(voice_app.os.environ, {
            "TWILIO_MEDIA_BRIDGE_SECRET": "twilio-bridge-test-secret",
            "DEEPGRAM_API_KEY": "deepgram-test-key",
            "CHUSKY_VOICE_TURN_URL": "https://chusky.example/internal/twilio/turn",
            "CHUSKY_VOICE_STATUS_URL": "https://chusky.example/internal/twilio/status",
        }, clear=True):
            settings = voice_app.Settings.from_env()
        self.assertEqual(settings.bridge_secret, "twilio-bridge-test-secret")
        self.assertEqual(settings.chusky_turn_url, "https://chusky.example/internal/twilio/turn")
        self.assertEqual(settings.chusky_status_url, "https://chusky.example/internal/twilio/status")

    async def asyncSetUp(self):
        self.websocket = FakeTwilioWebSocket()
        settings = SimpleNamespace(
            bridge_secret="test-bridge-secret",
            deepgram_api_key="test-deepgram-key",
            chusky_turn_url="http://localhost/internal/twilio/turn",
            chusky_status_url="http://localhost/internal/twilio/status",
            stt_model="flux-general-en",
            stt_eager_eot_threshold=0.45,
            stt_eot_threshold=0.65,
            stt_eot_timeout_ms=800,
            tts_model="flux-haley-en",
            greeting="",
        )
        self.call = voice_app.TwilioVoiceCall(
            "twc_test",
            1,
            "MZ_test",
            self.websocket,
            settings,
            voice_app.BridgeMetrics(),
        )
        self.addAsyncCleanup(self.call.http.aclose)
        self.addAsyncCleanup(self.call._close_persistent_tts)

    async def test_twilio_ingress_is_resampled_before_being_sent_to_flux(self):
        sent_audio = []
        sent_once = asyncio.Event()

        class CaptureSocket:
            async def send(self, frame):
                sent_audio.append(frame)
                sent_once.set()

        self.call.audio.put_nowait(b"\xff" * 160)
        sender = asyncio.create_task(self.call._send_audio(CaptureSocket()))
        await asyncio.wait_for(sent_once.wait(), 1)
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)

        # A 20 ms Twilio μ-law frame becomes approximately 20 ms of 48 kHz PCM.
        self.assertLess(abs(len(sent_audio[0]) / 2 / 48000 - 0.020), 0.00011)

    async def test_flux_stt_connection_declares_48khz_linear16(self):
        opened_urls = []

        def fake_connect(url, **_kwargs):
            opened_urls.append(url)
            return DeepgramConnectionContext(EmptyDeepgramSocket())

        with patch.object(voice_app, "connect", fake_connect):
            await self.call._run()

        query = parse_qs(urlparse(opened_urls[0]).query)
        self.assertEqual(query["encoding"], ["linear16"])
        self.assertEqual(query["sample_rate"], ["48000"])

    async def test_flux_tts_24khz_pcm_is_encoded_as_twilio_8khz_mulaw(self):
        deepgram_socket = FakeTtsSocket()
        opened_urls = []

        async def fake_connect(url, **_kwargs):
            opened_urls.append(url)
            return deepgram_socket

        self.call.tts_done_event = asyncio.Event()
        self.call.response_started_at = time.monotonic()
        with patch.object(voice_app, "connect", fake_connect):
            await self.call._ensure_persistent_tts()
            await asyncio.wait_for(self.call.tts_done_event.wait(), 1)
            await self.call.tts_reader_task

        query = parse_qs(urlparse(opened_urls[0]).query)
        self.assertEqual(query["encoding"], ["linear16"])
        self.assertEqual(query["sample_rate"], ["24000"])
        media = next(item for item in self.websocket.sent if item.get("event") == "media")
        encoded_payload = media["media"]["payload"]
        self.assertEqual(len(base64.b64decode(encoded_payload)), 160)

    async def test_selected_voice_overrides_bridge_default_for_tts_connection(self):
        opened_urls = []

        async def fake_connect(url, **_kwargs):
            opened_urls.append(url)
            return FakeTtsSocket()

        selected = voice_app.TwilioVoiceCall(
            "twc_voice", 1, "MZ_voice", self.websocket, self.call.settings,
            voice_app.BridgeMetrics(), tts_model="flux-hannah-en",
        )
        self.addAsyncCleanup(selected.http.aclose)
        self.addAsyncCleanup(selected._close_persistent_tts)
        with patch.object(voice_app, "connect", fake_connect):
            await selected._ensure_persistent_tts()
            await selected.tts_reader_task

        query = parse_qs(urlparse(opened_urls[0]).query)
        self.assertEqual(query["model"], ["flux-hannah-en"])

    async def test_inbound_eager_turn_streams_once_and_commits_the_final_answer(self):
        agent_http = FakeAgentHttp()
        self.call.http = agent_http
        tts_socket = StreamingFakeTtsSocket()

        async def fake_connect(_url, **_kwargs):
            return tts_socket

        transcript = "Can you help me with that?"
        deepgram = DeepgramTurnSocket([
            {"type": "TurnInfo", "event": "StartOfTurn", "turn_index": 1, "transcript": ""},
            {"type": "TurnInfo", "event": "EagerEndOfTurn", "turn_index": 1, "transcript": transcript},
            {"type": "TurnInfo", "event": "EndOfTurn", "turn_index": 1, "transcript": transcript},
        ])
        with patch.object(voice_app, "connect", fake_connect):
            await self.call._receive_transcripts(deepgram)
            await asyncio.wait_for(self.call.response_task, 1)

        self.assertEqual(len(agent_http.stream_requests), 1)
        request = agent_http.stream_requests[0]
        self.assertTrue(request["json"]["speculative"])
        self.assertEqual(request["json"]["transcript"], transcript)
        self.assertEqual(len(agent_http.posts), 1)
        self.assertTrue(agent_http.posts[0]["url"].endswith("/commit-turn"))
        self.assertEqual(agent_http.posts[0]["json"]["text"], "I can help you with that.")
        self.assertEqual([event["type"] for event in tts_socket.sent].count("Speak"), 1)
        latency = self.call.metrics.snapshot(1)["twilio"]["latencyMs"]
        self.assertEqual(latency["eagerToFirstAudio"]["count"], 1)
        self.assertEqual(latency["endOfTurnToFirstAudio"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
