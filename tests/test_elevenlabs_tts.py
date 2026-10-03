import base64
import unittest

from elevenlabs_tts import parse_audio_event, stream_url


class ElevenLabsTtsHelperTests(unittest.TestCase):
    def test_stream_url_keeps_api_key_out_of_url(self):
        url = stream_url("voice_123", "eleven_flash_v2_5")
        self.assertIn("model_id=eleven_flash_v2_5", url)
        self.assertIn("output_format=ulaw_8000", url)
        self.assertNotIn("api_key", url)

    def test_parser_accepts_provider_final_event_and_bounded_audio(self):
        event = parse_audio_event(
            '{"audio":"' + base64.b64encode(b"audio").decode() + '","isFinal":true}'
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.audio, b"audio")
        self.assertTrue(event.is_final)

    def test_parser_discards_invalid_audio(self):
        self.assertIsNone(parse_audio_event('{"audio":"not base64"}'))


if __name__ == "__main__":
    unittest.main()
