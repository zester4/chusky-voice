import unittest
from unittest.mock import patch

import app as voice_app


def settings() -> voice_app.RecallSettings:
    return voice_app.RecallSettings(
        bridge_secret="b" * 32,
        deepgram_api_key="deepgram-test",
        turn_stream_url="https://chusky.test/internal/recall/turn-stream",
        commit_turn_url="https://chusky.test/internal/recall/commit-turn",
        media_authorize_url="https://chusky.test/internal/recall/media-authorize",
        visual_frame_url="",
        realtime_secret="",
        stt_model="nova-3",
        stt_eager_eot_threshold=0.45,
        stt_eot_threshold=0.65,
        stt_eot_timeout_ms=800,
        nova_endpointing_ms=500,
        nova_utterance_end_ms=1000,
        tts_model="flux-haley-en",
        max_meeting_seconds=7200,
        max_active_meetings=4,
        copilot_min_interval_seconds=4,
        turn_start_budget_ms=10000,
        turn_fallback_enabled=True,
    )


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return self.response


class RecallHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        voice_app._recall_probe_cache = None

    async def test_probe_accepts_invalid_payload_as_proof_route_and_secret_are_live(self):
        with patch.object(voice_app.httpx, "AsyncClient", return_value=FakeClient(FakeResponse(400))):
            self.assertEqual(await voice_app.probe_recall_media_authorization(settings()), "configured")

    async def test_probe_exposes_stale_root_deployment_without_secrets(self):
        with patch.object(voice_app.httpx, "AsyncClient", return_value=FakeClient(FakeResponse(404))):
            self.assertEqual(await voice_app.probe_recall_media_authorization(settings()), "route_missing")

    async def test_health_marks_route_missing_as_misconfigured(self):
        with patch.object(voice_app.RecallSettings, "from_env", return_value=settings()), patch.object(voice_app.httpx, "AsyncClient", return_value=FakeClient(FakeResponse(404))):
            result = await voice_app.recall_health()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "misconfigured")
        self.assertEqual(result["checks"]["mediaAuthorization"], "route_missing")


if __name__ == "__main__":
    unittest.main()
