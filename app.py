"""Chusky's server-side voice media bridge.

This service accepts Twilio bidirectional Media Streams and Recall meeting
audio, streams transient audio to Deepgram, and plays Chusky's responses back
through the provider session. It stores no raw audio or caller credentials.
Chusky may retain bounded text turns in the owner's private conversation.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
import json
import logging
import math
import os
import secrets
import time
import uuid
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.responses import HTMLResponse
from websockets.asyncio.client import connect
from audio_formats import (
    DEEPGRAM_INPUT_SAMPLE_RATE,
    DEEPGRAM_OUTPUT_SAMPLE_RATE,
    deepgram_linear16_to_twilio_mulaw,
    deepgram_flux_listen_url,
    deepgram_flux_speak_url,
    deepgram_nova_listen_url,
    twilio_deepgram_listen_url,
    twilio_deepgram_speak_url,
    twilio_mulaw_to_deepgram_linear16,
)
from latency import (
    latency_summary,
    resolve_speculative_draft,
    take_tts_chunk,
    turn_fallback_text,
    turn_start_deadline_exceeded,
)
from recall_auth import valid_recall_ticket, wait_for_media_authorization
from recall_video import RecallScreenShareSampler, parse_screenshare_frame, valid_visual_handoff_url, verify_recall_websocket_signature, visual_configuration_issue, visual_configuration_status
from recall_turns import CopilotTurnGate, MeetingContextWindow, MeetingEchoGuard, MeetingMode, build_meeting_outcome_payload, default_meeting_greeting, flux_turn_time_bounds_ms, is_recall_invocation, parse_meeting_language_authorization, parse_meeting_media_authorization, parse_meeting_tts_model
from speech_text import normalize_voice_delta, normalize_voice_text
from twilio_auth import valid_twilio_ticket, valid_twilio_websocket

LOG = logging.getLogger("chusky.voice_bridge")
logging.basicConfig(level=os.getenv("VOICE_BRIDGE_LOG_LEVEL", "INFO"))
load_dotenv(Path(__file__).with_name(".env"))
@dataclass(frozen=True)
class Settings:
    bridge_secret: str
    deepgram_api_key: str
    chusky_turn_url: str
    chusky_status_url: str
    max_call_seconds: int
    max_active_calls: int
    twilio_auth_token: str
    twilio_media_stream_url: str
    stt_model: str
    stt_eager_eot_threshold: float
    stt_eot_threshold: float
    stt_eot_timeout_ms: int
    tts_model: str
    twilio_native_mulaw: bool
    barge_in_min_chars: int
    greeting: str
    turn_start_budget_ms: int
    turn_fallback_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.getenv("TWILIO_MEDIA_BRIDGE_SECRET", "").strip()
        deepgram = os.getenv("DEEPGRAM_API_KEY", "").strip()
        turn_url = os.getenv("CHUSKY_VOICE_TURN_URL", "http://127.0.0.1:3003/internal/twilio/turn").strip()
        status_url = os.getenv("CHUSKY_VOICE_STATUS_URL", "http://127.0.0.1:3003/internal/twilio/status").strip()
        if not secret or not deepgram or not turn_url.startswith(("http://", "https://")) or not turn_url.endswith("/turn") or not status_url.startswith(("http://", "https://")):
            raise RuntimeError("TWILIO_MEDIA_BRIDGE_SECRET, DEEPGRAM_API_KEY, CHUSKY_VOICE_TURN_URL ending in /turn, and CHUSKY_VOICE_STATUS_URL are required")
        return cls(
            secret, deepgram, turn_url, status_url,
            max(60, min(int(os.getenv("VOICE_BRIDGE_MAX_CALL_SECONDS", "7200")), 14_400)),
            max(1, min(int(os.getenv("VOICE_BRIDGE_MAX_ACTIVE_CALLS", "4")), 20)),
            os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            os.getenv("TWILIO_MEDIA_STREAM_URL", "").strip().rstrip("/"),
            os.getenv("VOICE_STT_MODEL", "flux-general-en").strip(),
            max(0.3, min(float(os.getenv("VOICE_STT_EAGER_EOT_THRESHOLD", "0.45")), 0.9)),
            max(0.5, min(float(os.getenv("VOICE_STT_EOT_THRESHOLD", "0.65")), 0.9)),
            # Flux's turn events do the semantic work; this is the final
            # silence window. Keep the default responsive for live calls.
            max(500, min(int(os.getenv("VOICE_STT_EOT_TIMEOUT_MS", "800")), 60_000)),
            os.getenv("VOICE_TTS_MODEL", "flux-haley-en").strip(),
            os.getenv("VOICE_TWILIO_NATIVE_MULAW", "true").strip().lower() != "false",
            max(1, min(int(os.getenv("VOICE_BARGE_IN_MIN_CHARS", "2")), 100)),
            os.getenv("VOICE_GREETING", "Hi, this is Chusky. How can I help?").strip()[:500],
            max(4_000, min(int(os.getenv("VOICE_TURN_START_BUDGET_MS", "10000")), 20_000)),
            os.getenv("VOICE_TURN_FALLBACK_ENABLED", "true").strip().lower() != "false",
        )


@dataclass(frozen=True)
class RecallSettings:
    bridge_secret: str
    deepgram_api_key: str
    turn_stream_url: str
    commit_turn_url: str
    media_authorize_url: str
    visual_frame_url: str
    realtime_secret: str
    stt_model: str
    stt_eager_eot_threshold: float
    stt_eot_threshold: float
    stt_eot_timeout_ms: int
    nova_endpointing_ms: int
    nova_utterance_end_ms: int
    tts_model: str
    max_meeting_seconds: int
    max_active_meetings: int
    copilot_min_interval_seconds: int
    turn_start_budget_ms: int
    turn_fallback_enabled: bool

    @classmethod
    def from_env(cls) -> "RecallSettings":
        if os.getenv("RECALL_MEETINGS_ENABLED", "false").strip().lower() != "true":
            raise RecallConfigurationError("meetings_disabled", ("RECALL_MEETINGS_ENABLED",))
        secret = os.getenv("RECALL_MEDIA_BRIDGE_SECRET", "").strip()
        deepgram = os.getenv("DEEPGRAM_API_KEY", "").strip()
        turn_url = os.getenv("CHUSKY_RECALL_TURN_STREAM_URL", "").strip()
        commit_url = os.getenv("CHUSKY_RECALL_COMMIT_TURN_URL", "").strip()
        authorize_url = os.getenv("CHUSKY_RECALL_MEDIA_AUTHORIZE_URL", "").strip()
        visual_frame_url = os.getenv("CHUSKY_RECALL_VISUAL_FRAME_URL", "").strip()
        # Prefer Recall's official setting name while keeping the historical
        # Chusky name as a backwards-compatible fallback.
        realtime_secret = os.getenv("RECALL_WORKSPACE_VERIFICATION_SECRET", "").strip() or os.getenv("RECALL_REALTIME_SECRET", "").strip()
        invalid_fields = []
        if len(secret.encode("utf-8")) < 32:
            invalid_fields.append("RECALL_MEDIA_BRIDGE_SECRET")
        if not deepgram:
            invalid_fields.append("DEEPGRAM_API_KEY")
        for field_name, url in (
            ("CHUSKY_RECALL_TURN_STREAM_URL", turn_url),
            ("CHUSKY_RECALL_COMMIT_TURN_URL", commit_url),
            ("CHUSKY_RECALL_MEDIA_AUTHORIZE_URL", authorize_url),
        ):
            if not url.startswith("https://"):
                invalid_fields.append(field_name)
        if invalid_fields:
            raise RecallConfigurationError("required_settings_invalid", tuple(invalid_fields))
        # Meeting STT is independently configurable from Twilio. Multilingual
        # meetings select Flux multi from the authenticated meeting profile.
        stt = os.getenv("RECALL_STT_MODEL", "nova-3").strip()
        tts = os.getenv("VOICE_TTS_MODEL", "flux-haley-en").strip()
        if stt not in {"nova-3", "flux-general-en", "flux-general-multi"} or not tts.startswith("flux-"):
            raise RecallConfigurationError("meeting_models_invalid", ("RECALL_STT_MODEL", "VOICE_TTS_MODEL"))
        return cls(
            secret, deepgram, turn_url, commit_url, authorize_url, visual_frame_url, realtime_secret, stt,
            max(0.3, min(float(os.getenv("VOICE_STT_EAGER_EOT_THRESHOLD", "0.45")), 0.9)),
            max(0.5, min(float(os.getenv("VOICE_STT_EOT_THRESHOLD", "0.65")), 0.9)),
            max(500, min(int(os.getenv("VOICE_STT_EOT_TIMEOUT_MS", "800")), 60_000)),
            max(100, min(int(os.getenv("RECALL_NOVA_ENDPOINTING_MS", "500")), 2_000)),
            max(1_000, min(int(os.getenv("RECALL_NOVA_UTTERANCE_END_MS", "1000")), 5_000)),
            tts,
            max(60, min(int(os.getenv("RECALL_MAX_MEETING_SECONDS", "7200")), 14_400)),
            max(1, min(int(os.getenv("RECALL_MAX_ACTIVE_MEETINGS", "4")), 20)),
            max(1, min(int(os.getenv("RECALL_COPILOT_MIN_INTERVAL_SECONDS", "4")), 120)),
            max(4_000, min(int(os.getenv("RECALL_TURN_START_BUDGET_MS", os.getenv("VOICE_TURN_START_BUDGET_MS", "10000"))), 20_000)),
            os.getenv("RECALL_TURN_FALLBACK_ENABLED", "true").strip().lower() != "false",
        )


class RecallConfigurationError(RuntimeError):
    """Safe configuration issue code and field names; never retain setting values."""

    def __init__(self, code: str, fields: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.fields = fields


class RecallMeetingAgentError(RuntimeError):
    """A meeting-turn failure carrying only a validated diagnostic code."""

    ALLOWED_CODES = frozenset({"approval_required", "agent_run_failed"})

    def __init__(self, code: Any) -> None:
        self.failure_code = code if isinstance(code, str) and code in self.ALLOWED_CODES else "agent_run_failed"
        super().__init__("Chusky meeting agent stream failed")


@dataclass(frozen=True)
class VoiceTurnResult:
    text: str
    cost: float


class TwilioVoiceCall:
    """Twilio bidirectional Media Stream transport.

    Twilio sends and accepts base64 `audio/x-mulaw` at 8 kHz. Flux receives
    and emits that native format by default, eliminating per-frame codec and
    resample work. The legacy PCM route remains an explicit configuration
    rollback; Chusky remains the sole agent/LLM runtime.
    """
    def __init__(self, call_id: str, user_id: int, stream_sid: str, websocket: WebSocket, settings: Settings, metrics: "BridgeMetrics", tts_model: str | None = None) -> None:
        self.call_id, self.user_id, self.stream_sid = call_id, user_id, stream_sid
        self.websocket, self.settings = websocket, settings
        if tts_model is not None and not re.fullmatch(r"flux-[a-z]+-en", tts_model):
            raise ValueError("Twilio TTS voice is invalid")
        self.tts_model = tts_model or settings.tts_model
        self.metrics = metrics
        # Keep one client alive for the call so successive turns reuse the
        # authenticated connection to Chusky rather than paying a new setup cost.
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))
        self.stop = asyncio.Event()
        # Twilio typically sends 20 ms frames. Keep at most one second of
        # audio so a transient STT slowdown drops stale speech instead of
        # creating a multi-second conversational lag.
        self.audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self.response_task: asyncio.Task[None] | None = None
        self.draft_task: asyncio.Task[VoiceTurnResult | None] | None = None
        self.draft_transcript = ""
        self.draft_turn_index: int | None = None
        self.finalized_turn_indexes: set[int] = set()
        self.eager_end_times: dict[int, float] = {}
        self.response_started_at = 0.0
        self.eager_response_started_at = 0.0
        self.tts_socket: Any | None = None
        self.tts_lock = asyncio.Lock()
        self.tts_reader_task: asyncio.Task[None] | None = None
        self.tts_done_event: asyncio.Event | None = None
        self.tts_first_audio_recorded = False
        self.turn_first_audio_event: asyncio.Event | None = None
        self.stt_resample_state: object | None = None
        self.tts_resample_state: object | None = None
        self.twilio_send_lock = asyncio.Lock()
        self.interrupted = False

    async def run(self) -> None:
        try:
            await self._notify_status("active")
            await asyncio.wait_for(self._run(), timeout=self.settings.max_call_seconds)
            await self._notify_status("ended")
            self.metrics.twilio_completed += 1
        except asyncio.TimeoutError:
            await self._notify_status("ended")
            self.metrics.twilio_completed += 1
        except WebSocketDisconnect:
            await self._notify_status("ended")
            self.metrics.twilio_completed += 1
        except Exception:
            LOG.exception("Twilio voice call ended with an error", extra={"call_id": self.call_id})
            await self._notify_status("failed", "Twilio media stream processing failed")
            self.metrics.twilio_failed += 1
        finally:
            self.stop.set()
            if self.response_task and not self.response_task.done():
                self.response_task.cancel()
                await asyncio.gather(self.response_task, return_exceptions=True)
            if self.draft_task and not self.draft_task.done():
                self.draft_task.cancel()
                await asyncio.gather(self.draft_task, return_exceptions=True)
            await self._close_persistent_tts()
            await self.http.aclose()
            try:
                await self.websocket.close()
            except Exception:
                pass

    async def _run(self) -> None:
        if not self.settings.stt_model.startswith("flux-"):
            raise RuntimeError("VOICE_STT_MODEL must be a Deepgram Flux conversational model, for example flux-general-en")
        url = twilio_deepgram_listen_url(
            self.settings.stt_model,
            self.settings.stt_eager_eot_threshold,
            self.settings.stt_eot_threshold,
            self.settings.stt_eot_timeout_ms,
            native_mulaw=getattr(self.settings, "twilio_native_mulaw", True),
        )
        async with connect(url, additional_headers={"Authorization": f"Token {self.settings.deepgram_api_key}"}, max_size=1_000_000) as socket:
            inbound = asyncio.create_task(self._receive_twilio(), name=f"twilio-in-{self.call_id}")
            sender = asyncio.create_task(self._send_audio(socket), name=f"twilio-stt-{self.call_id}")
            transcripts = asyncio.create_task(self._receive_transcripts(socket), name=f"twilio-out-{self.call_id}")
            if self.settings.greeting:
                self.interrupted = False
                self.response_started_at = time.monotonic()
                # Open and keep the per-call TTS socket warm during the greeting
                # so the first actual answer doesn't pay for a new handshake.
                self.response_task = asyncio.create_task(self._speak_persistent_text(self.settings.greeting), name=f"twilio-greeting-{self.call_id}")
                self.response_task.add_done_callback(self._observe_response_task)
            done, pending = await asyncio.wait({inbound, sender, transcripts}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()

    async def _receive_twilio(self) -> None:
        while not self.stop.is_set():
            event = json.loads(await self.websocket.receive_text())
            if event.get("event") == "stop":
                return
            if event.get("event") != "media":
                continue
            payload = str((event.get("media") or {}).get("payload") or "")
            if not payload:
                continue
            try:
                audio = base64.b64decode(payload, validate=True)
            except Exception:
                continue
            if audio and not self.audio.full():
                self.audio.put_nowait(audio)
            elif audio:
                self.metrics.dropped_inbound_frames += 1

    async def _send_audio(self, socket: Any) -> None:
        while not self.stop.is_set():
            twilio_frame = await self.audio.get()
            if getattr(self.settings, "twilio_native_mulaw", True):
                await socket.send(twilio_frame)
                continue
            linear16, self.stt_resample_state = twilio_mulaw_to_deepgram_linear16(twilio_frame, self.stt_resample_state)
            if linear16:
                await socket.send(linear16)

    @staticmethod
    def _same_transcript(left: str, right: str) -> bool:
        normalize = lambda value: " ".join("".join(char.lower() if char.isalnum() or char.isspace() else " " for char in value).split())
        return normalize(left) == normalize(right)

    async def _receive_transcripts(self, socket: Any) -> None:
        async for raw in socket:
            if not isinstance(raw, str):
                continue
            event = json.loads(raw)
            if event.get("type") != "TurnInfo":
                continue
            turn_event = str(event.get("event") or "")
            transcript = str(event.get("transcript") or "").strip()
            try:
                turn_index = int(event.get("turn_index") or 0)
            except (TypeError, ValueError):
                continue
            if turn_event in {"StartOfTurn", "TurnResumed"}:
                if turn_event == "TurnResumed":
                    self.eager_end_times.pop(turn_index, None)
                await self._barge_in()
                continue
            if turn_event == "EagerEndOfTurn" and transcript:
                self.eager_end_times[turn_index] = time.monotonic()
                if len(self.eager_end_times) > 8:
                    self.eager_end_times.pop(next(iter(self.eager_end_times)))
                await self._start_draft(transcript, turn_index)
                continue
            if turn_event == "EndOfTurn" and transcript:
                eager_at = self.eager_end_times.pop(turn_index, None)
                if eager_at is not None:
                    self.metrics.flux_eager_to_final_samples.append(int((time.monotonic() - eager_at) * 1000))
                if turn_index in self.finalized_turn_indexes:
                    continue
                self.finalized_turn_indexes.add(turn_index)
                matching_draft = bool(
                    self.draft_task
                    and self.draft_turn_index == turn_index
                    and self._same_transcript(self.draft_transcript, transcript)
                )
                if self.response_task and not self.response_task.done():
                    self.response_task.cancel()
                    await asyncio.gather(self.response_task, return_exceptions=True)
                self.interrupted = False
                self.response_started_at = time.monotonic()
                if matching_draft:
                    # The speculative stream may already be audible. Record
                    # zero final-turn-to-audio latency when so, and retain the
                    # eager timestamp for its separate aggregate metric.
                    if self.tts_first_audio_recorded:
                        self.metrics.end_of_turn_to_first_audio_samples.append(0)
                else:
                    self.eager_response_started_at = 0.0
                    self.tts_first_audio_recorded = False
                self.response_task = asyncio.create_task(self._respond_final(transcript, turn_index), name=f"twilio-response-{self.call_id}-{turn_index}")
                self.response_task.add_done_callback(self._observe_response_task)

    async def _start_draft(self, transcript: str, turn_index: int) -> None:
        if self.draft_task and not self.draft_task.done():
            if self.draft_turn_index == turn_index and self._same_transcript(self.draft_transcript, transcript):
                return
            self.draft_task.cancel()
            await asyncio.gather(self.draft_task, return_exceptions=True)
        self.draft_transcript, self.draft_turn_index = transcript, turn_index
        self.eager_response_started_at = time.monotonic()
        self.response_started_at = 0.0
        self.tts_first_audio_recorded = False
        self.draft_task = asyncio.create_task(
            self._request_agent_stream(transcript, speculative=True),
            name=f"twilio-draft-{self.call_id}-{turn_index}",
        )
        self.draft_task.add_done_callback(self._observe_response_task)

    def _observe_response_task(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            # Never log transcript/audio; the call may continue after a single
            # failed turn, and aggregate health exposes the failure count.
            LOG.warning("Twilio response task failed", extra={"call_id": self.call_id})

    async def _barge_in(self) -> None:
        """Stop active agent speech immediately when the caller starts talking."""
        response_task, draft_task = self.response_task, self.draft_task
        active = [task for task in (response_task, draft_task) if task and not task.done()]
        if not active:
            return
        self.interrupted = True
        self.metrics.barge_ins += 1
        if self.tts_socket is not None:
            try:
                async with self.tts_lock:
                    await self.tts_socket.send(json.dumps({"type": "Interrupt"}))
            except Exception:
                # The task cancellation below still closes a stalled TTS socket.
                pass
        try:
            await self._send_twilio({"event": "clear", "streamSid": self.stream_sid})
        except Exception:
            pass
        # The interrupted TTS stream is discarded; restart rate conversion at
        # the next utterance instead of carrying filter history across it.
        self.tts_resample_state = None
        self.eager_response_started_at = 0.0
        self.response_started_at = 0.0
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        if response_task in active:
            self.response_task = None
        if draft_task in active:
            self.draft_task, self.draft_transcript, self.draft_turn_index = None, "", None

    async def _send_twilio(self, event: dict[str, Any]) -> None:
        # Media, clear, and mark messages share one WebSocket. Serializing
        # sends preserves Twilio's expected order during a barge-in race.
        async with self.twilio_send_lock:
            await self.websocket.send_text(json.dumps(event))

    async def _request_agent(self, transcript: str) -> VoiceTurnResult:
        started = time.monotonic()
        try:
            response = await self.http.post(
                self.settings.chusky_turn_url,
                headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                # The bridge always commits the definitive result itself. Keep
                # this legacy batch request speculative so it never writes a
                # second copy before _commit_turn obtains its idempotent lease.
                json={"callId": self.call_id, "userId": self.user_id, "transcript": transcript, "speculative": True},
                timeout=httpx.Timeout(45.0, connect=10.0),
            )
            response.raise_for_status()
            payload = response.json()
            text = str(payload.get("text") or "").strip()
            cost = float(payload.get("cost") or 0)
            self.metrics.agent_turn_ms_total += int((time.monotonic() - started) * 1000)
            self.metrics.agent_turns += 1
            return VoiceTurnResult(text=normalize_voice_text(text)[:5000], cost=max(0, min(cost, 10)))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics.agent_failures += 1
            raise

    async def _ensure_persistent_tts(self) -> None:
        if self.tts_socket is not None and self.tts_reader_task and not self.tts_reader_task.done():
            return
        if not re.fullmatch(r"flux-[a-z]+-en", self.tts_model):
            raise RuntimeError("VOICE_TTS_MODEL must be a Flux streaming model (for example flux-haley-en)")
        url = twilio_deepgram_speak_url(self.tts_model, native_mulaw=getattr(self.settings, "twilio_native_mulaw", True))
        self.tts_socket = await connect(url, additional_headers={"Authorization": f"Token {self.settings.deepgram_api_key}"}, max_size=1_000_000)
        self.tts_resample_state = None
        self.tts_reader_task = asyncio.create_task(self._receive_persistent_tts(), name=f"twilio-tts-{self.call_id}")

    async def _receive_persistent_tts(self) -> None:
        socket = self.tts_socket
        if socket is None:
            return
        try:
            async for raw in socket:
                if isinstance(raw, bytes):
                    if self.interrupted or self.stop.is_set():
                        continue
                    # response_started_at is reset for every final turn;
                    # record only the first audio frame for that response.
                    if not self.tts_first_audio_recorded:
                        audio_started_at = time.monotonic()
                        first_audio_origin = self.eager_response_started_at or self.response_started_at
                        first_audio_ms = max(0, int((audio_started_at - first_audio_origin) * 1000)) if first_audio_origin else 0
                        self.metrics.tts_first_audio_ms_total += first_audio_ms
                        self.metrics.tts_first_audio_count += 1
                        if self.eager_response_started_at:
                            self.metrics.eager_to_first_audio_samples.append(first_audio_ms)
                            if self.response_started_at:
                                after_final_ms = max(0, int((audio_started_at - self.response_started_at) * 1000))
                                self.metrics.end_of_turn_to_first_audio_samples.append(after_final_ms)
                        else:
                            self.metrics.end_of_turn_to_first_audio_samples.append(first_audio_ms)
                        self.tts_first_audio_recorded = True
                        if self.turn_first_audio_event is not None:
                            self.turn_first_audio_event.set()
                    if getattr(self.settings, "twilio_native_mulaw", True):
                        twilio_audio = raw
                    else:
                        twilio_audio, self.tts_resample_state = deepgram_linear16_to_twilio_mulaw(raw, self.tts_resample_state)
                    for offset in range(0, len(twilio_audio), 1600):
                        payload = base64.b64encode(twilio_audio[offset:offset + 1600]).decode()
                        await self._send_twilio({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}})
                elif isinstance(raw, str):
                    event = json.loads(raw)
                    if event.get("type") == "SpeechMetadata":
                        if self.tts_done_event is not None:
                            self.tts_done_event.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Persistent Twilio TTS connection ended", extra={"call_id": self.call_id})

    async def _send_persistent_tts(self, text: str, flush: bool = False) -> None:
        if not text.strip() and not flush:
            return
        await self._ensure_persistent_tts()
        async with self.tts_lock:
            if self.tts_socket is None:
                raise RuntimeError("persistent TTS connection is unavailable")
            if text.strip():
                await self.tts_socket.send(json.dumps({"type": "Speak", "text": text}))
            if flush:
                await self.tts_socket.send(json.dumps({"type": "Flush"}))

    async def _request_agent_stream(self, transcript: str, *, speculative: bool = False) -> VoiceTurnResult | None:
        """Stream one answer, guarding only the time before speech begins.

        Speculative eager turns are never given a spoken fallback: they may be
        canceled and replaced by the definitive EndOfTurn request. A final
        turn may use a short recovery line when no audio has started within
        the configured budget; that line is intentionally not committed as
        the agent's substantive answer.
        """
        self.turn_first_audio_event = asyncio.Event()
        stream_task = asyncio.create_task(
            self._consume_agent_stream(transcript, speculative=speculative),
            name=f"twilio-agent-stream-{self.call_id}",
        )
        try:
            if not speculative and getattr(self.settings, "turn_fallback_enabled", True):
                budget_ms = getattr(self.settings, "turn_start_budget_ms", 10_000)
                budget_started_at = time.monotonic()
                try:
                    await asyncio.wait_for(self.turn_first_audio_event.wait(), timeout=max(1, int(budget_ms)) / 1000)
                except asyncio.TimeoutError:
                    if turn_start_deadline_exceeded(
                        budget_started_at,
                        time.monotonic(),
                        budget_ms,
                        self.tts_first_audio_recorded,
                        self.interrupted,
                    ) and not self.interrupted and not self.tts_first_audio_recorded:
                        stream_task.cancel()
                        await asyncio.gather(stream_task, return_exceptions=True)
                        self.metrics.turn_start_budget_exceeded += 1
                        await self._speak_slow_turn_fallback()
                        return None
                    raise
            return await stream_task
        finally:
            if not stream_task.done():
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
            self.turn_first_audio_event = None

    async def _consume_agent_stream(self, transcript: str, *, speculative: bool = False) -> VoiceTurnResult:
        started = time.monotonic()
        buffer = ""
        full_text = ""
        cost = 0.0
        first_delta_recorded = False
        self.tts_done_event = asyncio.Event()
        self.tts_first_audio_recorded = False
        try:
            stream_url = f"{self.settings.chusky_turn_url[:-len('/turn')]}/turn-stream"
            async with self.http.stream(
                "POST", stream_url,
                headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                json={"callId": self.call_id, "userId": self.user_id, "transcript": transcript, "speculative": speculative},
                timeout=httpx.Timeout(60.0, connect=10.0),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("type") == "delta":
                        delta = str(event.get("text") or "")
                        if delta and not first_delta_recorded:
                            self.metrics.agent_first_delta_samples.append(int((time.monotonic() - started) * 1000))
                            first_delta_recorded = True
                        full_text += delta
                        buffer += delta
                        if any(buffer.rstrip().endswith(mark) for mark in (".", "!", "?", ":", ";")):
                            # Speak a complete phrase as soon as its boundary
                            # arrives; don't wait for a large fixed buffer.
                            await self._send_persistent_tts(normalize_voice_text(buffer))
                            buffer = ""
                        else:
                            chunk = take_tts_chunk(buffer)
                            if chunk:
                                spoken, buffer = chunk
                                await self._send_persistent_tts(normalize_voice_text(spoken))
                    elif event.get("type") == "done":
                        cost = max(0, min(float(event.get("cost") or 0), 10))
                    elif event.get("type") == "error":
                        raise RuntimeError("streaming voice turn failed")
            full_text = normalize_voice_text(full_text)
            if buffer:
                await self._send_persistent_tts(normalize_voice_text(buffer), flush=True)
            elif full_text:
                await self._send_persistent_tts("", flush=True)
            if full_text:
                await asyncio.wait_for(self.tts_done_event.wait(), timeout=45.0)
            self.metrics.agent_turn_ms_total += int((time.monotonic() - started) * 1000)
            self.metrics.agent_turns += 1
            return VoiceTurnResult(text=normalize_voice_text(full_text)[:5000], cost=cost)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics.agent_failures += 1
            raise

    async def _speak_slow_turn_fallback(self) -> None:
        """Recover from a silent model turn without exposing transport errors."""
        try:
            if self.tts_socket is not None:
                async with self.tts_lock:
                    await self.tts_socket.send(json.dumps({"type": "Interrupt"}))
            await self._send_twilio({"event": "clear", "streamSid": self.stream_sid})
        except Exception:
            pass
        await self._close_persistent_tts()
        self.interrupted = False
        self.metrics.turn_fallbacks += 1
        self.response_started_at = time.monotonic()
        await self._speak(turn_fallback_text(self.metrics.turn_fallbacks - 1))

    async def _speak_persistent_text(self, text: str) -> None:
        self.tts_done_event = asyncio.Event()
        self.tts_first_audio_recorded = False
        await self._send_persistent_tts(text, flush=True)
        await asyncio.wait_for(self.tts_done_event.wait(), timeout=45.0)

    async def _close_persistent_tts(self) -> None:
        if self.tts_reader_task and not self.tts_reader_task.done():
            self.tts_reader_task.cancel()
            await asyncio.gather(self.tts_reader_task, return_exceptions=True)
        self.tts_reader_task = None
        socket, self.tts_socket = self.tts_socket, None
        self.tts_resample_state = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass

    async def _commit_turn(self, transcript: str, result: VoiceTurnResult, turn_index: int) -> None:
        """Commit one completed response without ever asking Chusky to answer twice.

        The Chusky endpoint owns idempotency by call and Flux turn index. A
        retry therefore has the exact same transcript, answer, and cost; a
        temporary busy lease or a 5xx cannot create duplicate history or
        duplicate caller speech.
        """
        url = f"{self.settings.chusky_turn_url[:-len('/turn')]}/commit-turn"
        payload = {
            "callId": self.call_id, "userId": self.user_id, "transcript": transcript,
            "text": result.text, "cost": result.cost, "turnId": f"{turn_index}",
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.http.post(
                    url,
                    headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                    json=payload,
                    timeout=httpx.Timeout(15.0, connect=5.0),
                )
                response.raise_for_status()
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(0.15 * (attempt + 1))
        raise RuntimeError("voice turn commit did not complete") from last_error

    async def _respond_final(self, transcript: str, turn_index: int) -> None:
        result: VoiceTurnResult | None = None
        try:
            matching_draft = bool(
                self.draft_task
                and self.draft_turn_index == turn_index
                and self._same_transcript(self.draft_transcript, transcript)
            )
            if matching_draft:
                # The eager request is already the live streamed response.
                # Reuse it through completion rather than canceling it and
                # paying for a second model generation at EndOfTurn.
                result = await resolve_speculative_draft(
                    self.draft_task,
                    transcript_matches=True,
                    grace_ms=None,
                )
                if result is None and self.tts_first_audio_recorded:
                    # Do not replay a full fallback after partial speech was
                    # already delivered to the caller.
                    return
            else:
                if self.draft_task and not self.draft_task.done():
                    self.draft_task.cancel()
                    await asyncio.gather(self.draft_task, return_exceptions=True)
                self.eager_response_started_at = 0.0
                result = None
            if result is None:
                result = await self._request_agent_stream(transcript, speculative=False)
            if result is None or not result.text or self.interrupted:
                return
            await self._commit_turn(transcript, result, turn_index)
        except Exception:
            # The caller may already have heard the completed streamed answer.
            # Never generate or speak another answer merely because durable
            # history persistence is temporarily unavailable.
            if result is not None:
                self.metrics.agent_failures += 1
                LOG.warning("Twilio voice turn commit failed", extra={"call_id": self.call_id})
                return
            # A provider or bridge deployment may not have the new streaming
            # route yet. Keep the call usable through the proven batch path.
            if not self.interrupted and not self.tts_first_audio_recorded:
                try:
                    await self._close_persistent_tts()
                    result = await self._request_agent(transcript)
                    if result.text:
                        await self._commit_turn(transcript, result, turn_index)
                        await self._speak(result.text)
                except Exception:
                    self.metrics.agent_failures += 1
                    LOG.warning("Twilio streamed response failed", extra={"call_id": self.call_id})
        finally:
            self.draft_task, self.draft_transcript, self.draft_turn_index = None, "", None
            self.eager_response_started_at = 0.0
            self.response_started_at = 0.0

    async def _speak(self, text: str) -> None:
        if not re.fullmatch(r"flux-[a-z]+-en", self.tts_model):
            raise RuntimeError("VOICE_TTS_MODEL must be a Flux streaming model (for example flux-haley-en)")
        url = twilio_deepgram_speak_url(self.tts_model, native_mulaw=getattr(self.settings, "twilio_native_mulaw", True))
        first_audio_at: float | None = None
        resample_state: object | None = None
        async with connect(url, additional_headers={"Authorization": f"Token {self.settings.deepgram_api_key}"}, max_size=1_000_000) as socket:
            self.tts_socket = socket
            try:
                async with self.tts_lock:
                    await socket.send(json.dumps({"type": "Speak", "text": text}))
                    await socket.send(json.dumps({"type": "Flush"}))
                async for raw in socket:
                    if isinstance(raw, bytes):
                        if self.interrupted or self.stop.is_set():
                            return
                        if first_audio_at is None:
                            first_audio_at = time.monotonic()
                            first_audio_ms = int((first_audio_at - self.response_started_at) * 1000)
                            self.metrics.tts_first_audio_ms_total += first_audio_ms
                            self.metrics.tts_first_audio_count += 1
                            self.metrics.end_of_turn_to_first_audio_samples.append(first_audio_ms)
                        if getattr(self.settings, "twilio_native_mulaw", True):
                            twilio_audio = raw
                        else:
                            twilio_audio, resample_state = deepgram_linear16_to_twilio_mulaw(raw, resample_state)
                        # Twilio permits any payload size; bounded 200 ms chunks
                        # reduce jitter and make clear/mark interruption prompt.
                        for offset in range(0, len(twilio_audio), 1600):
                            payload = base64.b64encode(twilio_audio[offset:offset + 1600]).decode()
                            await self._send_twilio({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}})
                    elif isinstance(raw, str):
                        event = json.loads(raw)
                        if event.get("type") == "SpeechMetadata":
                            if not self.interrupted:
                                await self._send_twilio({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": f"chusky-{uuid.uuid4()}"}})
                            return
            finally:
                self.tts_socket = None

    async def _notify_status(self, status: str, error: str | None = None) -> None:
        try:
            response = await self.http.post(
                self.settings.chusky_status_url,
                headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                json={"callId": self.call_id, "userId": self.user_id, "status": status, **({"error": error} if error else {})},
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
            response.raise_for_status()
        except Exception:
            LOG.warning("Could not report Twilio call status", extra={"call_id": self.call_id, "status": status})


class RecallVoiceSession:
    """Interactive meeting audio relay; audio is transient and never persisted."""

    def __init__(self, claims: dict[str, Any], websocket: WebSocket, settings: RecallSettings, recall_metrics: "RecallMetrics", play_intro: bool = True, greeting: str | None = None, tts_model: str | None = None) -> None:
        self.meeting_id = str(claims["meetingId"])
        self.user_id = int(claims["userId"])
        self.websocket = websocket
        self.settings = settings
        self.tts_model = parse_meeting_tts_model({"ttsModel": tts_model} if tts_model else None, settings.tts_model)
        self.metrics = recall_metrics
        self.play_intro = play_intro
        self.interaction_mode = claims.get("interactionMode", "addressed")
        if self.interaction_mode not in ("addressed", "copilot", "representative"):
            raise RuntimeError("Recall meeting interaction mode is invalid")
        self.greeting = greeting or default_meeting_greeting(self.interaction_mode)
        self.language_mode = claims.get("languageMode", "english") if claims.get("languageMode") in ("english", "multilingual") else "english"
        self.language_hints = [item for item in claims.get("languageHints", []) if isinstance(item, str)][:8]
        self.keyterms = [item for item in claims.get("keyterms", []) if isinstance(item, str)][:50]
        self.stt_model = "flux-general-multi" if self.language_mode == "multilingual" else settings.stt_model
        self.audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self.context = MeetingContextWindow()
        self.echo_guard = MeetingEchoGuard()
        self.copilot_gate = CopilotTurnGate(settings.copilot_min_interval_seconds)
        self.stop = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.tts_lock = asyncio.Lock()
        self.stt_socket: Any = None
        self.tts_socket: Any = None
        self.tts_done_event: asyncio.Event | None = None
        self.turn_first_audio_event: asyncio.Event | None = None
        self.tts_first_audio_recorded = False
        self.turn_first_audio_at: float | None = None
        self.response_task: asyncio.Task[None] | None = None
        self.draft_task: asyncio.Task[None] | None = None
        self.draft_transcript = ""
        self.draft_turn_index: int | None = None
        self.draft_result: tuple[str, float, int] | None = None
        self.eager_end_times: dict[int, float] = {}
        self.finalized_turn_indexes: set[int] = set()
        self.response_started_at = 0.0
        self.interrupted = False
        self.turn_index = 0
        self.nova_final_segments: list[str] = []
        self.nova_final_words: list[dict[str, float]] = []
        self.nova_final_audio_start: float | None = None
        self.nova_final_audio_end: float | None = None
        self.stt_audio_started_at: float | None = None

    async def _send_text(self, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_text(json.dumps(payload, separators=(",", ":")))

    async def _send_bytes(self, payload: bytes) -> None:
        async with self.send_lock:
            await self.websocket.send_bytes(payload)

    async def run(self) -> None:
        if not re.fullmatch(r"flux-[a-z]+-en", self.tts_model):
            raise RuntimeError("Recall voice requires a Flux streaming TTS model")
        if self.stt_model == "nova-3":
            stt_url = deepgram_nova_listen_url(
                self.stt_model,
                self.settings.nova_endpointing_ms,
                self.settings.nova_utterance_end_ms,
            )
        else:
            stt_url = deepgram_flux_listen_url(
                self.stt_model,
                self.settings.stt_eager_eot_threshold,
                self.settings.stt_eot_threshold,
                self.settings.stt_eot_timeout_ms,
                language_hints=self.language_hints,
            )
        tts_url = deepgram_flux_speak_url(self.tts_model)
        tasks: set[asyncio.Task[Any]] = set()
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as self.http:
            try:
                async with connect(stt_url, additional_headers={"Authorization": f"Token {self.settings.deepgram_api_key}"}, max_size=1_000_000, ping_interval=20, ping_timeout=20, close_timeout=3) as stt:
                    async with connect(tts_url, additional_headers={"Authorization": f"Token {self.settings.deepgram_api_key}"}, max_size=1_000_000, ping_interval=20, ping_timeout=20, close_timeout=3) as tts:
                        self.stt_socket, self.tts_socket = stt, tts
                        self.metrics.deepgram_sessions += 1
                        tasks = {
                            asyncio.create_task(self._receive_browser(), name=f"recall-browser-{self.meeting_id}"),
                            asyncio.create_task(self._send_stt_audio(), name=f"recall-stt-send-{self.meeting_id}"),
                            asyncio.create_task(self._receive_stt(), name=f"recall-stt-recv-{self.meeting_id}"),
                            asyncio.create_task(self._receive_tts(), name=f"recall-tts-recv-{self.meeting_id}"),
                            asyncio.create_task(asyncio.sleep(self.settings.max_meeting_seconds), name=f"recall-timeout-{self.meeting_id}"),
                        }
                        if self.stt_model.startswith("flux-"):
                            await stt.send(json.dumps({"type": "Configure", "language_hints": self.language_hints, "keyterms": self.keyterms}))
                        await self._send_text({"type": "ready", "sampleRate": DEEPGRAM_INPUT_SAMPLE_RATE, "interactionMode": self.interaction_mode, "languageMode": self.language_mode})
                        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        self.stop.set()
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        for task in done:
                            if not task.get_name().startswith("recall-timeout-"):
                                task.result()
            finally:
                self.stop.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.response_task and not self.response_task.done():
                    self.response_task.cancel()
                if self.draft_task and not self.draft_task.done():
                    self.draft_task.cancel()
                await asyncio.gather(*(task for task in (self.response_task, self.draft_task) if task), return_exceptions=True)
                await self._commit_outcome_transcript()
                for socket in (self.stt_socket, self.tts_socket):
                    if socket is not None:
                        try:
                            await socket.close()
                        except Exception:
                            pass
                self.stt_socket = self.tts_socket = None

    async def _commit_outcome_transcript(self) -> None:
        """Send only the bounded private transcript window for post-meeting outcomes."""
        if self.interaction_mode not in ("copilot", "representative"):
            return
        context = self.context.snapshot()
        if not context:
            return
        try:
            payload = build_meeting_outcome_payload(self.meeting_id, self.user_id, context)
        except ValueError:
            LOG.warning("Recall outcome transcript rejected locally", extra={"meeting_id": self.meeting_id})
            return
        endpoint = self.settings.commit_turn_url.rstrip("/").removesuffix("/commit-turn") + "/commit-transcript"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.http.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                    json=payload,
                    timeout=httpx.Timeout(10.0, connect=5.0),
                )
                response.raise_for_status()
                return
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as error:
                last_error = error
                status_code = error.response.status_code
                if status_code < 500 and status_code != 429:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.15 * (attempt + 1))
            except httpx.RequestError as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(0.15 * (attempt + 1))
        # Never log transcript text or provider payload; the outcome workflow
        # falls back to already-committed spoken turns if this best-effort flush fails.
        LOG.warning("Could not commit Recall outcome transcript", extra={"meeting_id": self.meeting_id, "error_type": type(last_error).__name__ if last_error else "UnknownError"})

    async def _receive_browser(self) -> None:
        ready = False
        while not self.stop.is_set():
            event = await self.websocket.receive()
            if event.get("type") == "websocket.disconnect":
                return
            text = event.get("text")
            if text is not None:
                try:
                    control = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(control, dict):
                    continue
                if control.get("type") == "stop":
                    return
                if control.get("type") == "force_end_turn":
                    if self.stt_socket is not None and self.stt_model.startswith("flux-"):
                        try:
                            await self.stt_socket.send(json.dumps({"type": "ForceEndTurn"}))
                        except Exception:
                            LOG.info("Recall force-end-turn could not be sent", extra={"meeting_id": self.meeting_id})
                    continue
                if control.get("type") == "ready":
                    try:
                        sample_rate = int(control.get("sampleRate", 0))
                    except (TypeError, ValueError, OverflowError):
                        sample_rate = 0
                    if sample_rate != DEEPGRAM_INPUT_SAMPLE_RATE:
                        await self._send_text({"type": "error", "error": "audio_sample_rate_unsupported"})
                        return
                    ready = True
                    self.metrics.browser_ready += 1
                    if self.play_intro and self.response_task is None:
                        self.response_task = asyncio.create_task(self._speak(self.greeting), name=f"recall-intro-{self.meeting_id}")
                        self.response_task.add_done_callback(self._observe_response)
                continue
            audio = event.get("bytes")
            if audio is None:
                continue
            if not ready or len(audio) < 2 or len(audio) > 96_000 or len(audio) % 2:
                continue
            if self.audio.full():
                # Prefer fresh speech over stale audio during temporary network
                # or STT backpressure. Never allow unbounded media buffering.
                try:
                    self.audio.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self.audio.put_nowait(audio)
                self.metrics.browser_audio_frames += 1
                self.metrics.browser_audio_bytes += len(audio)
            except asyncio.QueueFull:
                pass

    async def _send_stt_audio(self) -> None:
        while not self.stop.is_set():
            frame = await self.audio.get()
            if self.stt_socket is not None:
                if self.stt_audio_started_at is None:
                    self.stt_audio_started_at = time.time()
                await self.stt_socket.send(frame)

    async def _receive_stt(self) -> None:
        if self.stt_socket is None:
            return
        async for raw in self.stt_socket:
            if not isinstance(raw, str):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "Results" and self.stt_model == "nova-3":
                channel = event.get("channel")
                alternatives = channel.get("alternatives") if isinstance(channel, dict) else None
                alternative = alternatives[0] if isinstance(alternatives, list) and alternatives and isinstance(alternatives[0], dict) else {}
                transcript = normalize_voice_text(str(alternative.get("transcript") or ""))[:5000]
                words = alternative.get("words")
                if bool(event.get("is_final")) and isinstance(words, list):
                    for word in words:
                        if not isinstance(word, dict):
                            continue
                        start, end = word.get("start"), word.get("end")
                        if len(self.nova_final_words) < 2_500 and isinstance(start, (int, float)) and isinstance(end, (int, float)) and not isinstance(start, bool) and not isinstance(end, bool):
                            self.nova_final_words.append({"start": float(start), "end": float(end)})
                try:
                    segment_start = float(event.get("start"))
                    segment_end = segment_start + float(event.get("duration"))
                    if math.isfinite(segment_start) and math.isfinite(segment_end) and segment_start >= 0 and segment_end >= segment_start:
                        self.nova_final_audio_start = segment_start if self.nova_final_audio_start is None else min(self.nova_final_audio_start, segment_start)
                        self.nova_final_audio_end = segment_end if self.nova_final_audio_end is None else max(self.nova_final_audio_end, segment_end)
                except (TypeError, ValueError, OverflowError):
                    pass
                if transcript and not bool(event.get("is_final")):
                    # Nova emits SpeechStarted without transcript content. Use
                    # interim text to interrupt only a real participant, not
                    # Chusky's own echoed Output Media.
                    if self.response_task and not self.response_task.done() and not self.echo_guard.is_echo(transcript):
                        await self._interrupt()
                    continue
                if transcript and bool(event.get("is_final")):
                    if not self.nova_final_segments or self.nova_final_segments[-1] != transcript:
                        self.nova_final_segments.append(transcript)
                if bool(event.get("speech_final")):
                    final_transcript = normalize_voice_text(" ".join(self.nova_final_segments))[:5000]
                    self.nova_final_segments.clear()
                    timing = flux_turn_time_bounds_ms(self.stt_audio_started_at, {
                        "words": self.nova_final_words,
                        "audio_window_start": self.nova_final_audio_start,
                        "audio_window_end": self.nova_final_audio_end,
                    })
                    self.nova_final_words.clear()
                    self.nova_final_audio_start = self.nova_final_audio_end = None
                    if final_transcript:
                        await self._handle_final_stt_turn(final_transcript, *(timing or (None, None)))
                continue
            if event.get("type") == "UtteranceEnd" and self.stt_model == "nova-3":
                # UtteranceEnd is a server-side gap-detection fallback for
                # noisy meetings where audio VAD cannot produce speech_final.
                final_transcript = normalize_voice_text(" ".join(self.nova_final_segments))[:5000]
                self.nova_final_segments.clear()
                timing = flux_turn_time_bounds_ms(self.stt_audio_started_at, {
                    "words": self.nova_final_words,
                    "audio_window_start": self.nova_final_audio_start,
                    "audio_window_end": self.nova_final_audio_end,
                })
                self.nova_final_words.clear()
                self.nova_final_audio_start = self.nova_final_audio_end = None
                if final_transcript:
                    await self._handle_final_stt_turn(final_transcript, *(timing or (None, None)))
                continue
            if event.get("type") != "TurnInfo":
                continue
            turn_event = str(event.get("event") or "")
            transcript = normalize_voice_text(str(event.get("transcript") or ""))[:5000]
            try:
                provider_turn_index = int(event.get("turn_index") or 0)
            except (TypeError, ValueError):
                provider_turn_index = 0
            if turn_event in {"StartOfTurn", "TurnResumed"}:
                # Ordinary participants talking should neither burn model/TTS
                # budget nor cut off Chusky. Barge in only when the wake word
                # is actually present in the current recognized turn. Ignore
                # provisional transcripts matching recently streamed TTS so
                # Chusky cannot interrupt itself through the meeting mix.
                if self.echo_guard.is_echo(transcript):
                    self.metrics.turns_suppressed += 1
                elif is_recall_invocation(transcript):
                    if turn_event == "StartOfTurn":
                        asyncio.create_task(self._report_runtime("healthy", {}, "A participant started an addressed turn", "speech_detected"))
                    await self._interrupt()
                if turn_event == "TurnResumed" and provider_turn_index:
                    self.eager_end_times.pop(provider_turn_index, None)
            elif turn_event == "EndOfTurn" and transcript:
                timing = flux_turn_time_bounds_ms(self.stt_audio_started_at, event)
                if provider_turn_index:
                    self.eager_end_times.pop(provider_turn_index, None)
                    if provider_turn_index in self.finalized_turn_indexes:
                        continue
                    self.finalized_turn_indexes.add(provider_turn_index)
                    if len(self.finalized_turn_indexes) > 64:
                        self.finalized_turn_indexes = set(sorted(self.finalized_turn_indexes)[-32:])
                await self._handle_flux_end_turn(transcript, provider_turn_index, *(timing or (None, None)))
            elif turn_event == "EagerEndOfTurn" and transcript and provider_turn_index:
                if len(self.eager_end_times) >= 8:
                    self.eager_end_times.pop(next(iter(self.eager_end_times)))
                self.eager_end_times[provider_turn_index] = time.monotonic()
                # Keep speculative work conservative: only an explicit
                # invocation may start a draft. Final turns still use the
                # normal copilot/representative cadence.
                if is_recall_invocation(transcript) and not self.echo_guard.is_echo(transcript):
                    await self._start_draft(transcript, provider_turn_index)

    async def _start_draft(self, transcript: str, provider_turn_index: int) -> None:
        if self.draft_task and not self.draft_task.done():
            if self.draft_turn_index == provider_turn_index and self._same_transcript(self.draft_transcript, transcript):
                return
            await self._interrupt()
        elif self.response_task and not self.response_task.done():
            await self._interrupt()
        self.draft_transcript = transcript
        self.draft_turn_index = provider_turn_index
        self.draft_result = None
        asyncio.create_task(self._report_runtime("healthy", {"eager": True}, "A speculative response started after eager end-of-turn", "eager_transcript"))
        context = self.context.snapshot()
        self.draft_task = asyncio.create_task(
            self._respond(transcript, self.turn_index + 1, context, None, None, None, speculative=True),
            name=f"recall-draft-{self.meeting_id}-{provider_turn_index}",
        )
        self.draft_task.add_done_callback(self._observe_response)

    @staticmethod
    def _same_transcript(left: str, right: str) -> bool:
        normalize = lambda value: " ".join("".join(char.lower() if char.isalnum() or char.isspace() else " " for char in value).split())
        return normalize(left) == normalize(right)

    async def _handle_flux_end_turn(self, transcript: str, provider_turn_index: int, turn_started_at_ms: int | None = None, turn_ended_at_ms: int | None = None) -> None:
        matching_draft = bool(self.draft_task and self.draft_turn_index == provider_turn_index and self._same_transcript(self.draft_transcript, transcript))
        if matching_draft and self.draft_task:
            await asyncio.gather(self.draft_task, return_exceptions=True)
            result = self.draft_result
            if result and result[0]:
                self.turn_index += 1
                await self._commit_draft(transcript, result[0], result[1], self.turn_index, result[2])
                self._clear_draft()
                return
        if self.draft_task and not self.draft_task.done():
            await self._interrupt()
        self._clear_draft()
        await self._handle_final_stt_turn(transcript, turn_started_at_ms, turn_ended_at_ms)

    def _clear_draft(self) -> None:
        self.draft_task = None
        self.draft_transcript = ""
        self.draft_turn_index = None
        self.draft_result = None

    async def _commit_draft(self, transcript: str, text: str, cost: float, turn_id: int, response_ms: int) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            response = await client.post(
                self.settings.commit_turn_url,
                headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                json={"meetingId": self.meeting_id, "userId": self.user_id, "transcript": transcript, "text": text, "cost": cost, "turnId": f"flux-{turn_id}", "speak": True, "runtimeState": "healthy", "turn": {"completed": True, "eager": True, "finalResponseMs": response_ms}},
            )
            response.raise_for_status()
        self.context.add("participant", transcript)
        self.context.add("chusky", text)

    async def _handle_final_stt_turn(self, transcript: str, turn_started_at_ms: int | None = None, turn_ended_at_ms: int | None = None) -> None:
        """Send one completed Flux or Nova utterance through the same policy."""
        self.metrics.stt_final_turns += 1
        if self.echo_guard.is_echo(transcript):
            self.metrics.turns_suppressed += 1
            return
        invoked = is_recall_invocation(transcript)
        context = self.context.snapshot()
        context_turn_id = self.context.add("participant", transcript)
        should_evaluate = invoked
        if self.interaction_mode in ("copilot", "representative"):
            should_evaluate = self.copilot_gate.should_evaluate(invoked)
        elif not invoked:
            should_evaluate = False
        if not should_evaluate:
            self.metrics.turns_suppressed += 1
            return
        asyncio.create_task(self._report_runtime("healthy", {}, "A final speech turn was received", "final_transcript"))
        self.turn_index += 1
        self.metrics.agent_requests += 1
        if self.response_task and not self.response_task.done():
            await self._interrupt()
        self.interrupted = False
        self.response_started_at = time.monotonic()
        self.response_task = asyncio.create_task(self._respond(transcript, self.turn_index, context, context_turn_id, turn_started_at_ms, turn_ended_at_ms), name=f"recall-response-{self.meeting_id}-{self.turn_index}")
        self.response_task.add_done_callback(self._observe_response)

    def _observe_response(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # Meeting audio/transcripts are sensitive; keep diagnostics free
            # of transcripts, response content, media URLs, and ticket values.
            # Log the error class and a sanitized message so configuration
            # problems (e.g. 401 wrong bridge secret, 404 meeting not in_call)
            # are immediately visible without exposing sensitive data.
            self.metrics.agent_failures += 1
            exc_type = type(exc).__name__
            # Include HTTP status codes so 401/404/429 are distinguishable.
            status_hint = ""
            if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
                status_hint = f" HTTP {exc.response.status_code}"
            elif hasattr(exc, "status_code"):
                status_hint = f" HTTP {exc.status_code}"
            failure_code = getattr(exc, "failure_code", None)
            if failure_code not in RecallMeetingAgentError.ALLOWED_CODES:
                failure_code = None
            LOG.warning(
                "Recall meeting response failed: %s%s%s",
                exc_type, status_hint, f" code={failure_code}" if failure_code else "",
                extra={"meeting_id": self.meeting_id, "stage": "agent_response", "error_type": exc_type, **({"failure_code": failure_code} if failure_code else {})},
            )
            asyncio.create_task(self._report_runtime("degraded", {"failed": True, "errorCode": failure_code or "agent_run_failed"}, "Meeting response entered a degraded state"))

    async def _report_runtime(self, state: str, turn: dict[str, Any], summary: str, event_type: str | None = None) -> None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                await client.post(
                    self.settings.commit_turn_url,
                    headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                    json={"meetingId": self.meeting_id, "userId": self.user_id, "turnId": f"runtime-{self.turn_index}-{int(time.time() * 1000)}", "speak": False, "runtimeState": state, "summary": summary[:280], **({"eventType": event_type} if event_type else {}), "turn": turn},
                )
        except Exception:
            LOG.info(summary, extra={"meeting_id": self.meeting_id})

    async def _interrupt(self) -> None:
        active_tasks = [task for task in (self.response_task, self.draft_task) if task and not task.done()]
        if not active_tasks:
            return
        self.interrupted = True
        if self.tts_socket is not None:
            try:
                async with self.tts_lock:
                    await self.tts_socket.send(json.dumps({"type": "Interrupt"}))
            except Exception:
                pass
        try:
            await self._send_text({"type": "clear"})
        except Exception:
            pass
        for task in active_tasks:
            task.cancel()
        await asyncio.gather(*active_tasks, return_exceptions=True)
        if self.response_task in active_tasks:
            self.response_task = None
        if self.draft_task in active_tasks:
            self._clear_draft()

    async def _receive_tts(self) -> None:
        if self.tts_socket is None:
            return
        try:
            async for raw in self.tts_socket:
                if isinstance(raw, bytes):
                    if not self.interrupted and not self.stop.is_set():
                        if not self.tts_first_audio_recorded:
                            self.tts_first_audio_recorded = True
                            self.turn_first_audio_at = time.monotonic()
                            if self.turn_first_audio_event is not None:
                                self.turn_first_audio_event.set()
                            asyncio.create_task(self._report_runtime("healthy", {}, "The meeting agent started sending audio", "first_audio"))
                        await self._send_bytes(raw)
                        self.metrics.tts_audio_frames += 1
                        self.metrics.tts_audio_bytes += len(raw)
                elif isinstance(raw, str):
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "SpeechMetadata" and self.tts_done_event is not None:
                        self.tts_done_event.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Recall Deepgram TTS connection ended", extra={"meeting_id": self.meeting_id})

    async def _send_tts(self, text: str = "", flush: bool = False) -> None:
        if not text.strip() and not flush:
            return
        if self.tts_socket is None:
            raise RuntimeError("meeting TTS connection is unavailable")
        async with self.tts_lock:
            if text.strip():
                await self.tts_socket.send(json.dumps({"type": "Speak", "text": normalize_voice_text(text)}))
            if flush:
                await self.tts_socket.send(json.dumps({"type": "Flush"}))

    async def _speak(self, text: str) -> None:
        self.tts_done_event = asyncio.Event()
        self.tts_first_audio_recorded = False
        self.echo_guard.remember_output(text)
        await self._send_tts(text, flush=True)
        await asyncio.wait_for(self.tts_done_event.wait(), timeout=45.0)

    async def _speak_slow_turn_fallback(self) -> None:
        """Keep a meeting conversational when a response has not started."""
        try:
            if self.tts_socket is not None:
                async with self.tts_lock:
                    await self.tts_socket.send(json.dumps({"type": "Interrupt"}))
            await self._send_text({"type": "clear"})
        except Exception:
            pass
        self.interrupted = False
        self.metrics.turn_fallbacks += 1
        self.response_started_at = time.monotonic()
        await self._speak(turn_fallback_text(self.metrics.turn_fallbacks - 1))
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                await client.post(
                    self.settings.commit_turn_url,
                    headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                    json={"meetingId": self.meeting_id, "userId": self.user_id, "turnId": f"flux-fallback-{self.turn_index}", "speak": False, "runtimeState": "degraded", "turn": {"fallback": True, "completed": True}},
                )
        except Exception:
            LOG.info("Recall fallback runtime state could not be persisted", extra={"meeting_id": self.meeting_id})

    async def _respond(
        self,
        transcript: str,
        turn_id: int,
        context: list[dict[str, str]],
        context_turn_id: int | None,
        turn_started_at_ms: int | None,
        turn_ended_at_ms: int | None,
        speculative: bool = False,
    ) -> None:
        if self.tts_socket is None:
            LOG.warning("Recall meeting response skipped: TTS socket unavailable", extra={"meeting_id": self.meeting_id})
            return
        self.tts_done_event = asyncio.Event()
        buffer = ""
        full_text = ""
        cost = 0.0
        received_done = False
        speaking = self.interaction_mode == "addressed"
        received_delta = False
        started = time.monotonic()
        budget_ms = getattr(self.settings, "turn_start_budget_ms", 10_000)
        budget_started_at: float | None = started if speaking and not speculative else None
        self.turn_first_audio_event = asyncio.Event()
        self.tts_first_audio_recorded = False
        self.turn_first_audio_at = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(65.0, connect=8.0)) as client:
            try:
                async with client.stream(
                    "POST",
                    self.settings.turn_stream_url,
                    headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                    json={
                        "meetingId": self.meeting_id,
                        "userId": self.user_id,
                        "transcript": transcript,
                        "context": context,
                        "interactionMode": self.interaction_mode,
                        **({"turnStartedAtMs": turn_started_at_ms, "turnEndedAtMs": turn_ended_at_ms} if turn_started_at_ms is not None and turn_ended_at_ms is not None else {}),
                    },
                ) as response:
                    if response.status_code != 200:
                        LOG.warning(
                            "Recall turn-stream returned HTTP %d (check RECALL_MEDIA_BRIDGE_SECRET and meeting in_call status)",
                            response.status_code,
                            extra={"meeting_id": self.meeting_id},
                        )
                    response.raise_for_status()
                    lines = response.aiter_lines().__aiter__()
                    while True:
                        try:
                            if not speculative and budget_started_at is not None and not self.tts_first_audio_recorded and getattr(self.settings, "turn_fallback_enabled", True):
                                remaining = max(0.001, float(max(1, int(budget_ms))) / 1000 - (time.monotonic() - budget_started_at))
                                if turn_start_deadline_exceeded(budget_started_at, time.monotonic(), budget_ms, self.tts_first_audio_recorded, self.interrupted):
                                    raise asyncio.TimeoutError
                                line = await asyncio.wait_for(lines.__anext__(), timeout=remaining)
                            else:
                                line = await lines.__anext__()
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            if not speculative and not self.interrupted and not self.tts_first_audio_recorded and getattr(self.settings, "turn_fallback_enabled", True):
                                self.metrics.turn_start_budget_exceeded += 1
                                await self._speak_slow_turn_fallback()
                                return
                            raise
                        if not line:
                            continue
                        event = json.loads(line)
                        if event.get("type") == "speaker":
                            self.context.set_speaker(context_turn_id, event.get("name"))
                        elif event.get("type") == "delta":
                            if not speaking:
                                continue
                            delta = normalize_voice_delta(str(event.get("text") or ""))
                            if not delta:
                                continue
                            if not received_delta:
                                asyncio.create_task(self._report_runtime("healthy", {}, "The meeting agent started speaking", "agent_first_token"))
                            received_delta = True
                            full_text += delta
                            buffer += delta
                            if any(buffer.rstrip().endswith(mark) for mark in (".", "!", "?", ":", ";")):
                                self.echo_guard.remember_output(buffer)
                                await self._send_tts(buffer)
                                buffer = ""
                            elif len(buffer) >= 120:
                                self.echo_guard.remember_output(buffer)
                                await self._send_tts(buffer)
                                buffer = ""
                        elif event.get("type") == "silent":
                            self.metrics.agent_silent += 1
                            speaking = False
                            budget_started_at = None
                            full_text = ""
                            buffer = ""
                        elif event.get("type") == "speak":
                            self.metrics.agent_speaking += 1
                            speaking = True
                            received_done = False
                            budget_started_at = time.monotonic()
                        elif event.get("type") == "mode":
                            if event.get("mode") == "addressed":
                                self.interaction_mode = "addressed"
                            await self._send_text({"type": "mode", "mode": self.interaction_mode, "reason": event.get("reason")})
                        elif event.get("type") == "done":
                            cost = max(0.0, min(float(event.get("cost") or 0), 10.0))
                            received_done = True
                            if event.get("speak") is False:
                                speaking = False
                                budget_started_at = None
                                full_text = ""
                                buffer = ""
                            elif speaking and not received_delta:
                                fallback_text = normalize_voice_text(str(event.get("text") or ""))[:5000]
                                full_text = fallback_text
                                buffer = fallback_text
                        elif event.get("type") == "error":
                            raise RecallMeetingAgentError(event.get("code"))
            finally:
                self.turn_first_audio_event = None
        full_text = normalize_voice_text(full_text)[:5000]
        if not received_done:
            return
        if not full_text:
            if speculative:
                self.draft_result = ("", cost, max(0, round((time.monotonic() - started) * 1000)))
                return
            if self.interaction_mode in ("copilot", "representative"):
                async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
                    response = await client.post(
                        self.settings.commit_turn_url,
                        headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                        json={"meetingId": self.meeting_id, "userId": self.user_id, "cost": cost, "turnId": f"flux-{turn_id}", "speak": False, "runtimeState": "healthy", "turn": {"completed": True}},
                    )
                    response.raise_for_status()
            return
        if buffer.strip():
            self.echo_guard.remember_output(buffer)
        await self._send_tts(buffer, flush=True)
        await asyncio.wait_for(self.tts_done_event.wait(), timeout=45.0)
        asyncio.create_task(self._report_runtime("healthy", {}, "The meeting agent finished the spoken response", "final_audio"))
        response_ms = max(0, round((time.monotonic() - started) * 1000))
        if speculative:
            self.draft_result = (full_text, cost, response_ms)
            return
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            response = await client.post(
                self.settings.commit_turn_url,
                headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                json={"meetingId": self.meeting_id, "userId": self.user_id, "transcript": transcript, "text": full_text, "cost": cost, "turnId": f"flux-{turn_id}", "speak": True, "runtimeState": "healthy", "turn": {"completed": True, **({"firstAudioMs": max(0, round((self.turn_first_audio_at - started) * 1000))} if self.turn_first_audio_at else {}), "finalResponseMs": response_ms}},
            )
            response.raise_for_status()
        self.context.add("chusky", full_text)
        _elapsed_ms = int((time.monotonic() - started) * 1000)


class RecallMeetingManager:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.lock = asyncio.Lock()

    async def reserve(self, meeting_id: str, maximum: int) -> bool:
        async with self.lock:
            if meeting_id in self.active or len(self.active) >= maximum:
                return False
            self.active.add(meeting_id)
            return True

    async def release(self, meeting_id: str) -> None:
        async with self.lock:
            self.active.discard(meeting_id)


@dataclass
class RecallMetrics:
    """Aggregate, content-free diagnostics for the live Recall media path."""
    websocket_sessions: int = 0
    browser_ready: int = 0
    browser_audio_frames: int = 0
    browser_audio_bytes: int = 0
    deepgram_sessions: int = 0
    stt_final_turns: int = 0
    turns_suppressed: int = 0
    agent_requests: int = 0
    agent_speaking: int = 0
    agent_silent: int = 0
    agent_failures: int = 0
    turn_start_budget_exceeded: int = 0
    turn_fallbacks: int = 0
    tts_audio_frames: int = 0
    tts_audio_bytes: int = 0
    invalid_ticket_rejections: int = 0
    media_authorization_waits: int = 0
    media_authorization_timeouts: int = 0
    media_authorization_rejections: int = 0
    initialization_failures: int = 0
    screen_share_websocket_sessions: int = 0
    screen_share_frames_sampled: int = 0
    screen_share_frames_forwarded: int = 0
    screen_share_frames_dropped: int = 0

    def snapshot(self, active_meetings: int) -> dict[str, Any]:
        return {
            "activeMeetings": active_meetings,
            "websocketSessions": self.websocket_sessions,
            "browserReady": self.browser_ready,
            "browserAudioFrames": self.browser_audio_frames,
            "browserAudioBytes": self.browser_audio_bytes,
            "deepgramSessions": self.deepgram_sessions,
            "sttFinalTurns": self.stt_final_turns,
            "turnsSuppressed": self.turns_suppressed,
            "agentRequests": self.agent_requests,
            "agentSpeaking": self.agent_speaking,
            "agentSilent": self.agent_silent,
            "agentFailures": self.agent_failures,
            "turnStartBudgetExceeded": self.turn_start_budget_exceeded,
            "turnFallbacks": self.turn_fallbacks,
            "ttsAudioFrames": self.tts_audio_frames,
            "ttsAudioBytes": self.tts_audio_bytes,
            "invalidTicketRejections": self.invalid_ticket_rejections,
            "mediaAuthorizationWaits": self.media_authorization_waits,
            "mediaAuthorizationTimeouts": self.media_authorization_timeouts,
            "mediaAuthorizationRejections": self.media_authorization_rejections,
            "initializationFailures": self.initialization_failures,
            "screenShareWebsocketSessions": self.screen_share_websocket_sessions,
            "screenShareFramesSampled": self.screen_share_frames_sampled,
            "screenShareFramesForwarded": self.screen_share_frames_forwarded,
            "screenShareFramesDropped": self.screen_share_frames_dropped,
        }


@dataclass
class BridgeMetrics:
    twilio_started: int = 0
    twilio_completed: int = 0
    twilio_failed: int = 0
    barge_ins: int = 0
    dropped_inbound_frames: int = 0
    agent_turns: int = 0
    agent_turn_ms_total: int = 0
    agent_failures: int = 0
    turn_start_budget_exceeded: int = 0
    turn_fallbacks: int = 0
    tts_first_audio_count: int = 0
    tts_first_audio_ms_total: int = 0
    flux_eager_to_final_samples: deque[int] = field(default_factory=lambda: deque(maxlen=256))
    agent_first_delta_samples: deque[int] = field(default_factory=lambda: deque(maxlen=256))
    eager_to_first_audio_samples: deque[int] = field(default_factory=lambda: deque(maxlen=256))
    end_of_turn_to_first_audio_samples: deque[int] = field(default_factory=lambda: deque(maxlen=256))

    def snapshot(self, active_twilio: int) -> dict[str, Any]:
        return {
            "twilio": {
                "active": active_twilio,
                "started": self.twilio_started,
                "completed": self.twilio_completed,
                "failed": self.twilio_failed,
                "bargeIns": self.barge_ins,
                "droppedInboundFrames": self.dropped_inbound_frames,
                "agentFailures": self.agent_failures,
                "turnStartBudgetExceeded": self.turn_start_budget_exceeded,
                "turnFallbacks": self.turn_fallbacks,
                "averageAgentTurnMs": round(self.agent_turn_ms_total / self.agent_turns) if self.agent_turns else None,
                "averageTtsFirstAudioMs": round(self.tts_first_audio_ms_total / self.tts_first_audio_count) if self.tts_first_audio_count else None,
                "latencyMs": {
                    "fluxEagerToFinal": latency_summary(list(self.flux_eager_to_final_samples)),
                    "agentFirstDelta": latency_summary(list(self.agent_first_delta_samples)),
                    "eagerToFirstAudio": latency_summary(list(self.eager_to_first_audio_samples)),
                    "endOfTurnToFirstAudio": latency_summary(list(self.end_of_turn_to_first_audio_samples)),
                },
            },
        }


class CallManager:
    def __init__(self) -> None:
        self.twilio_calls: set[str] = set()
        self.lock = asyncio.Lock()

    async def reserve_twilio(self, call_id: str, settings: Settings) -> bool:
        async with self.lock:
            if call_id in self.twilio_calls or len(self.twilio_calls) >= settings.max_active_calls:
                return False
            self.twilio_calls.add(call_id)
            return True

    async def release_twilio(self, call_id: str) -> None:
        async with self.lock:
            self.twilio_calls.discard(call_id)


app = FastAPI(title="Chusky Voice Media Bridge", docs_url=None, redoc_url=None)
calls = CallManager()
recall_meetings = RecallMeetingManager()
recall_metrics = RecallMetrics()
recall_handshakes = asyncio.Semaphore(32)
metrics = BridgeMetrics()
_recall_probe_lock = asyncio.Lock()
_recall_probe_cache: tuple[float, str] | None = None


async def probe_recall_media_authorization(settings: RecallSettings) -> str:
    """Verify the root media route without exposing secrets or meeting data."""
    global _recall_probe_cache
    now = time.monotonic()
    if _recall_probe_cache and now - _recall_probe_cache[0] < 30:
        return _recall_probe_cache[1]
    async with _recall_probe_lock:
        now = time.monotonic()
        if _recall_probe_cache and now - _recall_probe_cache[0] < 30:
            return _recall_probe_cache[1]
        try:
            # An empty payload deliberately reaches validation. A 400 means
            # the route and bridge secret are live; 404 means a stale or
            # incorrectly deployed root service; 401 means secret drift.
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=2.0)) as client:
                response = await client.post(
                    settings.media_authorize_url,
                    headers={"Authorization": f"Bearer {settings.bridge_secret}"},
                    json={},
                )
            if response.status_code == 400:
                result = "configured"
            elif response.status_code == 401:
                result = "bridge_auth_mismatch"
            elif response.status_code == 404:
                result = "route_missing"
            elif response.status_code == 503:
                result = "root_meetings_disabled"
            elif response.status_code in (408, 425, 429) or response.status_code >= 500:
                result = "upstream_unavailable"
            else:
                result = f"http_{response.status_code}"
        except (httpx.HTTPError, OSError, asyncio.TimeoutError):
            result = "unreachable"
        _recall_probe_cache = (time.monotonic(), result)
        return result


def authenticate(authorization: str | None, settings: Settings) -> None:
    expected = f"Bearer {settings.bridge_secret}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        settings = Settings.from_env()
        twilio_ready = bool(settings.twilio_auth_token and settings.twilio_media_stream_url.startswith("wss://"))
        return {"ok": True, "checks": {"twilioWebSocket": "configured" if twilio_ready else "misconfigured", "fluxStt": "configured" if settings.stt_model.startswith("flux-") else "misconfigured", "fluxTts": "configured" if settings.tts_model.startswith("flux-") else "misconfigured"}, "metrics": metrics.snapshot(len(calls.twilio_calls))}
    except RuntimeError:
        return {"ok": False, "checks": {"configuration": "misconfigured"}, "metrics": metrics.snapshot(len(calls.twilio_calls))}


@app.get("/recall/health")
async def recall_health() -> dict[str, Any]:
    try:
        settings = RecallSettings.from_env()
        media_authorization = await probe_recall_media_authorization(settings)
        status = "configured" if media_authorization == "configured" else "misconfigured" if media_authorization in {"bridge_auth_mismatch", "route_missing", "root_meetings_disabled"} else "degraded"
        visual_status = visual_configuration_status(settings.visual_frame_url, settings.realtime_secret)
        return {
            "ok": status != "misconfigured",
            "provider": "recall",
            "status": status,
            "checks": {"mediaAuthorization": media_authorization},
            "optionalFeatures": {"sharedScreenUnderstanding": visual_status, "sharedScreenConfigurationIssue": visual_configuration_issue(settings.visual_frame_url, settings.realtime_secret)},
            "metrics": recall_metrics.snapshot(len(recall_meetings.active)),
        }
    except RecallConfigurationError as exc:
        enabled = os.getenv("RECALL_MEETINGS_ENABLED", "false").strip().lower() == "true"
        return {"ok": not enabled, "provider": "recall", "status": "misconfigured" if enabled else "disabled", "configurationIssue": exc.code, "metrics": recall_metrics.snapshot(len(recall_meetings.active))}
    except RuntimeError:
        enabled = os.getenv("RECALL_MEETINGS_ENABLED", "false").strip().lower() == "true"
        return {"ok": not enabled, "provider": "recall", "status": "misconfigured" if enabled else "disabled", "configurationIssue": "invalid_configuration", "metrics": recall_metrics.snapshot(len(recall_meetings.active))}


RECALL_MEDIA_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(self)",
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline' blob:; style-src 'unsafe-inline'; connect-src 'self' wss:; worker-src blob:; media-src blob:; img-src data: https://chusky-web.vercel.app; base-uri 'none'; form-action 'none'; frame-ancestors 'self' https://*.recall.ai",
}

RECALL_MEDIA_UNAVAILABLE_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>Chusky</title>
  <style>
    :root { color-scheme: dark; font: 16px/1.5 system-ui, sans-serif; background: #101216; color: #f6f7f9; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #101216; }
    main { width: min(100%, 680px); padding: 40px 28px; display: grid; justify-items: center; gap: 14px; border: 1px solid #333947; border-radius: 22px; background: #191d25; text-align: center; }
    img { width: 144px; aspect-ratio: 1; border-radius: 50%; object-fit: cover; }
    h1, p { margin: 0; }
    .role { color: #bdc7d8; }
    .status { margin-top: 8px; max-width: 48ch; color: #ffaaa4; }
  </style>
</head>
<body><main aria-label="Chusky meeting participant">
  <img src="https://chusky-web.vercel.app/chusky/chusky-profile.jpg" alt="Chusky, a digital assistant" referrerpolicy="no-referrer">
  <h1>Chusky</h1><p class="role">Digital assistant</p>
  <p class="status" role="status">Meeting audio is temporarily unavailable. Please ask the meeting host to rejoin Chusky.</p>
</main></body>
</html>"""


