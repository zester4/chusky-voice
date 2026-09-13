import unittest

from speech_text import normalize_voice_delta, normalize_voice_text


class RecallSpeechTextTests(unittest.TestCase):
    def test_preserves_stream_delta_boundaries_until_a_complete_phrase_is_ready(self):
        self.assertEqual(normalize_voice_delta(" clarify"), " clarify")
        self.assertEqual(
            normalize_voice_text("The price is $4.99. plan1.clarify your idea."),
            "The price is 4 dollars and 99 cents. plan 1. clarify your idea.",
        )


if __name__ == "__main__":
    unittest.main()
