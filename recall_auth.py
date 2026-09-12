"""Recall session-ticket verification; kept stdlib-only for isolated testing."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any


def valid_recall_ticket(ticket: str, secret: str, now_seconds: int | None = None) -> dict[str, Any] | None:
    """Verify Node's scoped base64url HMAC ticket without logging its value."""
    try:
        payload, supplied = ticket.split(".", 1)
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
        signature = base64.urlsafe_b64decode(supplied + "=" * (-len(supplied) % 4))
        current = int(time.time()) if now_seconds is None else now_seconds
        if not isinstance(claims, dict):
            return None
        expires = claims.get("expiresAt")
        meeting_id = claims.get("meetingId")
        user_id = claims.get("userId")
        if not hmac.compare_digest(signature, expected):
            return None
        if set(claims) not in (
            {"meetingId", "userId", "expiresAt"},
            {"meetingId", "userId", "expiresAt", "interactionMode"},
        ):
            return None
        if not isinstance(meeting_id, str) or not re.fullmatch(r"mtg_[A-Za-z0-9_-]{1,80}", meeting_id):
            return None
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            return None
        if isinstance(expires, bool) or not isinstance(expires, int) or expires <= current or expires > current + 31 * 24 * 60 * 60:
            return None
        mode = claims.get("interactionMode", "addressed")
        if mode not in ("addressed", "copilot", "representative"):
            return None
        result = {"meetingId": meeting_id, "userId": user_id, "expiresAt": expires}
        if "interactionMode" in claims:
            result["interactionMode"] = mode
        return result
    except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError, base64.binascii.Error):
        return None


async def wait_for_media_authorization(
    check,
    *,
    timeout_seconds: float = 15.0,
    initial_interval: float = 0.5,
    max_interval: float = 2.0,
    monotonic=time.monotonic,
    sleep=asyncio.sleep,
) -> int:
    """Retry only the explicit pre-call state; never retry auth or terminal failures."""
    if timeout_seconds < 0 or initial_interval <= 0 or max_interval <= 0:
        raise ValueError("invalid media authorization retry policy")
    deadline = monotonic() + timeout_seconds
    interval = initial_interval
    while True:
        status = await check()
        if status != 425:
            return status
        remaining = deadline - monotonic()
        if remaining <= 0:
            return status
        await sleep(min(interval, remaining))
        interval = min(interval * 2, max_interval)
