"""Privacy-conscious turn gating for interactive Recall meeting audio."""
from __future__ import annotations

import math
import re
import time
from collections import deque
from difflib import SequenceMatcher
from typing import Any, Literal


ContextRole = Literal["participant", "chusky"]
MeetingMode = Literal["addressed", "copilot", "representative"]


def default_meeting_greeting(mode: str) -> str:
    del mode
    return "Hi, I’m Chusky."


def parse_meeting_media_authorization(value: Any, fallback_mode: str) -> tuple[MeetingMode, str]:
    """Validate the owner-scoped meeting mode and spoken intro from Chusky's authenticated API."""
    if fallback_mode not in ("addressed", "copilot", "representative"):
        fallback_mode = "addressed"
    if value is None:
        return fallback_mode, default_meeting_greeting(fallback_mode)
    if not isinstance(value, dict) or not {"interactionMode", "greeting"}.issubset(value) or set(value) - {"interactionMode", "greeting", "ttsModel"}:
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


def parse_meeting_tts_model(value: Any, fallback_model: str) -> str:
    """Read only a safe Flux model ID from Chusky's authenticated media grant."""
    if not isinstance(fallback_model, str) or not re.fullmatch(r"flux-[a-z]+-en", fallback_model):
        raise ValueError("invalid meeting fallback TTS model")
    if not isinstance(value, dict) or "ttsModel" not in value:
        return fallback_model
    model = value.get("ttsModel")
    if not isinstance(model, str) or not re.fullmatch(r"flux-[a-z]+-en", model):
        raise ValueError("invalid meeting TTS model from authorization service")
    return model


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
        self._items: deque[tuple[int, float, ContextRole, str, str | None]] = deque()
        self._next_turn_id = 0

    def _prune(self, now: float) -> None:
        while self._items and now - self._items[0][1] > self.ttl_seconds:
            self._items.popleft()
        while len(self._items) > self.max_turns or sum(len(item[3]) for item in self._items) > self.max_chars:
            self._items.popleft()

    def add(self, role: ContextRole, text: str, now: float | None = None) -> int | None:
        if role not in ("participant", "chusky") or not isinstance(text, str):
            return None
        cleaned = re.sub(r"\s+", " ", text).strip()[:1_000]
        if not cleaned:
            return None
        timestamp = time.monotonic() if now is None else float(now)
        self._prune(timestamp)
        self._next_turn_id += 1
        turn_id = self._next_turn_id
        self._items.append((turn_id, timestamp, role, cleaned, None))
        self._prune(timestamp)
        return turn_id

    def set_speaker(self, turn_id: int | None, speaker_name: str | None) -> None:
        if turn_id is None:
            return
        cleaned = re.sub(r"\s+", " ", speaker_name).strip()[:160] if isinstance(speaker_name, str) else ""
        cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", cleaned).strip()
        self._items = deque(
            (item_id, timestamp, role, text, cleaned or None) if item_id == turn_id else (item_id, timestamp, role, text, name)
            for item_id, timestamp, role, text, name in self._items
        )

    def snapshot(self, now: float | None = None) -> list[dict[str, str]]:
        timestamp = time.monotonic() if now is None else float(now)
        self._prune(timestamp)
        return [
            {"role": role, "text": text, **({"speakerName": name} if name else {})}
            for _, _, role, text, name in self._items
        ]


def flux_turn_time_bounds_ms(stream_started_at: float | None, event: dict[str, Any]) -> tuple[int, int] | None:
    """Map Deepgram's stream-relative word times to UTC milliseconds for Recall correlation."""
    if not isinstance(stream_started_at, (int, float)) or isinstance(stream_started_at, bool) or not math.isfinite(stream_started_at) or stream_started_at <= 0:
        return None

    starts: list[float] = []
    ends: list[float] = []
    words = event.get("words")
    if isinstance(words, list):
        for word in words:
            if not isinstance(word, dict):
                continue
            start, end = word.get("start"), word.get("end")
            if (
                isinstance(start, (int, float)) and not isinstance(start, bool)
                and isinstance(end, (int, float)) and not isinstance(end, bool)
                and math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= 120
            ):
                starts.append(float(start))
                ends.append(float(end))

    if not starts:
        start, end = event.get("audio_window_start"), event.get("audio_window_end")
        if not (
            isinstance(start, (int, float)) and not isinstance(start, bool)
            and isinstance(end, (int, float)) and not isinstance(end, bool)
            and math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= 120
        ):
            return None
        starts, ends = [float(start)], [float(end)]

    start_ms = round(stream_started_at * 1000 + min(starts) * 1000)
    end_ms = round(stream_started_at * 1000 + max(ends) * 1000)
    if end_ms <= start_ms or end_ms - start_ms > 120_000:
        return None
    return start_ms, end_ms


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
