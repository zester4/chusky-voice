import base64
import hashlib
import hmac
import json
import unittest

from recall_auth import valid_recall_ticket, wait_for_media_authorization


class RecallTicketTests(unittest.TestCase):
    def make_ticket(self, claims, secret="recall-bridge-secret"):
        payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        return f"{payload}.{encoded_signature}"

    def test_accepts_ticket_with_valid_signature_scope_and_expiry(self):
        claims = {"meetingId": "mtg_123", "userId": 42, "expiresAt": 1_800_000_100}
        ticket = self.make_ticket(claims)
        self.assertEqual(valid_recall_ticket(ticket, "recall-bridge-secret", 1_800_000_000), claims)

    def test_rejects_wrong_secret_expired_or_malformed_tickets(self):
        claims = {"meetingId": "mtg_123", "userId": 42, "expiresAt": 1_800_000_100}
        ticket = self.make_ticket(claims)
        self.assertIsNone(valid_recall_ticket(ticket, "wrong-secret", 1_800_000_000))
        self.assertIsNone(valid_recall_ticket(ticket, "recall-bridge-secret", 1_800_000_101))
        self.assertIsNone(valid_recall_ticket("not-a-ticket", "recall-bridge-secret", 1_800_000_000))

    def test_rejects_unscoped_or_overlong_lived_claims(self):
        current = 1_800_000_000
        for claims in (
            {"meetingId": "mtg_123", "userId": 42, "expiresAt": current + 100, "role": "admin"},
            {"meetingId": "mtg_123", "userId": 42, "expiresAt": current + 32 * 24 * 60 * 60},
            {"meetingId": "mtg_123", "userId": True, "expiresAt": current + 100},
        ):
            self.assertIsNone(valid_recall_ticket(self.make_ticket(claims), "recall-bridge-secret", current))

    def test_accepts_only_signed_addressed_or_copilot_modes(self):
        current = 1_800_000_000
        for mode in ("addressed", "copilot", "representative"):
            claims = {"meetingId": "mtg_123", "userId": 42, "expiresAt": current + 100, "interactionMode": mode}
            self.assertEqual(valid_recall_ticket(self.make_ticket(claims), "recall-bridge-secret", current), claims)
        claims = {"meetingId": "mtg_123", "userId": 42, "expiresAt": current + 100, "interactionMode": "unbounded"}
        self.assertIsNone(valid_recall_ticket(self.make_ticket(claims), "recall-bridge-secret", current))


class RecallMediaAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_call_transition_then_authorizes(self):
        statuses = iter((425, 425, 204))
        calls = 0
        elapsed = 0.0
        sleeps = []

        async def check():
            nonlocal calls
            calls += 1
            return next(statuses)

        def clock():
            return elapsed

        async def sleep(seconds):
            nonlocal elapsed
            sleeps.append(seconds)
            elapsed += seconds

        status = await wait_for_media_authorization(check, timeout_seconds=5, monotonic=clock, sleep=sleep)
        self.assertEqual(status, 204)
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    async def test_does_not_retry_credential_or_terminal_rejections(self):
        for rejected_status in (401, 404, 410):
            calls = 0

            async def check():
                nonlocal calls
                calls += 1
                return rejected_status

            self.assertEqual(await wait_for_media_authorization(check), rejected_status)
            self.assertEqual(calls, 1)

    async def test_pending_call_wait_is_bounded(self):
        calls = 0
        elapsed = 0.0

        async def check():
            nonlocal calls
            calls += 1
            return 425

        def clock():
            return elapsed

        async def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        status = await wait_for_media_authorization(
            check,
            timeout_seconds=1.25,
            initial_interval=0.5,
            max_interval=0.5,
            monotonic=clock,
            sleep=sleep,
        )
        self.assertEqual(status, 425)
        self.assertEqual(calls, 4)


if __name__ == "__main__":
    unittest.main()
