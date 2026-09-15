"""Twilio Media Stream signature and short-lived bridge-ticket verification."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from urllib.parse import urlsplit


def valid_twilio_websocket(websocket, settings) -> bool:
    """Validate the Media Streams handshake with Twilio's official SDK.

    Use the configured public WSS URL as the signature URL instead of trusting
    forwarded Host headers. Twilio documents a trailing-slash compatibility
    retry for Voice WSS handshakes. The stream ticket is checked separately
    after Twilio's authenticated ``start`` message arrives.
    """
    auth_token = str(getattr(settings, "twilio_auth_token", "") or "").strip()
    configured_url = str(getattr(settings, "twilio_media_stream_url", "") or "").strip()
    try:
        signature = str(websocket.headers.get("x-twilio-signature", "") or "").strip()
        request_url = str(websocket.url)
        configured = urlsplit(configured_url)
        requested = urlsplit(request_url)
    except (AttributeError, TypeError, ValueError):
        return False

    if (
        not auth_token
        or not signature
        or configured.scheme.lower() != "wss"
        or not configured.hostname
        or configured.username is not None
        or configured.password is not None
        or configured.query
        or configured.fragment
        or requested.scheme.lower() not in {"ws", "wss"}
        or not requested.hostname
        or requested.hostname.lower() != configured.hostname.lower()
        or requested.path not in {"/twilio/stream", "/twilio/stream/"}
        or requested.query
        or requested.fragment
        or configured.path not in {"/twilio/stream", "/twilio/stream/"}
    ):
        return False

    try:
        # Keep Twilio's evolving signature rules in its SDK rather than
        # maintaining a local HMAC implementation.
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(auth_token)
        canonical_url = configured_url.rstrip("/")
        if validator.validate(canonical_url, {}, signature):
            return True
        return bool(validator.validate(f"{canonical_url}/", {}, signature))
    except Exception:
        # A missing/broken SDK or malformed signature must fail closed; the
        # websocket route must never become unauthenticated on validator error.
        return False


def valid_twilio_ticket(
    call_id: str,
    user_id: int,
    ticket: str,
    secret: str,
    tts_model: str | None = None,
) -> bool:
    """Verify ticket scope, expiry, and any account-selected Flux voice."""
    try:
        parts = ticket.split(".")
        if len(parts) == 2:
            expires, supplied = parts
            ticket_model = None
            if tts_model is not None:
                return False
        elif len(parts) == 3:
            expires, ticket_model, supplied = parts
            if not re.fullmatch(r"flux-[a-z]+-en", ticket_model) or ticket_model != tts_model:
                return False
        else:
            return False
        expires_at = int(expires)
    except (TypeError, ValueError):
        return False
    current = int(time.time() * 1000)
    if expires_at < current or expires_at > current + 6 * 60_000:
        return False
    payload = f"{call_id}.{user_id}.{expires_at}" + (f".{ticket_model}" if ticket_model else "")
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)
