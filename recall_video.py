"""Bounded Recall shared-screen event validation and sampling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from typing import Any
from urllib.parse import urlsplit


MAX_FRAME_BYTES = 1_500_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _decode_recall_base64(value: str) -> bytes:
    """Decode Recall's standard base64 values with optional ``=`` padding."""
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded, validate=True)


def valid_recall_workspace_secret(secret: str) -> bool:
    if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 512:
        return False
    try:
        return len(_decode_recall_base64(secret[6:])) >= 16
    except (ValueError, TypeError):
        return False


def valid_visual_handoff_url(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def visual_configuration_issue(handoff_url: str, secret: str) -> str:
    """Return a safe operator diagnosis without returning configuration values."""
    if not handoff_url and not secret:
        return "disabled"
    if not handoff_url:
        return "missing_handoff_url"
    if not valid_visual_handoff_url(handoff_url):
        return "invalid_handoff_url"
    if not secret:
        return "missing_workspace_secret"
    if not valid_recall_workspace_secret(secret):
        return "invalid_workspace_secret"
    return "configured"


def visual_configuration_status(handoff_url: str, secret: str) -> str:
    issue = visual_configuration_issue(handoff_url, secret)
    return "configured" if issue == "configured" else "disabled" if issue == "disabled" else "misconfigured"


def _header(headers: Any, name: str) -> str | None:
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.title())
        return str(value) if value is not None else None
    except (AttributeError, TypeError):
        return None


def verify_recall_websocket_signature(
    secret: str,
    headers: Any,
    *,
    now_seconds: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify Recall's signed websocket upgrade (empty payload), with replay bounds."""
    if not valid_recall_workspace_secret(secret):
        return False
    message_id = _header(headers, "webhook-id") or _header(headers, "svix-id")
    timestamp = _header(headers, "webhook-timestamp") or _header(headers, "svix-timestamp")
    signatures = _header(headers, "webhook-signature") or _header(headers, "svix-signature")
    if not message_id or len(message_id) > 200 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", message_id):
        return False
    if not timestamp or not re.fullmatch(r"\d{1,12}", timestamp) or not signatures or len(signatures) > 2048:
        return False
    try:
        timestamp_seconds = int(timestamp)
        now = int(time.time()) if now_seconds is None else now_seconds
        if abs(now - timestamp_seconds) > tolerance_seconds:
            return False
        key = _decode_recall_base64(secret[6:])
        expected = hmac.new(key, f"{message_id}.{timestamp}.".encode(), hashlib.sha256).digest()
        for item in signatures.split():
            version, separator, encoded = item.partition(",")
            if version != "v1" or not separator:
                continue
            try:
                if hmac.compare_digest(_decode_recall_base64(encoded), expected):
                    return True
            except (ValueError, TypeError):
                continue
    except (ValueError, TypeError):
        return False
    return False


def _bounded_png(buffer: Any) -> str | None:
    if not isinstance(buffer, str) or not buffer or len(buffer) > ((MAX_FRAME_BYTES + 2) // 3) * 4:
        return None
    try:
        raw = base64.b64decode(buffer, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) < 33 or len(raw) > MAX_FRAME_BYTES or base64.b64encode(raw).decode("ascii") != buffer:
        return None
    if raw[:8] != _PNG_SIGNATURE or raw[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", raw[16:24])
    if not width or not height or width > 1280 or height > 1280 or width * height > 1_000_000:
        return None
    return buffer


def parse_screenshare_frame(payload: Any) -> dict[str, Any] | None:
    """Extract only bot ownership and screen PNG; discard all other provider data."""
    if not isinstance(payload, dict) or payload.get("event") != "video_separate_png.data":
        return None
    envelope = payload.get("data")
    event_data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(event_data, dict) or event_data.get("type") != "screenshare":
        return None
    buffer = _bounded_png(event_data.get("buffer"))
    bot = envelope.get("bot") if isinstance(envelope, dict) else None
    metadata = bot.get("metadata") if isinstance(bot, dict) else None
    if not buffer or not isinstance(bot, dict) or not isinstance(metadata, dict):
        return None
    provider_bot_id = bot.get("id")
    meeting_id = metadata.get("chusky_meeting_id")
    user_id_raw = metadata.get("chusky_user_id")
    if not isinstance(provider_bot_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", provider_bot_id):
        return None
    if not isinstance(meeting_id, str) or not re.fullmatch(r"mtg_[A-Za-z0-9_-]{1,80}", meeting_id):
        return None
    if isinstance(user_id_raw, bool) or not (isinstance(user_id_raw, int) or isinstance(user_id_raw, str) and re.fullmatch(r"[1-9]\d{0,14}", user_id_raw)):
        return None
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        return None
    if user_id <= 0 or user_id > 9_007_199_254_740_991:
        return None
    return {"provider_bot_id": provider_bot_id, "meeting_id": meeting_id, "user_id": user_id, "buffer": buffer}


class RecallScreenShareSampler:
    """Sample changes plus periodic refreshes so static slides stay available."""

    def __init__(self, min_interval_seconds: float = 2.5, refresh_interval_seconds: float = 8.0) -> None:
        self.min_interval_seconds = max(1.0, min(float(min_interval_seconds), 30.0))
        self.refresh_interval_seconds = max(self.min_interval_seconds, min(float(refresh_interval_seconds), 30.0))
        self.last_digest: str | None = None
        self.last_sent_at = float("-inf")

    def accept(self, base64_png: str, *, now: float | None = None) -> bool:
        if _bounded_png(base64_png) is None:
            return False
        current = time.monotonic() if now is None else now
        digest = hashlib.sha256(base64.b64decode(base64_png)).hexdigest()
        since_last = current - self.last_sent_at
        if since_last < self.min_interval_seconds:
            return False
        if digest == self.last_digest and since_last < self.refresh_interval_seconds:
            return False
        self.last_digest = digest
        self.last_sent_at = current
        return True

    def retry(self) -> None:
        """Allow the same frame to be retried after a transient handoff failure."""
        self.last_digest = None
