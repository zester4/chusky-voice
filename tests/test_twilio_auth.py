import hashlib
import hmac
import time
import unittest

from twilio_auth import valid_twilio_ticket


class TwilioTicketVoiceBindingTests(unittest.TestCase):
    def test_ticket_binds_selected_voice_and_preserves_legacy_default_ticket(self):
        expiry = int(time.time() * 1000) + 60_000
        call_id, user_id, secret, voice = "twc_test", 7, "bridge-secret", "flux-haley-en"
        payload = f"{call_id}.{user_id}.{expiry}.{voice}"
        signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        ticket = f"{expiry}.{voice}.{signature}"
        self.assertTrue(valid_twilio_ticket(call_id, user_id, ticket, secret, voice))
        self.assertFalse(valid_twilio_ticket(call_id, user_id, ticket, secret, "flux-kit-en"))
        self.assertFalse(valid_twilio_ticket(call_id, user_id, ticket, secret))
        self.assertFalse(valid_twilio_ticket("twc_other", user_id, ticket, secret, voice))

        legacy_payload = f"{call_id}.{user_id}.{expiry}"
        legacy_signature = hmac.new(secret.encode(), legacy_payload.encode(), hashlib.sha256).hexdigest()
        legacy_ticket = f"{expiry}.{legacy_signature}"
        self.assertTrue(valid_twilio_ticket(call_id, user_id, legacy_ticket, secret))
        self.assertFalse(valid_twilio_ticket(call_id, user_id, legacy_ticket, secret, voice))

    def test_ticket_rejects_malformed_and_expired_models(self):
        expiry = int(time.time() * 1000) + 60_000
        for ticket, voice in (("bad.ticket", None), (f"{expiry}.https://example.com.signature", "https://example.com")):
            with self.subTest(ticket=ticket):
                self.assertFalse(valid_twilio_ticket("twc_test", 7, ticket, "bridge-secret", voice))
        expired = expiry - 120_000
        payload = f"twc_test.7.{expired}.flux-haley-en"
        signature = hmac.new(b"bridge-secret", payload.encode(), hashlib.sha256).hexdigest()
        self.assertFalse(valid_twilio_ticket("twc_test", 7, f"{expired}.flux-haley-en.{signature}", "bridge-secret", "flux-haley-en"))


if __name__ == "__main__":
    unittest.main()
