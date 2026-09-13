import unittest

from recall_turns import (
    CopilotTurnGate,
    MeetingContextWindow,
    default_meeting_greeting,
    is_recall_invocation,
    parse_meeting_media_authorization,
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


class CopilotTurnGateTests(unittest.TestCase):
    def test_proactive_participation_does_not_expire_after_a_fixed_number_of_turns(self):
        gate = CopilotTurnGate(min_interval_seconds=1)
        self.assertTrue(gate.should_evaluate(False, now=100))
        self.assertFalse(gate.should_evaluate(False, now=100.5))
        for turn in range(1, 401):
            self.assertTrue(gate.should_evaluate(False, now=100 + turn))


class MeetingConversationDefaultsTests(unittest.TestCase):
    def test_default_join_opens_with_a_natural_brief_introduction(self):
        self.assertEqual(
            default_meeting_greeting("copilot"),
            "Hi everyone, I’m Chusky. I’ll follow along and join in when I can help.",
        )


class MeetingAuthorizationPresentationTests(unittest.TestCase):
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

    def test_old_authorization_response_uses_short_mode_appropriate_greeting(self):
        mode, greeting = parse_meeting_media_authorization(None, "addressed")
        self.assertEqual(mode, "addressed")
        self.assertIn("say my name", greeting.lower())
        mode, greeting = parse_meeting_media_authorization(None, "copilot")
        self.assertEqual(mode, "copilot")
        self.assertIn("join in when I can help", greeting)

    def test_rejects_malformed_authorized_mode_or_unbounded_greeting(self):
        for payload in (
            {"interactionMode": "unbounded", "greeting": "Hello"},
            {"interactionMode": "copilot", "greeting": "x" * 501},
            {"interactionMode": "copilot", "greeting": "Hello", "extra": "no"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_meeting_media_authorization(payload, "addressed")


if __name__ == "__main__":
    unittest.main()
