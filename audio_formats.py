"""Codec and sample-rate conversions at the Twilio/Deepgram boundary."""
from __future__ import annotations

import audioop
from urllib.parse import urlencode


TWILIO_SAMPLE_RATE = 8_000
DEEPGRAM_INPUT_SAMPLE_RATE = 48_000
DEEPGRAM_OUTPUT_SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


def twilio_mulaw_to_deepgram_linear16(
    frame: bytes,
    state: object | None = None,
) -> tuple[bytes, object | None]:
    """Decode Twilio's 8 kHz μ-law frame and resample it to Flux's 48 kHz PCM."""
    if not frame:
        return b"", state
    pcm = audioop.ulaw2lin(frame, SAMPLE_WIDTH_BYTES)
    return audioop.ratecv(
        pcm,
        SAMPLE_WIDTH_BYTES,
        CHANNELS,
        TWILIO_SAMPLE_RATE,
        DEEPGRAM_INPUT_SAMPLE_RATE,
        state,
    )


def deepgram_linear16_to_twilio_mulaw(
    frame: bytes,
    state: object | None = None,
) -> tuple[bytes, object | None]:
    """Resample Flux's 24 kHz PCM and encode it for Twilio's 8 kHz μ-law stream."""
    if len(frame) % SAMPLE_WIDTH_BYTES:
        raise ValueError("Deepgram linear16 audio must contain complete 16-bit samples")
    if not frame:
        return b"", state
    pcm, next_state = audioop.ratecv(
        frame,
        SAMPLE_WIDTH_BYTES,
        CHANNELS,
        DEEPGRAM_OUTPUT_SAMPLE_RATE,
        TWILIO_SAMPLE_RATE,
        state,
    )
    return audioop.lin2ulaw(pcm, SAMPLE_WIDTH_BYTES), next_state


def deepgram_flux_listen_url(
    model: str,
    eager_eot_threshold: float,
    eot_threshold: float,
    eot_timeout_ms: int,
) -> str:
    query = urlencode(
        {
            "model": model,
            "encoding": "linear16",
            "sample_rate": DEEPGRAM_INPUT_SAMPLE_RATE,
            "eager_eot_threshold": eager_eot_threshold,
            "eot_threshold": eot_threshold,
            "eot_timeout_ms": eot_timeout_ms,
        }
    )
    return f"wss://api.deepgram.com/v2/listen?{query}"


def deepgram_flux_speak_url(model: str) -> str:
    query = urlencode(
        {
            "model": model,
            "encoding": "linear16",
            "sample_rate": DEEPGRAM_OUTPUT_SAMPLE_RATE,
        }
    )
    return f"wss://api.deepgram.com/v2/speak?{query}"


# Keep the established Twilio names as compatibility wrappers. Both transports
# use the same documented Flux PCM contract (48 kHz input, 24 kHz output).
def twilio_deepgram_listen_url(model: str, eager_eot_threshold: float, eot_threshold: float, eot_timeout_ms: int) -> str:
    return deepgram_flux_listen_url(model, eager_eot_threshold, eot_threshold, eot_timeout_ms)


def twilio_deepgram_speak_url(model: str) -> str:
    return deepgram_flux_speak_url(model)
