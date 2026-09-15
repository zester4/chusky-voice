import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from twilio_auth import valid_twilio_websocket


STREAM_URL = "wss://voice.example/twilio/stream"
SIGNATURE = "twilio-signature"


class StubRequestValidator:
    accepted_urls = set()
    calls = []
    error = None

    def __init__(self, auth_token):
        self.auth_token = auth_token

    def validate(self, url, params, signature):
        self.calls.append((url, params, signature, self.auth_token))
        if self.error:
            raise self.error
        return url in self.accepted_urls and signature == SIGNATURE


class TwilioWebSocketSignatureTests(unittest.TestCase):
    def setUp(self):
        StubRequestValidator.accepted_urls = set()
        StubRequestValidator.calls = []
        StubRequestValidator.error = None
        twilio_module = types.ModuleType("twilio")
        twilio_module.__path__ = []
        validator_module = types.ModuleType("twilio.request_validator")
        validator_module.RequestValidator = StubRequestValidator
        self.sdk_modules = {
            "twilio": twilio_module,
            "twilio.request_validator": validator_module,
        }
        self.settings = SimpleNamespace(
            twilio_auth_token="test-auth-token",
            twilio_media_stream_url=STREAM_URL,
        )
        self.websocket = SimpleNamespace(
            headers={"x-twilio-signature": SIGNATURE},
            url=STREAM_URL,
        )

    def validate(self):
        with patch.dict(sys.modules, self.sdk_modules):
            return valid_twilio_websocket(self.websocket, self.settings)

    def test_accepts_valid_signature_for_configured_wss_url(self):
        StubRequestValidator.accepted_urls = {STREAM_URL}

        self.assertTrue(self.validate())
        self.assertEqual(
            StubRequestValidator.calls,
            [(STREAM_URL, {}, SIGNATURE, "test-auth-token")],
        )

    def test_retries_only_with_twilios_documented_trailing_slash(self):
        StubRequestValidator.accepted_urls = {STREAM_URL + "/"}

        self.assertTrue(self.validate())
        self.assertEqual(
            [call[0] for call in StubRequestValidator.calls],
            [STREAM_URL, STREAM_URL + "/"],
        )

    def test_rejects_missing_auth_or_signature(self):
        self.settings.twilio_auth_token = ""
        self.assertFalse(self.validate())
        self.assertEqual(StubRequestValidator.calls, [])

        self.settings.twilio_auth_token = "test-auth-token"
        self.websocket.headers = {}
        self.assertFalse(self.validate())
        self.assertEqual(StubRequestValidator.calls, [])

    def test_rejects_wrong_host_path_and_query_before_sdk_validation(self):
        for request_url in (
            "wss://attacker.example/twilio/stream",
            "wss://voice.example/other",
            "wss://voice.example/twilio/stream//",
            "wss://voice.example/twilio/stream?forged=1",
        ):
            with self.subTest(request_url=request_url):
                self.websocket.url = request_url
                self.assertFalse(self.validate())

        self.assertEqual(StubRequestValidator.calls, [])

    def test_rejects_non_wss_or_query_bearing_config(self):
        for configured_url in (
            "https://voice.example/twilio/stream",
            "wss://voice.example/twilio/stream?ticket=secret",
        ):
            with self.subTest(configured_url=configured_url):
                self.settings.twilio_media_stream_url = configured_url
                self.assertFalse(self.validate())

        self.assertEqual(StubRequestValidator.calls, [])

    def test_fails_closed_if_twilio_sdk_validation_raises(self):
        StubRequestValidator.error = RuntimeError("validation unavailable")

        self.assertFalse(self.validate())


if __name__ == "__main__":
    unittest.main()
