import unittest
from urllib.parse import parse_qs, urlparse

from audio_formats import (
    DEEPGRAM_INPUT_SAMPLE_RATE,
    DEEPGRAM_OUTPUT_SAMPLE_RATE,
    TWILIO_SAMPLE_RATE,
    deepgram_linear16_to_twilio_mulaw,
    deepgram_flux_listen_url,
    deepgram_flux_speak_url,
    deepgram_nova_listen_url,
    twilio_deepgram_listen_url,
    twilio_deepgram_speak_url,
    twilio_mulaw_to_deepgram_linear16,
)


class TwilioAudioFormatTests(unittest.TestCase):
    def test_twilio_twenty_ms_frame_becomes_48khz_linear16_without_duration_change(self):
        self.assertEqual(TWILIO_SAMPLE_RATE, 8000)
        self.assertEqual(DEEPGRAM_INPUT_SAMPLE_RATE, 48000)

        # 160 mu-law samples represent 20 ms at Twilio's fixed 8 kHz format.
        pcm, _state = twilio_mulaw_to_deepgram_linear16(b"\xff" * 160)

        # ratecv carries interpolation state between frames; the first frame
        # may omit the initial interpolation edge (at most one 48 kHz sample).
        duration_seconds = len(pcm) / 2 / DEEPGRAM_INPUT_SAMPLE_RATE
        self.assertLess(abs(duration_seconds - 0.020), 0.00011)

    def test_resampling_state_keeps_adjacent_twilio_frames_contiguous(self):
        state = None
        converted = bytearray()
        for _ in range(50):
            frame, state = twilio_mulaw_to_deepgram_linear16(b"\xff" * 160, state)
            converted.extend(frame)

        duration_seconds = len(converted) / 2 / DEEPGRAM_INPUT_SAMPLE_RATE
        self.assertLess(abs(duration_seconds - 1.0), 0.00011)

    def test_24khz_linear16_is_converted_back_to_twilio_mu_law(self):
        self.assertEqual(DEEPGRAM_OUTPUT_SAMPLE_RATE, 24000)

        # 480 mono PCM samples at 24 kHz represent 20 ms.
        mulaw, _state = deepgram_linear16_to_twilio_mulaw(b"\x00\x00" * 480)

        self.assertEqual(len(mulaw), 160)

    def test_rejects_partial_linear16_samples(self):
        with self.assertRaises(ValueError):
            deepgram_linear16_to_twilio_mulaw(b"\x00")

    def test_flux_listen_contract_uses_48khz_linear16_and_turn_detection(self):
        url = twilio_deepgram_listen_url("flux-general-en", 0.45, 0.65, 800)
        query = parse_qs(urlparse(url).query)

        self.assertEqual(urlparse(url).path, "/v2/listen")
        self.assertEqual(query["model"], ["flux-general-en"])
        self.assertEqual(query["encoding"], ["linear16"])
        self.assertEqual(query["sample_rate"], ["48000"])
        self.assertEqual(query["eager_eot_threshold"], ["0.45"])
        self.assertEqual(query["eot_threshold"], ["0.65"])
        self.assertEqual(query["eot_timeout_ms"], ["800"])

        # Recall's webpage supplies the same 48 kHz mono PCM directly, without
        # Twilio's μ-law conversion; both transports share the Flux contract.
        recall_url = deepgram_flux_listen_url("flux-general-en", 0.45, 0.65, 800)
        self.assertEqual(parse_qs(urlparse(recall_url).query), query)

    def test_flux_speak_contract_uses_24khz_linear16(self):
        url = twilio_deepgram_speak_url("flux-haley-en")
        query = parse_qs(urlparse(url).query)

        self.assertEqual(urlparse(url).path, "/v2/speak")
        self.assertEqual(query["model"], ["flux-haley-en"])
        self.assertEqual(query["encoding"], ["linear16"])
        self.assertEqual(query["sample_rate"], ["24000"])
        self.assertEqual(deepgram_flux_speak_url("flux-haley-en"), url)

    def test_nova_listen_contract_uses_v1_vad_endpointing_and_stable_results(self):
        url = deepgram_nova_listen_url("nova-3", 500, 1000)
        query = parse_qs(urlparse(url).query)

        self.assertEqual(urlparse(url).path, "/v1/listen")
        self.assertEqual(query["model"], ["nova-3"])
        self.assertEqual(query["encoding"], ["linear16"])
        self.assertEqual(query["sample_rate"], ["48000"])
        self.assertEqual(query["channels"], ["1"])
        self.assertEqual(query["interim_results"], ["true"])
        self.assertEqual(query["vad_events"], ["true"])
        self.assertEqual(query["endpointing"], ["500"])
        self.assertEqual(query["utterance_end_ms"], ["1000"])


if __name__ == "__main__":
    unittest.main()