@app.get("/recall/media", response_class=HTMLResponse)
async def recall_media_page() -> HTMLResponse:
    try:
        page = Path(__file__).with_name("recall_media.html").read_text("utf-8")
    except OSError as exc:
        # Keep the failure visible inside the meeting and diagnose it without
        # logging filesystem paths, environment values, or provider data.
        LOG.error(
            "Recall media page asset unavailable",
            extra={"stage": "media_page_asset", "reason": "read_failed", "error_type": type(exc).__name__},
        )
        return HTMLResponse(RECALL_MEDIA_UNAVAILABLE_PAGE, status_code=503, headers=RECALL_MEDIA_SECURITY_HEADERS)
    # This branded shell is static and safe to serve even if audio settings are
    # incomplete; the authenticated websocket owns configuration validation.
    return HTMLResponse(page, headers=RECALL_MEDIA_SECURITY_HEADERS)


@app.websocket("/recall/audio")
async def recall_audio(websocket: WebSocket) -> None:
    """Authenticate via a scoped first frame, then stream live 48/24 kHz PCM."""
    await websocket.accept()
    reserved_id: str | None = None
    handshake_slot = False
    stage = "handshake"
    try:
        try:
            await asyncio.wait_for(recall_handshakes.acquire(), timeout=0.5)
            handshake_slot = True
        except asyncio.TimeoutError:
            await websocket.close(code=1013)
            return
        stage = "configuration"
        settings = RecallSettings.from_env()
        stage = "handshake"
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=8.0)
        auth = json.loads(raw)
        if not isinstance(auth, dict):
            await websocket.close(code=1008)
            return
        ticket = str(auth.get("session") or "") if auth.get("type") == "authenticate" else ""
        stage = "ticket_verification"
        claims = valid_recall_ticket(ticket, settings.bridge_secret)
        if not claims:
            recall_metrics.invalid_ticket_rejections += 1
            LOG.warning("Recall media ticket rejected", extra={"stage": stage})
            await websocket.close(code=1008, reason="invalid_ticket")
            return
        # The short-lived page token is not sufficient by itself: check that
        # Chusky's verified Recall webhook has moved this owner's bot in-call.
        # This prevents pre-join use and replay after the meeting ends.
        stage = "media_authorization"
        pending_notified = False
        authorization_reason = ""
        authorization_code = ""
        authorized_meeting: tuple[MeetingMode, str] | None = None
        authorized_language: tuple[str, list[str], list[str]] = ("english", [], [])
        authorized_tts_model = settings.tts_model
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=2.0)) as client:
            async def check_media_authorization() -> int:
                nonlocal pending_notified, authorized_meeting, authorized_language, authorized_tts_model, authorization_reason, authorization_code
                response = await client.post(
                    settings.media_authorize_url,
                    headers={"Authorization": f"Bearer {settings.bridge_secret}"},
                    json={"meetingId": claims["meetingId"], "userId": claims["userId"]},
                )
                if response.status_code == 200:
                    authorization_payload = response.json()
                    authorized_meeting = parse_meeting_media_authorization(
                        authorization_payload, str(claims.get("interactionMode", "addressed")),
                    )
                    authorized_language = parse_meeting_language_authorization(authorization_payload)
                    authorized_tts_model = parse_meeting_tts_model(authorization_payload, settings.tts_model)
                if response.status_code == 425 and not pending_notified:
                    pending_notified = True
                    recall_metrics.media_authorization_waits += 1
                    await websocket.send_json({"type": "status", "code": "meeting_not_ready"})
                elif response.status_code >= 400:
                    try:
                        payload = response.json()
                        if isinstance(payload, dict):
                            if isinstance(payload.get("code"), str):
                                authorization_code = payload["code"][:80]
                            if isinstance(payload.get("reason"), str):
                                authorization_reason = payload["reason"][:320]
                    except (ValueError, TypeError):
                        # Older/stale root deployments returned a plain 404
                        # body. Do not mislabel that as a missing meeting;
                        # give the participant a safe deployment diagnosis.
                        pass
                    if response.status_code == 404 and not authorization_reason:
                        authorization_code = "media_authorize_route_unavailable"
                        authorization_reason = "Chusky's meeting service could not verify this session. Rejoin after the service finishes deploying."
                return response.status_code

            # Recall webhooks and the provider's retrieve endpoint can briefly
            # disagree while a bot is being admitted. Keep the page pending
            # long enough for that normal race to settle, while still bounding
            # startup so a genuinely dead bot does not hang forever.
            authorization_status = await wait_for_media_authorization(check_media_authorization, timeout_seconds=45.0)
        if authorization_status not in (200, 204):
            if authorization_status == 425:
                recall_metrics.media_authorization_timeouts += 1
                LOG.warning("Recall media authorization timed out waiting for active meeting", extra={"stage": stage, "http_status": 425})
                await websocket.close(code=1013, reason="meeting_not_ready")
            elif authorization_status in (404, 410):
                recall_metrics.media_authorization_rejections += 1
                LOG.warning(
                    "Recall media authorization denied",
                    extra={"stage": stage, "http_status": authorization_status, "reason_code": authorization_code or "meeting_unavailable"},
                )
                if authorization_reason:
                    await websocket.send_json({"type": "status", "code": "meeting_unavailable", "message": authorization_reason})
                await websocket.close(code=1008, reason="meeting_unavailable")
            elif authorization_status == 401:
                recall_metrics.media_authorization_rejections += 1
                LOG.error("Recall media bridge authentication rejected", extra={"stage": stage, "http_status": authorization_status})
                await websocket.close(code=1011, reason="bridge_auth_failed")
            else:
                recall_metrics.media_authorization_rejections += 1
                LOG.warning(
                    "Recall media authorization service rejected request",
                    extra={"stage": stage, "http_status": authorization_status, "reason_code": authorization_code or "authorization_failed"},
                )
                await websocket.close(code=1011, reason="media_authorization_failed")
            return
        if authorized_meeting is None:
            authorized_meeting = parse_meeting_media_authorization(None, str(claims.get("interactionMode", "addressed")))
        claims["interactionMode"] = authorized_meeting[0]
        claims["languageMode"], claims["languageHints"], claims["keyterms"] = authorized_language
        greeting = authorized_meeting[1]
        if auth.get("reconnect") is True:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                    response = await client.post(
                        settings.commit_turn_url,
                        headers={"Authorization": f"Bearer {settings.bridge_secret}"},
                        json={
                            "meetingId": claims["meetingId"],
                            "userId": claims["userId"],
                            "turnId": f"reconnect-{uuid.uuid4().hex[:24]}",
                            "speak": False,
                            "runtimeState": "reconnecting",
                        },
                    )
                    response.raise_for_status()
            except Exception:
                LOG.info("Recall reconnect state could not be persisted", extra={"meeting_id": claims["meetingId"]})
        stage = "meeting_capacity"
        if not await recall_meetings.reserve(claims["meetingId"], settings.max_active_meetings):
            await websocket.close(code=1013)
            return
        reserved_id = claims["meetingId"]
        recall_metrics.websocket_sessions += 1
        await websocket.send_json({"type": "authenticated"})
        recall_handshakes.release()
        handshake_slot = False
        stage = "voice_session"
        await RecallVoiceSession(claims, websocket, settings, recall_metrics, play_intro=not bool(auth.get("reconnect")), greeting=greeting, tts_model=authorized_tts_model).run()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # Do not emit exception details that could contain credential URLs or
        # request metadata. Aggregate health is available separately.
        recall_metrics.initialization_failures += 1
        error_type = type(exc).__name__
        response = getattr(exc, "response", None)
        http_status = getattr(response, "status_code", None)
        configuration_fields = getattr(exc, "fields", ())
        LOG.warning(
            "Recall media session could not be initialized",
            extra={
                "stage": stage,
                "error_type": error_type,
                **({"configuration_issue": exc.code} if isinstance(exc, RecallConfigurationError) else {}),
                **({"configuration_fields": list(configuration_fields)} if configuration_fields else {}),
                **({"http_status": http_status} if http_status else {}),
            },
        )
        try:
            await websocket.close(code=1011, reason="voice_service_unavailable")
        except Exception:
            pass
    finally:
        if handshake_slot:
            recall_handshakes.release()
        if reserved_id:
            await recall_meetings.release(reserved_id)


