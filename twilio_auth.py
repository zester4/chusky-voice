"""Short-lived Twilio bridge ticket verification; intentionally stdlib-only."""
from __future__ import annotations

import hashlib
import hmac
import re
import time


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
