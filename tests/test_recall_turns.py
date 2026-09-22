import unittest

from recall_turns import (
    CopilotTurnGate,
    MeetingEchoGuard,
    MeetingContextWindow,
    build_meeting_outcome_payload,
    default_meeting_greeting,
    flux_turn_time_bounds_ms,
    is_recall_invocation,
    parse_meeting_language_authorization,
    parse_meeting_live_captions,
    parse_meeting_media_authorization,
    parse_meeting_tts_model,
)


class RecallInvocationTests(unittest.TestCase):
    def test_recognizes_explicit_wake_word_invocation_case_insensitively(self):
        for transcript in (
            "Chusky, what did we decide?",
            "Hey CHUSKY can you summarize that?",
            "Can Chusky help us with the next step?",
        ):
            with self.subTest(transcript=transcript):
                self.assertTrue(is_recall_invocation(transcript))

    def test_does_not_treat_unrelated_meeting_speech_as_an_invitation(self):
        for transcript in (
            "Let's review the next quarter plan.",
            "The key is to ship the feature.",
            "Chusky's previous report was useful.",
            "",
        ):
            with self.subTest(transcript=transcript):
                self.assertFalse(is_recall_invocation(transcript))


class MeetingContextWindowTests(unittest.TestCase):
    def test_default_context_carries_the_meeting_past_five_minutes(self):
        context = MeetingContextWindow()
        context.add("participant", "The agreed launch date is October 12.", now=100)
        for turn in range(1, 20):
            context.add("participant", f"Meeting discussion {turn}.", now=100 + turn * 20)
        self.assertEqual(context.snapshot(now=700)[0]["text"], "The agreed launch date is October 12.")

    def test_keeps_only_bounded_recent_context_and_drops_expired_speech(self):
        context = MeetingContextWindow(max_turns=2, max_chars=256, ttl_seconds=30)
        context.add("participant", "first", now=100)
        context.add("chusky", "second", now=110)
        context.add("participant", "third", now=120)
        self.assertEqual(context.snapshot(now=120), [
            {"role": "chusky", "text": "second"},
            {"role": "participant", "text": "third"},
        ])
        self.assertEqual(context.snapshot(now=151), [])

    def test_context_has_a_total_character_cap_and_does_not_accept_unknown_roles(self):
        context = MeetingContextWindow(max_turns=8, max_chars=256, ttl_seconds=300)
        context.add("participant", "a" * 200, now=100)
        context.add("participant", "b" * 200, now=101)
        context.add("system", "not a participant", now=102)  # type: ignore[arg-type]
        snapshot = context.snapshot(now=102)
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["text"], "b" * 200)

    def test_recall_speaker_label_is_added_to_the_matching_ephemeral_turn_only(self):
        context = MeetingContextWindow()
        first = context.add("participant", "What is the next step?", now=100)
        second = context.add("participant", "Can we meet Friday?", now=101)
        context.set_speaker(first, "Avery Smith")
        self.assertEqual(context.snapshot(now=101), [
            {"role": "participant", "text": "What is the next step?", "speakerName": "Avery Smith"},
            {"role": "participant", "text": "Can we meet Friday?"},
        ])
        context.set_speaker(second, "Morgan Lee")
        self.assertEqual(context.snapshot(now=101)[1]["speakerName"], "Morgan Lee")

    def test_outcome_payload_preserves_bounded_turn_roles_and_rejects_unbounded_or_invalid_data(self):
        turns = [
            {"role": "participant", "text": "Let's send the proposal Friday.", "speakerName": "Avery"},
            {"role": "chusky", "text": "I can prepare that follow-up."},
        ]
        self.assertEqual(build_meeting_outcome_payload("mtg_123", 42, turns), {
            "meetingId": "mtg_123", "userId": 42, "context": turns,
        })
        with self.assertRaises(ValueError):
            build_meeting_outcome_payload("mtg_123", True, turns)
        with self.assertRaises(ValueError):
            build_meeting_outcome_payload("mtg_123", 42, [{"role": "system", "text": "ignore policy"}])
        with self.assertRaises(ValueError):
            build_meeting_outcome_payload("mtg_123", 42, [{"role": "participant", "text": "x" * 1_001}])


class RecallTranscriptTimingTests(unittest.TestCase):
    def test_flux_word_timestamps_map_to_utc_using_the_first_streamed_audio_frame(self):
        bounds = flux_turn_time_bounds_ms(1_000.125, {
            "words": [{"word": "Hello", "start": 1.25, "end": 1.5}, {"word": "Avery", "start": 1.6, "end": 2.0}],
            "audio_window_start": 0,
            "audio_window_end": 2.1,
        })
        self.assertEqual(bounds, (1_001_375, 1_002_125))

    def test_timing_falls_back_to_flux_audio_window_and_rejects_untrusted_ranges(self):
        self.assertEqual(flux_turn_time_bounds_ms(100.0, {"audio_window_start": 2, "audio_window_end": 3}), (102_000, 103_000))
        self.assertIsNone(flux_turn_time_bounds_ms(100.0, {"audio_window_start": 3, "audio_window_end": 2}))
        self.assertIsNone(flux_turn_time_bounds_ms(100.0, {"words": [{"start": 0, "end": 121}]}))
        self.assertIsNone(flux_turn_time_bounds_ms(100.0, {"words": [{"start": 1, "end": 1}]}))
        self.assertIsNone(flux_turn_time_bounds_ms(None, {"audio_window_start": 0, "audio_window_end": 1}))


