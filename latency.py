"""Small, dependency-free latency policies used by the Twilio call loop."""
from __future__ import annotations

import asyncio
from math import ceil
from typing import TypeVar

T = TypeVar("T")


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
    grace_ms: int = 100,
) -> T | None:
    """Reuse a matching draft only if it is ready almost immediately.

    An unfinished eager draft is canceled after a short grace period so the
    definitive turn can use the streaming response path instead of waiting for
    a complete, non-streaming model result.
    """
    if task is None or not transcript_matches:
        return None

    if not task.done() and grace_ms > 0:
        await asyncio.wait({task}, timeout=grace_ms / 1000)

    if not task.done():
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
    soft_limit: int = 48,
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