@app.websocket("/recall/video")
async def recall_video(websocket: WebSocket) -> None:
    """Receive Recall's signed PNG stream and forward sampled screenshares only."""
    accepted = False
    handshake_slot = False
    stage = "configuration"
    try:
        settings = RecallSettings.from_env()
        if not valid_visual_handoff_url(settings.visual_frame_url) or visual_configuration_status(settings.visual_frame_url, settings.realtime_secret) != "configured":
            await websocket.close(code=1011, reason="visual_context_not_configured")
            return
        if websocket.url.path != "/recall/video" or websocket.url.query:
            await websocket.close(code=1008, reason="invalid_endpoint")
            return
        stage = "signature_verification"
        try:
            await asyncio.wait_for(recall_handshakes.acquire(), timeout=0.5)
            handshake_slot = True
        except asyncio.TimeoutError:
            await websocket.close(code=1013, reason="server_busy")
            return
        if not verify_recall_websocket_signature(settings.realtime_secret, websocket.headers):
            await websocket.close(code=1008, reason="invalid_recall_signature")
            return
        await websocket.accept()
        accepted = True
        recall_metrics.screen_share_websocket_sessions += 1
        # Do not hold the shared audio/video handshake semaphore for the life
        # of this long-lived websocket; it could otherwise block call audio.
        recall_handshakes.release()
        handshake_slot = False
        sampler = RecallScreenShareSampler(min_interval_seconds=2.5)
        stage = "receiving_frames"
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            while True:
                raw = await websocket.receive_text()
                if len(raw.encode("utf-8")) > 2_050_000:
                    recall_metrics.screen_share_frames_dropped += 1
                    await websocket.close(code=1009, reason="frame_too_large")
                    return
                try:
                    event = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    recall_metrics.screen_share_frames_dropped += 1
                    continue
                frame = parse_screenshare_frame(event)
                if frame is None:
                    # Ignore webcam, audio, chat, and malformed provider events.
                    continue
                if not sampler.accept(frame["buffer"]):
                    continue
                recall_metrics.screen_share_frames_sampled += 1
                try:
                    response = await client.post(
                        settings.visual_frame_url,
                        headers={"Authorization": f"Bearer {settings.bridge_secret}"},
                        json={
                            "meetingId": frame["meeting_id"],
                            "userId": frame["user_id"],
                            "providerBotId": frame["provider_bot_id"],
                            "frameBase64": frame["buffer"],
                        },
                    )
                    if response.status_code == 202:
                        recall_metrics.screen_share_frames_forwarded += 1
                    elif response.status_code in (425, 429) or response.status_code >= 500:
                        # The meeting-status webhook can lag video startup, and
                        # Redis/bridge failures can be transient. Retry the same
                        # static slide on a later provider frame, still subject
                        # to the sampler and Redis rate limit.
                        sampler.retry()
                        recall_metrics.screen_share_frames_dropped += 1
                    else:
                        recall_metrics.screen_share_frames_dropped += 1
                        if response.status_code in (401, 404):
                            await websocket.close(code=1008, reason="meeting_not_authorized")
                            return
                except httpx.HTTPError as exc:
                    recall_metrics.screen_share_frames_dropped += 1
                    LOG.warning(
                        "Recall shared-screen handoff failed",
                        extra={"stage": stage, "error_type": type(exc).__name__},
                    )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        LOG.warning(
            "Recall shared-screen websocket ended",
            extra={"stage": stage, "error_type": type(exc).__name__},
        )
        if accepted:
            try:
                await websocket.close(code=1011, reason="visual_stream_unavailable")
            except Exception:
                pass
    finally:
        if handshake_slot:
            recall_handshakes.release()
        if accepted:
            recall_metrics.screen_share_websocket_sessions = max(0, recall_metrics.screen_share_websocket_sessions - 1)


