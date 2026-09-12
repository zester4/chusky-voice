import unittest

from recall_turns import CopilotTurnGate, MeetingContextWindow, is_recall_invocation


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
    def test_proactive_turns_are_rate_and_count_limited_but_direct_requests_remain_available(self):
        gate = CopilotTurnGate(min_interval_seconds=8, max_evaluations=2)
        self.assertEqual(gate.should_evaluate(False, now=100), (True, False))
        self.assertEqual(gate.should_evaluate(False, now=105), (False, False))
        self.assertEqual(gate.should_evaluate(False, now=108), (True, False))
        self.assertEqual(gate.should_evaluate(False, now=120), (False, True))
        self.assertEqual(gate.should_evaluate(True, now=120), (True, False))


if __name__ == "__main__":
    unittest.main()
