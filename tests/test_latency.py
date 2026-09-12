import asyncio
import unittest

from latency import latency_summary, resolve_speculative_draft, take_tts_chunk


class ResolveSpeculativeDraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_a_completed_draft_for_the_same_transcript(self):
        async def draft():
            return "The requested information is ready."

        task = asyncio.create_task(draft())
        await task

        result = await resolve_speculative_draft(task, transcript_matches=True)

        self.assertEqual(result, "The requested information is ready.")

    async def test_cancels_an_unfinished_matching_draft_instead_of_waiting_for_it(self):
        started = asyncio.Event()

        async def slow_draft():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(slow_draft())
        await started.wait()

        result = await resolve_speculative_draft(task, transcript_matches=True, grace_ms=0)

        self.assertIsNone(result)
        self.assertTrue(task.cancelled())

    async def test_does_not_reuse_a_draft_for_a_different_transcript(self):
        task = asyncio.create_task(asyncio.sleep(0, result="old transcript"))
        await task

        result = await resolve_speculative_draft(task, transcript_matches=False)

        self.assertIsNone(result)
        self.assertEqual(task.result(), "old transcript")


class TakeTtsChunkTests(unittest.TestCase):
    def test_waits_for_more_text_below_the_soft_limit(self):
        self.assertIsNone(take_tts_chunk("A short first phrase."))

    def test_emits_at_a_word_boundary_and_preserves_all_text(self):
        text = "Here is a natural sentence that is long enough to speak before the whole answer is generated."

        chunk = take_tts_chunk(text)

        self.assertIsNotNone(chunk)
        spoken, remainder = chunk
        self.assertLessEqual(len(spoken), 48)
        self.assertEqual(spoken + remainder, text)
        self.assertFalse(spoken.endswith(" "))

    def test_hard_limit_prevents_a_long_unbroken_token_from_stalling_speech(self):
        text = "x" * 90

        chunk = take_tts_chunk(text)

        self.assertEqual(chunk, ("x" * 80, "x" * 10))


class LatencySummaryTests(unittest.TestCase):
    def test_reports_bounded_sample_count_and_tail_latency(self):
        self.assertEqual(
            latency_summary([10, 20, 30, 40, 50]),
            {"count": 5, "average": 30, "p50": 30, "p95": 50},
        )

    def test_empty_latency_samples_are_reported_as_unavailable(self):
        self.assertEqual(latency_summary([]), {"count": 0, "average": None, "p50": None, "p95": None})


if __name__ == "__main__":
    unittest.main()
