import os
import unittest
from unittest.mock import patch

import app as voice_app


class RecallMediaPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_page_is_served_even_when_audio_configuration_is_missing(self):
        with patch.object(
            voice_app.RecallSettings,
            "from_env",
            side_effect=AssertionError("static page must not depend on audio configuration"),
        ):
            response = await voice_app.recall_media_page()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Digital assistant", response.body)
        self.assertIn("https://chusky-web.vercel.app", response.headers["content-security-policy"])
        self.assertIn("no-store", response.headers["cache-control"])

    def test_invalid_audio_configuration_reports_only_safe_field_names(self):
        settings = {
            "RECALL_MEETINGS_ENABLED": "true",
            "RECALL_MEDIA_BRIDGE_SECRET": "test-short-secret",
            "DEEPGRAM_API_KEY": "",
            "CHUSKY_RECALL_TURN_STREAM_URL": "http://turn.invalid",
            "CHUSKY_RECALL_COMMIT_TURN_URL": "",
            "CHUSKY_RECALL_MEDIA_AUTHORIZE_URL": "",
        }
        with patch.dict(os.environ, settings, clear=True):
            with self.assertRaises(voice_app.RecallConfigurationError) as raised:
                voice_app.RecallSettings.from_env()

        self.assertEqual(raised.exception.code, "required_settings_invalid")
        self.assertIn("RECALL_MEDIA_BRIDGE_SECRET", raised.exception.fields)
        self.assertIn("DEEPGRAM_API_KEY", raised.exception.fields)
        self.assertNotIn("test-short-secret", str(raised.exception))

    async def test_missing_media_asset_returns_branded_dark_recovery_page(self):
        with patch.object(voice_app, "Path") as path:
            path.return_value.with_name.return_value.read_text.side_effect = OSError("private filesystem detail")
            response = await voice_app.recall_media_page()

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"digital assistant", response.body.lower())
        self.assertIn(b"temporarily unavailable", response.body.lower())
        self.assertNotIn(b"private filesystem detail", response.body)
        self.assertNotIn(b"Meeting assistant unavailable", response.body)
        self.assertIn(b"background: #101216", response.body)
        self.assertIn("no-store", response.headers["cache-control"])


if __name__ == "__main__":
    unittest.main()
