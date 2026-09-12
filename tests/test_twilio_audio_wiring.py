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


class TwilioAudioWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.websocket = FakeTwilioWebSocket()
        settings = SimpleNamespace(
            bridge_secret="test-bridge-secret",
            deepgram_api_key="test-deepgram-key",
            chusky_turn_url="http://localhost/internal/facetime/turn",
            chusky_status_url="http://localhost/internal/facetime/status",
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


if __name__ == "__main__":
    unittest.main()
