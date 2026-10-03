"""Small, validated helpers for the optional Twilio ElevenLabs TTS path.

The direct Twilio bridge owns the provider connection.  This module only
builds the documented multi-context messages and decodes bounded audio events;
it never logs or returns provider credentials.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from urllib.parse import urlencode


ELEVENLABS_TTS_URL = "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/multi-stream-input"
VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_AUDIO_BYTES = 2_000_000


class ElevenLabsConfigurationError(RuntimeError):
    """Raised when the optional Twilio ElevenLabs configuration is incomplete."""


@dataclass(frozen=True)
class ElevenLabsAudioEvent:
    audio: bytes | None
    is_final: bool


def validate_configuration(api_key: str, voice_id: str, model_id: str) -> None:
    if not api_key.strip():
        raise ElevenLabsConfigurationError("ELEVENLABS_API_KEY is required when VOICE_ELEVENLABS_ENABLED=true")
    if not VOICE_ID_PATTERN.fullmatch(voice_id):
        raise ElevenLabsConfigurationError("ELEVENLABS_VOICE_ID is invalid")
    if not MODEL_ID_PATTERN.fullmatch(model_id):
        raise ElevenLabsConfigurationError("ELEVENLABS_MODEL_ID is invalid")


def stream_url(voice_id: str, model_id: str, output_format: str = "ulaw_8000") -> str:
    validate_configuration("configured", voice_id, model_id)
    if output_format != "ulaw_8000":
        raise ElevenLabsConfigurationError("ElevenLabs Twilio output must be ulaw_8000")
    return ELEVENLABS_TTS_URL.format(voice_id=voice_id) + "?" + urlencode({
        "model_id": model_id,
        "output_format": output_format,
    })


def start_context(context_id: str) -> dict[str, str]:
    return {"context_id": context_id, "text": " "}


def speak(context_id: str, text: str) -> dict[str, str]:
    return {"context_id": context_id, "text": text}


def flush(context_id: str) -> dict[str, object]:
    return {"context_id": context_id, "flush": True}


def close_context(context_id: str) -> dict[str, object]:
    return {"context_id": context_id, "close_context": True}


def parse_audio_event(raw: str) -> ElevenLabsAudioEvent | None:
    """Parse one provider event without accepting unbounded binary payloads."""
    try:
        import json

        event = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    audio_value = event.get("audio")
    audio: bytes | None = None
    if isinstance(audio_value, str) and audio_value:
        try:
            audio = base64.b64decode(audio_value, validate=True)
        except (binascii.Error, ValueError):
            return None
        if len(audio) > MAX_AUDIO_BYTES:
            return None
    is_final = event.get("is_final") is True or event.get("isFinal") is True
    return ElevenLabsAudioEvent(audio=audio, is_final=is_final)