@app.websocket("/twilio/stream")
async def twilio_stream(websocket: WebSocket) -> None:
    """Receive a signed Twilio bidirectional Media Stream.

    The WebSocket itself carries no browser/client authorization. Chusky's
    TwiML route embeds an expiring HMAC ticket as a Stream parameter, which is
    checked only after Twilio's initial `start` event arrives.
    """
    try:
        settings = Settings.from_env()
    except RuntimeError:
        await websocket.close(code=1011)
        return
    if not valid_twilio_websocket(websocket, settings):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        start_event: dict[str, Any] | None = None
        for _ in range(3):
            event = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=5))
            if event.get("event") == "start":
                start_event = event
                break
        if not start_event:
            await websocket.close(code=1008)
            return
        start = start_event.get("start") or {}
        params = start.get("customParameters") or {}
        call_id = str(params.get("callId") or "").strip()
        try:
            user_id = int(str(params.get("userId") or ""))
        except ValueError:
            user_id = 0
        ticket = str(params.get("ticket") or "")
        tts_model = str(params.get("ttsModel") or "").strip() or None
        stream_sid = str(start.get("streamSid") or start_event.get("streamSid") or "").strip()
        media_format = start.get("mediaFormat") or {}
        if (not call_id.startswith("twc_") or user_id <= 0 or not stream_sid
                or media_format.get("encoding") != "audio/x-mulaw"
                or media_format.get("sampleRate") != 8000
                or media_format.get("channels") != 1
                or not valid_twilio_ticket(call_id, user_id, ticket, settings.bridge_secret, tts_model)
                or (tts_model is not None and not re.fullmatch(r"flux-[a-z]+-en", tts_model))):
            await websocket.close(code=1008)
            return
        if not await calls.reserve_twilio(call_id, settings):
            await websocket.close(code=1013)
            return
        metrics.twilio_started += 1
        try:
            await TwilioVoiceCall(call_id, user_id, stream_sid, websocket, settings, metrics, tts_model=tts_model).run()
        finally:
            await calls.release_twilio(call_id)
    except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError):
        try:
            await websocket.close(code=1008)
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        # Railway injects PORT and routes public traffic to it. Keep the
        # explicit bridge port as a local/Oracle fallback only.
        host=os.getenv("VOICE_BRIDGE_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT") or os.getenv("VOICE_BRIDGE_PORT", "3004")),
        proxy_headers=True,
        # Recall sends bounded JSON envelopes containing base64 PNG frames.
        # Keep the server-side WebSocket limit close to our application limit.
        ws_max_size=2_100_000,
    )
