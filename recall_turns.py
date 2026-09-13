"""Privacy-conscious turn gating for interactive Recall meeting audio."""
from __future__ import annotations

import re
import time
from collections import deque
from difflib import SequenceMatcher
from typing import Any, Literal


ContextRole = Literal["participant", "chusky"]
MeetingMode = Literal["addressed", "copilot", "representative"]


def default_meeting_greeting(mode: str) -> str:
    if mode == "addressed":
        return "Hi everyone, I’m Chusky. Say my name if you’d like me to jump in."
    return "Hi everyone, I’m Chusky. I’ll follow along and join in when I can help."


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
    """Smooth bursts without timing out autonomous participation during a meeting."""

    def __init__(self, min_interval_seconds: int = 4) -> None:
        self.min_interval_seconds = max(1, min(int(min_interval_seconds), 120))
        self.last_evaluation_at = float("-inf")

    def should_evaluate(self, invoked: bool, now: float | None = None) -> bool:
        if invoked:
            return True
        timestamp = time.monotonic() if now is None else float(now)
        if timestamp - self.last_evaluation_at < self.min_interval_seconds:
            return False
        self.last_evaluation_at = timestamp
        return True


class MeetingContextWindow:
    """Small in-process transcript window; never writes ambient speech to storage."""

    def __init__(self, max_turns: int = 32, max_chars: int = 12_000, ttl_seconds: int = 1_800) -> None:
        self.max_turns = max(1, min(int(max_turns), 32))
        self.max_chars = max(256, min(int(max_chars), 12_000))
        self.ttl_seconds = max(30, min(int(ttl_seconds), 3_600))
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


class MeetingEchoGuard:
    """Drop short-lived STT echoes of Chusky's own recently streamed speech."""

    def __init__(self, ttl_seconds: int = 12, max_chunks: int = 48) -> None:
        self.ttl_seconds = max(2, min(int(ttl_seconds), 30))
        self.max_chunks = max(1, min(int(max_chunks), 64))
        self._chunks: deque[tuple[float, tuple[str, ...]]] = deque()

    @staticmethod
    def _tokens(text: str) -> tuple[str, ...]:
        if not isinstance(text, str):
            return ()
        return tuple(re.findall(r"[\w']+", text.casefold()))[:250]

    def _prune(self, now: float) -> None:
        while self._chunks and now - self._chunks[0][0] > self.ttl_seconds:
            self._chunks.popleft()
        while len(self._chunks) > self.max_chunks:
            self._chunks.popleft()

    def remember_output(self, text: str, now: float | None = None) -> None:
        tokens = self._tokens(text)
        if not tokens:
            return
        timestamp = time.monotonic() if now is None else float(now)
        self._prune(timestamp)
        self._chunks.append((timestamp, tokens))
        self._prune(timestamp)

    def is_echo(self, transcript: str, now: float | None = None) -> bool:
        tokens = self._tokens(transcript)
        if not tokens:
            return False
        timestamp = time.monotonic() if now is None else float(now)
        self._prune(timestamp)
        if not self._chunks:
            return False

        if len(tokens) < 5:
            # Short affirmations are too ambiguous to match approximately.
            if any(timestamp - spoken_at <= 2 and tokens == spoken for spoken_at, spoken in self._chunks):
                return True
            if len(tokens) < 3:
                return False

        recent_speech = tuple(token for _, chunk in self._chunks for token in chunk)[-600:]
        matched = sum(block.size for block in SequenceMatcher(None, tokens, recent_speech, autojunk=False).get_matching_blocks())
        return matched / len(tokens) >= 0.9


def is_recall_invocation(transcript: str, wake_word: str = "Chusky") -> bool:
    """Only send a meeting utterance to Chusky when it explicitly says the wake word."""
    if not transcript or not wake_word or len(transcript) > 5_000:
        return False
    return re.search(rf"(?<![\w'’]){re.escape(wake_word)}(?![\w'’])", transcript, re.IGNORECASE) is not None
