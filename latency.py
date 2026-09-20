"""Small, dependency-free latency policies used by the Twilio call loop."""
from __future__ import annotations

import asyncio
from math import ceil, isfinite
from typing import TypeVar

T = TypeVar("T")


_TURN_FALLBACKS = (
    "I’m taking a moment to get that right. Give me just a second.",
    "I’m checking that carefully now. One moment, please.",
    "I want to make sure I answer that properly. Give me a moment.",
)


def turn_start_deadline_exceeded(
    started_at: float,
    now: float,
    budget_ms: int,
    first_audio_started: bool,
    interrupted: bool,
) -> bool:
    """Return whether a response is still silent past its start budget.

    The budget is deliberately a *time-to-first-audio* guard, not a maximum
    answer duration. Once audio has started, the model may finish naturally.
    Invalid clock values fail closed so a diagnostic condition cannot create a
    surprise interruption in a live call.
    """
    if first_audio_started or interrupted:
        return False
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(float(value)) for value in (started_at, now)):
        return False
    bounded_budget_ms = max(1_000, min(int(budget_ms), 60_000))
    return float(now) - float(started_at) >= bounded_budget_ms / 1000


def turn_fallback_text(turn_number: int = 0) -> str:
    """Select a brief, human-sounding recovery line without exposing errors."""
    try:
        index = max(0, int(turn_number)) % len(_TURN_FALLBACKS)
    except (TypeError, ValueError, OverflowError):
        index = 0
    return _TURN_FALLBACKS[index]


def latency_summary(samples: list[int]) -> dict[str, int | None]:
    """Summarize a bounded set of integer millisecond samples."""
    if not samples:
        return {"count": 0, "average": None, "p50": None, "p95": None}

    ordered = sorted(samples)

    def percentile(fraction: float) -> int:
        return ordered[max(0, ceil(len(ordered) * fraction) - 1)]

    return {
        "count": len(ordered),
        "average": round(sum(ordered) / len(ordered)),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
    }


async def resolve_speculative_draft(
    task: asyncio.Task[T] | None,
    *,
    transcript_matches: bool,
    grace_ms: int | None = 100,
) -> T | None:
    """Reuse a matching draft, optionally waiting for its streamed result.

    ``None`` waits for completion; use this when the draft has already been
    streamed to the caller and restarting inference would only add latency.
    A numeric grace period preserves the older behavior for non-streaming
    speculative drafts.
    """
    if task is None or not transcript_matches:
        return None

    if not task.done() and grace_ms is None:
        await asyncio.gather(task, return_exceptions=True)
    elif not task.done() and grace_ms > 0:
        await asyncio.wait({task}, timeout=grace_ms / 1000)

    if not task.done() and grace_ms is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return None

    if task.cancelled():
        return None

    try:
        return task.result()
    except Exception:
        # A failed speculative request must not suppress the definitive stream.
        return None


def take_tts_chunk(
    buffer: str,
    *,
    soft_limit: int = 24,
    hard_limit: int = 80,
) -> tuple[str, str] | None:
    """Split buffered model text on a word boundary without losing content."""
    if len(buffer) < soft_limit:
        return None

    soft_boundary = buffer.rfind(" ", 0, soft_limit + 1)
    if soft_boundary >= soft_limit // 2:
        return buffer[:soft_boundary], buffer[soft_boundary:]

    if len(buffer) < hard_limit:
        return None

    hard_boundary = buffer.rfind(" ", 0, hard_limit + 1)
    split_at = hard_boundary if hard_boundary > 0 else hard_limit
    return buffer[:split_at], buffer[split_at:]
