import base64
import hashlib
import hmac
import json
import unittest

from recall_auth import valid_recall_ticket


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


if __name__ == "__main__":
    unittest.main()
