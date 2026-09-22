import base64
import hashlib
import hmac
import unittest

from recall_video import RecallScreenShareSampler, parse_screenshare_frame, valid_visual_handoff_url, verify_recall_websocket_signature, visual_configuration_issue, visual_configuration_status


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/WQAAAABJRU5ErkJggg=="
SECRET = "whsec_" + base64.b64encode(b"workspace-secret-for-tests-32-bytes").decode()


def event(frame=PNG, media_type="screenshare"):
    return {
        "event": "video_separate_png.data",
        "data": {
            "data": {"buffer": frame, "type": media_type},
            "bot": {"id": "recall_bot_123", "metadata": {"chusky_meeting_id": "mtg_video_1", "chusky_user_id": "42"}},
        },
    }


def signed_headers(secret=SECRET, message_id="msg_video_123", timestamp="1800000000", payload=""):
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signature = base64.b64encode(hmac.new(key, f"{message_id}.{timestamp}.{payload}".encode(), hashlib.sha256).digest()).decode()
    return {"webhook-id": message_id, "webhook-timestamp": timestamp, "webhook-signature": f"v1,{signature}"}


class RecallVideoTests(unittest.TestCase):
    def test_optional_visual_health_requires_valid_https_handoff_and_workspace_secret(self):
        self.assertEqual(visual_configuration_status("", ""), "disabled")
        self.assertEqual(visual_configuration_issue("", ""), "disabled")
        self.assertEqual(visual_configuration_issue("", SECRET), "missing_handoff_url")
        self.assertEqual(visual_configuration_issue("https://chusky.example/frame", ""), "missing_workspace_secret")
        self.assertEqual(visual_configuration_status("https://chusky.example/internal/recall/visual-frame", SECRET), "configured")
        self.assertEqual(visual_configuration_status("http://chusky.example/frame", SECRET), "misconfigured")
        self.assertEqual(visual_configuration_status("https://chusky.example/frame?token=x", SECRET), "misconfigured")
        self.assertEqual(visual_configuration_status("https://chusky.example/frame", "whsec_invalid"), "misconfigured")
        self.assertFalse(valid_visual_handoff_url("https://user:password@chusky.example/frame"))

    def test_verifies_recall_websocket_upgrade_with_empty_payload_and_fresh_timestamp(self):
        headers = signed_headers()
        self.assertTrue(verify_recall_websocket_signature(SECRET, headers, now_seconds=1_800_000_000))
        self.assertFalse(verify_recall_websocket_signature(SECRET, headers, now_seconds=1_800_000_400))
        wrong_secret = "whsec_" + base64.b64encode(b"another-wrong-key-for-signing-32bytes").decode()
        self.assertFalse(verify_recall_websocket_signature(SECRET, signed_headers(secret=wrong_secret), now_seconds=1_800_000_000))
        self.assertFalse(verify_recall_websocket_signature(SECRET, {"webhook-id": "missing"}, now_seconds=1_800_000_000))

    def test_extracts_only_signed_screen_share_frame_and_minimum_owned_bot_metadata(self):
        parsed = parse_screenshare_frame(event())
        self.assertEqual(parsed, {"provider_bot_id": "recall_bot_123", "meeting_id": "mtg_video_1", "user_id": 42, "buffer": PNG})
        self.assertIsNone(parse_screenshare_frame(event(media_type="webcam")))
        self.assertIsNone(parse_screenshare_frame({**event(), "event": "participant_events.chat_message"}))
        malformed = event()
        malformed["data"]["bot"]["metadata"]["chusky_user_id"] = "0"
        self.assertIsNone(parse_screenshare_frame(malformed))

    def test_sampler_caps_rate_deduplicates_unchanged_frames_and_rejects_bad_png(self):
        sampler = RecallScreenShareSampler(min_interval_seconds=2.5)
        self.assertTrue(sampler.accept(PNG, now=10))
        self.assertFalse(sampler.accept(PNG, now=13), "unchanged frame is not forwarded")
        different = bytearray(base64.b64decode(PNG))
        different[-5] ^= 0x01
        changed = base64.b64encode(different).decode()
        self.assertFalse(sampler.accept(changed, now=11), "rate limit applies even to a changed frame")
        self.assertTrue(sampler.accept(changed, now=13))
        self.assertFalse(sampler.accept("not-png", now=20))

    def test_sampler_refreshes_static_slides_on_a_bounded_heartbeat(self):
        sampler = RecallScreenShareSampler(min_interval_seconds=2.5, refresh_interval_seconds=8)
        self.assertTrue(sampler.accept(PNG, now=10))
        self.assertFalse(sampler.accept(PNG, now=17.9))
        self.assertTrue(sampler.accept(PNG, now=18), "static screen is refreshed before the encrypted frame TTL expires")

    def test_sampler_can_retry_same_static_frame_after_transient_handoff_failure(self):
        sampler = RecallScreenShareSampler(min_interval_seconds=2.5)
        self.assertTrue(sampler.accept(PNG, now=10))
        sampler.retry()
        self.assertFalse(sampler.accept(PNG, now=11), "transient retries remain rate limited")
        self.assertTrue(sampler.accept(PNG, now=13), "unchanged image can be resent when the meeting becomes ready")


if __name__ == "__main__":
    unittest.main()
