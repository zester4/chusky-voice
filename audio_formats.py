"""Codec and sample-rate conversions at the Twilio/Deepgram boundary."""
from __future__ import annotations

try:
    import audioop
except ModuleNotFoundError:  # Python 3.13+; native μ-law needs no codec module.
    audioop = None
from urllib.parse import urlencode


TWILIO_SAMPLE_RATE = 8_000
DEEPGRAM_INPUT_SAMPLE_RATE = 48_000
DEEPGRAM_OUTPUT_SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
AUDIOOP_AVAILABLE = audioop is not None


def _legacy_audioop():
    if audioop is None:
        raise RuntimeError(
            "VOICE_TWILIO_NATIVE_MULAW=false requires the optional audioop-lts package; "
            "native μ-law mode does not require it"
        )
    return audioop


def twilio_mulaw_to_deepgram_linear16(
    frame: bytes,
    state: object | None = None,
) -> tuple[bytes, object | None]:
    """Decode Twilio's 8 kHz μ-law frame and resample it to Flux's 48 kHz PCM."""
    if not frame:
        return b"", state
    codec = _legacy_audioop()
    pcm = codec.ulaw2lin(frame, SAMPLE_WIDTH_BYTES)
    return codec.ratecv(
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
    codec = _legacy_audioop()
    pcm, next_state = codec.ratecv(
        frame,
        SAMPLE_WIDTH_BYTES,
        CHANNELS,
        DEEPGRAM_OUTPUT_SAMPLE_RATE,
        TWILIO_SAMPLE_RATE,
        state,
    )
    return codec.lin2ulaw(pcm, SAMPLE_WIDTH_BYTES), next_state


def deepgram_flux_listen_url(
    model: str,
    eager_eot_threshold: float,
    eot_threshold: float,
    eot_timeout_ms: int,
    *,
    language_hints: list[str] | None = None,
) -> str:
    query_values: dict[str, object] = {
        "model": model,
        "encoding": "linear16",
        "sample_rate": DEEPGRAM_INPUT_SAMPLE_RATE,
        "eager_eot_threshold": eager_eot_threshold,
        "eot_threshold": eot_threshold,
        "eot_timeout_ms": eot_timeout_ms,
    }
    if model == "flux-general-multi":
        hints = [hint.strip() for hint in (language_hints or []) if isinstance(hint, str) and hint.strip()]
        # Flux Multilingual auto-detects when no hints are supplied. When
        # hints exist they bias the expected languages without disabling the
        # provider's dynamic language detection.
        if hints:
            query_values["language_hint"] = hints
    query = urlencode(query_values, doseq=True)
    return f"wss://api.deepgram.com/v2/listen?{query}"


def deepgram_nova_listen_url(
    model: str,
    endpointing_ms: int,
    utterance_end_ms: int,
) -> str:
    """Build the documented Nova live-STT contract for Recall meetings."""
    query = urlencode(
        {
            "model": model,
            "encoding": "linear16",
            "sample_rate": DEEPGRAM_INPUT_SAMPLE_RATE,
            "channels": CHANNELS,
            "interim_results": "true",
            "smart_format": "true",
            "punctuate": "true",
            "vad_events": "true",
            "endpointing": endpointing_ms,
            "utterance_end_ms": utterance_end_ms,
        }
    )
    return f"wss://api.deepgram.com/v1/listen?{query}"


def deepgram_flux_speak_url(model: str) -> str:
    query = urlencode(
        {
            "model": model,
            "encoding": "linear16",
            "sample_rate": DEEPGRAM_OUTPUT_SAMPLE_RATE,
        }
    )
    return f"wss://api.deepgram.com/v2/speak?{query}"


def twilio_deepgram_listen_url(
    model: str,
    eager_eot_threshold: float,
    eot_threshold: float,
    eot_timeout_ms: int,
    *,
    native_mulaw: bool = True,
) -> str:
    """Build the Flux STT URL for a Twilio media stream.

    Twilio Media Streams are already raw 8 kHz μ-law. Flux accepts that exact
    format, so the production default avoids decode and resample work on every
    20 ms frame. The linear16 route remains an explicit rollback option while
    call-quality measurements are gathered. Recall has its own PCM helpers and
    never uses this function.
    """
    if not native_mulaw:
        return deepgram_flux_listen_url(model, eager_eot_threshold, eot_threshold, eot_timeout_ms)
    query = urlencode(
        {
            "model": model,
            "encoding": "mulaw",
            "sample_rate": TWILIO_SAMPLE_RATE,
            "eager_eot_threshold": eager_eot_threshold,
            "eot_threshold": eot_threshold,
            "eot_timeout_ms": eot_timeout_ms,
        }
    )
    return f"wss://api.deepgram.com/v2/listen?{query}"


def twilio_deepgram_speak_url(model: str, *, native_mulaw: bool = True) -> str:
    """Build the Flux TTS URL for Twilio's raw 8 kHz μ-law output contract."""
    if not native_mulaw:
        return deepgram_flux_speak_url(model)
    query = urlencode(
        {
            "model": model,
            "encoding": "mulaw",
            "sample_rate": TWILIO_SAMPLE_RATE,
        }
    )
    return f"wss://api.deepgram.com/v2/speak?{query}"
