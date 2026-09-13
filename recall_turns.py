"""Privacy-conscious turn gating for interactive Recall meeting audio."""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Any, Literal


ContextRole = Literal["participant", "chusky"]
MeetingMode = Literal["addressed", "copilot", "representative"]


def default_meeting_greeting(mode: str) -> str:
    if mode == "addressed":
        return "Hi everyone, I’m Chusky, the AI meeting assistant. Say ‘Chusky’ when you’d like me to respond."
    return "Hi everyone, I’m Chusky, the AI meeting copilot. I’ll follow the conversation and speak up when I can add something useful; you can address me directly anytime."


def parse_meeting_media_authorization(value: Any, fallback_mode: str) -> tuple[MeetingMode, str]:
    """Validate the owner-scoped meeting mode and spoken intro from Chusky's authenticated API."""
    if fallback_mode not in ("addressed", "copilot", "representative"):
        fallback_mode = "addressed"
    if value is None:
        return fallback_mode, default_meeting_greeting(fallback_mode)
    if not isinstance(value, dict) or set(value) != {"interactionMode", "greeting"}:
        raise ValueError("invalid meeting media authorization response")
    mode = value.get("interactionMode")
    greeting = value.get("greeting")
    if mode not in ("addressed", "copilot", "representative"):
        raise ValueError("invalid meeting interaction mode from authorization service")
    if not isinstance(greeting, str):
        raise ValueError("invalid meeting greeting from authorization service")
    greeting = re.sub(r"\s+", " ", greeting).strip()
    if not greeting or len(greeting) > 500 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", greeting):
        raise ValueError("invalid meeting greeting from authorization service")
    return mode, greeting


class CopilotTurnGate:
    """Rate- and count-limit proactive model evaluations; explicit calls bypass the copilot cap."""

    def __init__(self, min_interval_seconds: int = 8, max_evaluations: int = 120) -> None:
        self.min_interval_seconds = max(5, min(int(min_interval_seconds), 120))
        self.max_evaluations = max(1, min(int(max_evaluations), 1_000))
        self.last_evaluation_at = float("-inf")
        self.evaluations = 0

    def should_evaluate(self, invoked: bool, now: float | None = None) -> tuple[bool, bool]:
        if invoked:
            return True, False
        timestamp = time.monotonic() if now is None else float(now)
        if self.evaluations >= self.max_evaluations:
            return False, True
        if timestamp - self.last_evaluation_at < self.min_interval_seconds:
            return False, False
        self.last_evaluation_at = timestamp
        self.evaluations += 1
        return True, False


class MeetingContextWindow:
    """Small in-process transcript window; never writes ambient speech to storage."""

    def __init__(self, max_turns: int = 12, max_chars: int = 6_000, ttl_seconds: int = 300) -> None:
        self.max_turns = max(1, min(int(max_turns), 32))
        self.max_chars = max(256, min(int(max_chars), 12_000))
        self.ttl_seconds = max(30, min(int(ttl_seconds), 900))
        self._items: deque[tuple[float, ContextRole, str]] = deque()

    def _prune(self, now: float) -> None:
        while self._items and now - self._items[0][0] > self.ttl_seconds:
            self._items.popleft()
        while len(self._items) > self.max_turns or sum(len(item[2]) for item in self._items) > self.max_chars:
            self._items.popleft()

    def add(self, role: ContextRole, text: str, now: float | None = None) -> None:
        if role not in ("participant", "chusky") or not isinstance(text, str):
            return
        cleaned = re.sub(r"\s+", " ", text).strip()[:1_000]
        if not cleaned:
            return
        timestamp = time.monotonic() if now is None else float(now)
        self._prune(timestamp)
        self._items.append((timestamp, role, cleaned))
        self._prune(timestamp)

    def snapshot(self, now: float | None = None) -> list[dict[str, str]]:
        timestamp = time.monotonic() if now is None else float(now)
        self._prune(timestamp)
        return [{"role": role, "text": text} for _, role, text in self._items]


def is_recall_invocation(transcript: str, wake_word: str = "Chusky") -> bool:
    """Only send a meeting utterance to Chusky when it explicitly says the wake word."""
    if not transcript or not wake_word or len(transcript) > 5_000:
        return False
    return re.search(rf"(?<![\w'’]){re.escape(wake_word)}(?![\w'’])", transcript, re.IGNORECASE) is not None