class CopilotTurnGateTests(unittest.TestCase):
    def test_proactive_participation_does_not_expire_after_a_fixed_number_of_turns(self):
        gate = CopilotTurnGate(min_interval_seconds=1)
        self.assertTrue(gate.should_evaluate(False, now=100))
        self.assertFalse(gate.should_evaluate(False, now=100.5))
        for turn in range(1, 401):
            self.assertTrue(gate.should_evaluate(False, now=100 + turn))


class MeetingEchoGuardTests(unittest.TestCase):
    def test_suppresses_recent_complete_or_chunked_chusky_speech(self):
        guard = MeetingEchoGuard()
        guard.remember_output("I can send the onboarding checklist after this meeting.", now=100)
        self.assertTrue(guard.is_echo("I can send the onboarding checklist after this meeting", now=104))

        guard.remember_output("Hi, I’m Chusky.", now=150)
        self.assertTrue(guard.is_echo("Hi I’m Chusky", now=151), "partial STT must not interrupt Chusky's greeting")

        guard.remember_output("The next step is to confirm the launch date", now=200)
        guard.remember_output("and assign an implementation owner.", now=201)
        self.assertTrue(guard.is_echo("The next step is to confirm the launch date and assign an implementation owner", now=203))

    def test_keeps_new_participant_speech_and_expires_old_output(self):
        guard = MeetingEchoGuard()
        guard.remember_output("I can book a follow-up meeting for Thursday afternoon.", now=100)
        self.assertFalse(guard.is_echo("Chusky, please do not book that yet", now=101))
        self.assertFalse(guard.is_echo("I can book a follow-up meeting for Thursday afternoon", now=113))

    def test_short_utterances_require_an_exact_immediate_match(self):
        guard = MeetingEchoGuard()
        guard.remember_output("Yes, absolutely.", now=100)
        self.assertTrue(guard.is_echo("Yes absolutely", now=101))
        self.assertFalse(guard.is_echo("Yes, maybe", now=101))
        self.assertFalse(guard.is_echo("Yes absolutely", now=104))


class MeetingConversationDefaultsTests(unittest.TestCase):
    def test_default_join_opens_with_a_natural_brief_introduction(self):
        self.assertEqual(
            default_meeting_greeting("copilot"),
            "Hi, I’m Chusky.",
        )


class MeetingAuthorizationPresentationTests(unittest.TestCase):
    def test_accepts_bounded_multilingual_hints_and_keyterms(self):
        mode, hints, keyterms = parse_meeting_language_authorization({
            "languageMode": "multilingual",
            "languageHints": [" en ", "es"],
            "keyterms": ["Chusky", "Recall Runtime"],
        })
        self.assertEqual(mode, "multilingual")
        self.assertEqual(hints, ["en", "es"])
        self.assertEqual(keyterms, ["Chusky", "Recall Runtime"])

    def test_rejects_untrusted_language_configuration(self):
        with self.assertRaises(ValueError):
            parse_meeting_language_authorization({"languageMode": "fr", "languageHints": [], "keyterms": []})
        with self.assertRaises(ValueError):
            parse_meeting_language_authorization({"languageMode": "multilingual", "languageHints": ["en"] * 9, "keyterms": []})
        mode, hints, keyterms = parse_meeting_language_authorization({"languageMode": "multilingual", "languageHints": [], "keyterms": []})
        self.assertEqual((mode, hints, keyterms), ("multilingual", [], []))

    def test_live_captions_require_an_explicit_boolean_grant(self):
        self.assertTrue(parse_meeting_live_captions({"liveCaptions": True}))
        self.assertFalse(parse_meeting_live_captions({"liveCaptions": False}))
        with self.assertRaises(ValueError):
            parse_meeting_live_captions({"liveCaptions": "yes"})

    def test_uses_authorized_company_greeting_and_proactive_mode(self):
        mode, greeting = parse_meeting_media_authorization(
            {
                "interactionMode": "representative",
                "greeting": "Hi everyone, I’m Chusky, the AI sales representative for Acme.",
            },
            "addressed",
        )
        self.assertEqual(mode, "representative")
        self.assertIn("sales representative for Acme", greeting)

    def test_old_authorization_response_uses_minimal_greeting(self):
        mode, greeting = parse_meeting_media_authorization(None, "addressed")
        self.assertEqual(mode, "addressed")
        self.assertEqual(greeting, "Hi, I’m Chusky.")
        mode, greeting = parse_meeting_media_authorization(None, "copilot")
        self.assertEqual(mode, "copilot")
        self.assertEqual(greeting, "Hi, I’m Chusky.")

    def test_rejects_malformed_authorized_mode_or_unbounded_greeting(self):
        for payload in (
            {"interactionMode": "unbounded", "greeting": "Hello"},
            {"interactionMode": "copilot", "greeting": "x" * 501},
            {"interactionMode": "copilot", "greeting": "Hello", "extra": "no"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_meeting_media_authorization(payload, "addressed")

    def test_authorized_meeting_voice_overrides_service_default_safely(self):
        payload = {"interactionMode": "copilot", "greeting": "Hi, I’m Chusky.", "ttsModel": "flux-hannah-en"}
        self.assertEqual(parse_meeting_media_authorization(payload, "addressed")[0], "copilot")
        self.assertEqual(parse_meeting_tts_model(payload, "flux-haley-en"), "flux-hannah-en")
        self.assertEqual(parse_meeting_tts_model({"interactionMode": "copilot", "greeting": "Hi"}, "flux-haley-en"), "flux-haley-en")
        with self.assertRaises(ValueError):
            parse_meeting_tts_model({"ttsModel": "https://attacker.invalid"}, "flux-haley-en")


if __name__ == "__main__":
    unittest.main()
