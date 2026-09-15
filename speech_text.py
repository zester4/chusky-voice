"""Speech-safe text normalization shared by live call and meeting paths."""
from __future__ import annotations

import re


def normalize_voice_text(value: str) -> str:
    """Convert complete model text into natural speech for Deepgram TTS."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```[^\n]*\n?[\s\S]*?```", " I can share the technical details in writing. ", text)
    text = re.sub(r"!\[([^\]]*)\]\((https?://[^\s)]+)\)", r"\1", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1", text, flags=re.I)
    text = re.sub(r"https?://[^\s)]+", " link ", text, flags=re.I)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s*([-*_])(?:\s*\1){2,}\s*$", "", text, flags=re.M)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+)__", r"\1", text)
    text = re.sub(r"~~([^~\n]+)~~", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", text)
    text = text.replace("*", "").replace("#", "")

    def spoken_dollars(match: re.Match[str]) -> str:
        dollars = int(match.group(1))
        cents_raw = match.group(2)
        parts = [f"{dollars} dollar{'s' if dollars != 1 else ''}"]
        if cents_raw is not None:
            cents = int(cents_raw.ljust(2, "0"))
            if cents:
                parts.append(f"and {cents} cent{'s' if cents != 1 else ''}")
        return " ".join(parts)

    text = re.sub(r"\$(\d{1,7})(?:\.(\d{1,2}))?", spoken_dollars, text)
    text = re.sub(r"([.!?;:])(?=[A-Za-z])", r"\1 ", text)
    text = re.sub(r"([a-z])(?=\d)", r"\1 ", text)
    text = re.sub(r"(\d)(?=[A-Za-z])", r"\1 ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def normalize_voice_delta(value: str) -> str:
    """Keep streamed token whitespace intact until a full phrase is ready."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or "").replace("\r\n", "\n").replace("\r", "\n"))
