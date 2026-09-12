"""Chusky's server-side voice media bridge.

This service accepts an already-authorized call handoff from Chusky, joins the
short-lived Agora room supplied by Sendblue, streams 16 kHz PCM to Deepgram,
and speaks Chusky's response back into the room. It intentionally stores no
audio, Agora token, or caller phone number. Chusky may retain bounded text
turns in the owner's existing private conversation history.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
import hashlib
import hmac
import json
import logging
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
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect
from audio_formats import (
    deepgram_linear16_to_twilio_mulaw,
    twilio_deepgram_listen_url,
    twilio_deepgram_speak_url,
    twilio_mulaw_to_deepgram_linear16,
)
from latency import latency_summary, resolve_speculative_draft, take_tts_chunk

LOG = logging.getLogger("chusky.voice_bridge")
logging.basicConfig(level=os.getenv("VOICE_BRIDGE_LOG_LEVEL", "INFO"))
load_dotenv(Path(__file__).with_name(".env"))
SAMPLE_RATE = 16_000
CHANNELS = 1
BYTES_PER_MS = SAMPLE_RATE * CHANNELS * 2 // 1000
PCM_CHUNK_BYTES = BYTES_PER_MS * 20


def normalize_voice_text(value: str) -> str:
    """Remove Markdown syntax before text reaches Deepgram TTS or history."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```[^\n]*\n?([\s\S]*?)```", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\((https?://[^\s)]+)\)", r"\1: \2", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1: \2", text, flags=re.I)
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
    return re.sub(r"\n{3,}", "\n\n", text.replace("*", "").replace("#", "")).strip()


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
    barge_in_min_chars: int
    greeting: str

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.getenv("FACETIME_MEDIA_BRIDGE_SECRET", "").strip()
        deepgram = os.getenv("DEEPGRAM_API_KEY", "").strip()
        turn_url = os.getenv("CHUSKY_VOICE_TURN_URL", "http://127.0.0.1:3003/internal/facetime/turn").strip()
        status_url = os.getenv("CHUSKY_VOICE_STATUS_URL", "http://127.0.0.1:3003/internal/facetime/status").strip()
        if not secret or not deepgram or not turn_url.startswith(("http://", "https://")) or not turn_url.endswith("/turn") or not status_url.startswith(("http://", "https://")):
            raise RuntimeError("FACETIME_MEDIA_BRIDGE_SECRET, DEEPGRAM_API_KEY, CHUSKY_VOICE_TURN_URL ending in /turn, and CHUSKY_VOICE_STATUS_URL are required")
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
            max(1, min(int(os.getenv("VOICE_BARGE_IN_MIN_CHARS", "2")), 100)),
            os.getenv("VOICE_GREETING", "Hi, this is Chusky. How can I help?").strip()[:500],
        )


class AgoraCredentials(BaseModel):
    appId: str = Field(min_length=1, max_length=300)
    channelName: str = Field(min_length=1, max_length=300)
    token: str = Field(min_length=1, max_length=4000)
    uid: int = Field(ge=0)


class StartCall(BaseModel):
    callId: str = Field(pattern=r"^ftc_[0-9a-fA-F-]{36}$")
    userId: int = Field(gt=0)
    phoneNumber: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    purpose: str = Field(min_length=1, max_length=1000)
    agora: AgoraCredentials


class CallerAudioObserver:  # Base class is added dynamically after Agora imports.
    pass


def valid_twilio_ticket(call_id: str, user_id: int, ticket: str, secret: str) -> bool:
    """Verify the short-lived HMAC ticket minted by Chusky's signed TwiML route."""
    try:
        expires, supplied = ticket.split(".", 1)
        expires_at = int(expires)
    except (TypeError, ValueError):
        return False
    if expires_at < int(time.time() * 1000) or expires_at > int(time.time() * 1000) + 6 * 60_000:
        return False
    payload = f"{call_id}.{user_id}.{expires_at}".encode()
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


def valid_twilio_websocket(websocket: WebSocket, settings: Settings) -> bool:
    """Validate Twilio's signed WSS handshake using its official SDK helper.

    Twilio documents a trailing-slash retry for WebSocket validation; we retain
    the short-lived Chusky ticket as a second independent authorization check.
    """
    signature = websocket.headers.get("x-twilio-signature", "")
    if not signature or not settings.twilio_auth_token or not settings.twilio_media_stream_url:
        return False
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(settings.twilio_auth_token)
        return validator.validate(settings.twilio_media_stream_url, {}, signature) or validator.validate(f"{settings.twilio_media_stream_url}/", {}, signature)
    except Exception:
        LOG.exception("Twilio WebSocket signature validation could not run")
        return False


def load_agora_observer(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[bytes]):
    """Load the optional native SDK only when a live call starts."""
    from agora.rtc.audio_frame_observer import IAudioFrameObserver  # type: ignore[import-not-found]

    class Observer(IAudioFrameObserver):
        def _enqueue(self, audio: bytes) -> None:
            def put() -> None:
                if not queue.full():
                    queue.put_nowait(audio)
            loop.call_soon_threadsafe(put)

        def on_record_audio_frame(self, *_args: Any) -> int:
            return 0

        def on_playback_audio_frame(self, *_args: Any) -> int:
            return 0

        def on_ear_monitoring_audio_frame(self, *_args: Any) -> int:
            return 0

        def on_playback_audio_frame_before_mixing(self, _local_user: Any, _channel_id: str, _uid: str, frame: Any, *_args: Any) -> int:
            # Agora is configured below to provide exactly 16 kHz mono 16-bit PCM.
            if frame.samples_per_sec == SAMPLE_RATE and frame.channels == CHANNELS and frame.bytes_per_sample == 2 and frame.buffer:
                self._enqueue(bytes(frame.buffer))
            return 1

        def on_get_audio_frame_position(self, *_args: Any) -> int:
            return 0

    return Observer()


class VoiceCall:
    def __init__(self, request: StartCall, settings: Settings) -> None:
        self.request = request
        self.settings = settings
        self.session_id = f"vbr_{uuid.uuid4()}"
        self.stop = asyncio.Event()
        self.audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)  # bounded: roughly ten seconds.
        self.connection: Any | None = None
        self.service: Any | None = None
        self.response_task: asyncio.Task[None] | None = None
        self.tts_socket: Any | None = None
        self.tts_lock = asyncio.Lock()
        self.finalized_turn_indexes: set[int] = set()

    async def run(self) -> None:
        try:
            self._connect_agora()
            await self._notify_status("active")
            await asyncio.wait_for(self._run_transcription(), timeout=self.settings.max_call_seconds)
            await self._notify_status("ended")
        except asyncio.TimeoutError:
            LOG.info("Voice call reached maximum duration", extra={"call_id": self.request.callId})
            await self._notify_status("ended")
        except Exception:
            LOG.exception("Voice call ended with an error", extra={"call_id": self.request.callId})
            await self._notify_status("failed", "media bridge connection or processing failed")
        finally:
            self.stop.set()
            if self.response_task and not self.response_task.done():
                self.response_task.cancel()
                await asyncio.gather(self.response_task, return_exceptions=True)
            self._release_agora()

    def _connect_agora(self) -> None:
        # Exact configuration follows Agora's Python Server SDK PCM receive/send example.
        from agora.rtc.agora_service import AgoraService, AgoraServiceConfig  # type: ignore[import-not-found]
        from agora.rtc.agora_base import (  # type: ignore[import-not-found]
            AudioProfileType, AudioPublishType, AudioScenarioType, AudioSubscriptionOptions,
            RTCConnConfig, RtcConnectionPublishConfig, VideoPublishType,
        )

        loop = asyncio.get_running_loop()
        service = AgoraService()
        result = service.initialize(AgoraServiceConfig(
            appid=self.request.agora.appId,
            enable_audio_processor=1,
            enable_audio_device=0,
            enable_video=0,
        ))
        if result != 0:
            raise RuntimeError(f"Agora service initialization failed ({result})")
        connection = service.create_rtc_connection(
            RTCConnConfig(
                auto_subscribe_audio=1,
                auto_subscribe_video=0,
                audio_recv_media_packet=0,
                audio_subs_options=AudioSubscriptionOptions(packet_only=0, pcm_data_only=1, bytes_per_sample=2, number_of_channels=1, sample_rate_hz=SAMPLE_RATE),
            ),
            RtcConnectionPublishConfig(
                audio_profile=AudioProfileType.AUDIO_PROFILE_DEFAULT,
                audio_scenario=AudioScenarioType.AUDIO_SCENARIO_AI_SERVER,
                is_publish_audio=True,
                is_publish_video=False,
                audio_publish_type=AudioPublishType.AUDIO_PUBLISH_TYPE_PCM,
                video_publish_type=VideoPublishType.VIDEO_PUBLISH_TYPE_NONE,
            ),
        )
        if connection is None:
            service.release()
            raise RuntimeError("Agora connection could not be created")
        if connection.connect(self.request.agora.token, self.request.agora.channelName, str(self.request.agora.uid)) != 0:
            connection.release()
            service.release()
            raise RuntimeError("Agora channel connection failed")
        local_user = connection.get_local_user()
        # Must be configured before registering the frame observer.
        local_user.set_playback_audio_frame_before_mixing_parameters(CHANNELS, SAMPLE_RATE)
        if connection.register_audio_frame_observer(load_agora_observer(loop, self.audio), 0, None) != 0:
            connection.disconnect()
            connection.release()
            service.release()
            raise RuntimeError("Agora audio observer registration failed")
        if connection.publish_audio() != 0:
            connection.disconnect()
            connection.release()
            service.release()
            raise RuntimeError("Agora audio publish failed")
        self.connection, self.service = connection, service

    async def _run_transcription(self) -> None:
        if not self.settings.stt_model.startswith("flux-"):
            raise RuntimeError("VOICE_STT_MODEL must be a Deepgram Flux conversational model, for example flux-general-en")
        query = (
            f"model={self.settings.stt_model}&encoding=linear16&sample_rate=16000"
            f"&eager_eot_threshold={self.settings.stt_eager_eot_threshold}"
            f"&eot_threshold={self.settings.stt_eot_threshold}"
            f"&eot_timeout_ms={self.settings.stt_eot_timeout_ms}"
        )
        url = f"wss://api.deepgram.com/v2/listen?{query}"
        async with connect(url, additional_headers={"Authorization": f"Token {self.settings.deepgram_api_key}"}, max_size=1_000_000) as socket:
            sender = asyncio.create_task(self._send_audio(socket))
            receiver = asyncio.create_task(self._receive_transcripts(socket))
            stopper = asyncio.create_task(self.stop.wait())
            done, pending = await asyncio.wait({sender, receiver, stopper}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()

    async def _send_audio(self, socket: Any) -> None:
        while not self.stop.is_set():
            audio = await self.audio.get()
            await socket.send(audio)

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
                await self._barge_in()
            elif turn_event == "EndOfTurn" and transcript and turn_index not in self.finalized_turn_indexes:
                self.finalized_turn_indexes.add(turn_index)
                if self.response_task and not self.response_task.done():
                    self.response_task.cancel()
                    await asyncio.gather(self.response_task, return_exceptions=True)
                self.response_task = asyncio.create_task(self._respond(transcript), name=f"facetime-response-{self.request.callId}-{turn_index}")

    async def _barge_in(self) -> None:
        if not self.response_task or self.response_task.done():
            return
        if self.tts_socket is not None:
            try:
                async with self.tts_lock:
                    await self.tts_socket.send(json.dumps({"type": "Interrupt"}))
            except Exception:
                pass
        self.response_task.cancel()
        await asyncio.gather(self.response_task, return_exceptions=True)
        self.response_task = None

    async def _respond(self, transcript: str) -> None:
        # The bridge has no agent, memory, or tool credentials. Chusky owns all of that.
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            response = await client.post(self.settings.chusky_turn_url, headers={"Authorization": f"Bearer {self.settings.bridge_secret}"}, json={"callId": self.request.callId, "userId": self.request.userId, "transcript": transcript})
            response.raise_for_status()
            text = str(response.json().get("text") or "").strip()
        if text:
            await self._speak(text[:5000])

    async def _notify_status(self, status: str, error: str | None = None) -> None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                response = await client.post(self.settings.chusky_status_url, headers={"Authorization": f"Bearer {self.settings.bridge_secret}"}, json={"callId": self.request.callId, "userId": self.request.userId, "status": status, **({"error": error} if error else {})})
                response.raise_for_status()
        except Exception:
            # The bridge must still clean up its Agora resources if Chusky is restarting.
            LOG.warning("Could not report call status", extra={"call_id": self.request.callId, "status": status})

    async def _speak(self, text: str) -> None:
        if not self.settings.tts_model.startswith("flux-"):
            raise RuntimeError("VOICE_TTS_MODEL must be a Flux streaming model (for example flux-haley-en)")
        url = f"wss://api.deepgram.com/v2/speak?model={self.settings.tts_model}&encoding=linear16&sample_rate=16000"
        async with connect(url, additional_headers={"Authorization": f"Token {self.settings.deepgram_api_key}"}, max_size=1_000_000) as socket:
            self.tts_socket = socket
            try:
                async with self.tts_lock:
                    await socket.send(json.dumps({"type": "Speak", "text": text}))
                    await socket.send(json.dumps({"type": "Flush"}))
                async for raw in socket:
                    if isinstance(raw, bytes):
                        if self.stop.is_set() or self.connection is None:
                            return
                        # Flux returns raw PCM frames. Push them as soon as
                        # they arrive instead of waiting for a complete file.
                        remainder = len(raw) % BYTES_PER_MS
                        if remainder:
                            raw += b"\x00" * (BYTES_PER_MS - remainder)
                        for offset in range(0, len(raw), PCM_CHUNK_BYTES):
                            if self.stop.is_set() or self.connection is None:
                                return
                            while not self.stop.is_set() and not self.connection.is_push_to_rtc_completed():
                                await asyncio.sleep(0.01)
                            if self.stop.is_set():
                                return
                            chunk = memoryview(raw[offset:offset + PCM_CHUNK_BYTES])
                            if self.connection.push_audio_pcm_data(chunk, SAMPLE_RATE, CHANNELS) != 0:
                                raise RuntimeError("Agora rejected synthesized PCM audio")
                            await asyncio.sleep(len(chunk) / (SAMPLE_RATE * CHANNELS * 2))
                    elif isinstance(raw, str) and json.loads(raw).get("type") == "SpeechMetadata":
                        return
            finally:
                self.tts_socket = None

    def _release_agora(self) -> None:
        if self.connection is not None:
            try:
                self.connection.disconnect()
                self.connection.release()
            except Exception:
                LOG.exception("Agora connection cleanup failed", extra={"call_id": self.request.callId})
            self.connection = None
        if self.service is not None:
            try:
                self.service.release()
            except Exception:
                LOG.exception("Agora service cleanup failed", extra={"call_id": self.request.callId})
            self.service = None


@dataclass(frozen=True)
class VoiceTurnResult:
    text: str
    cost: float


class TwilioVoiceCall:
    """Twilio bidirectional Media Stream transport.

    Twilio sends and accepts base64 `audio/x-mulaw` at 8 kHz. The bridge
    converts at the provider boundary to Deepgram's configured 48 kHz input
    and 24 kHz output formats; Chusky remains the sole agent/LLM runtime.
    """
    def __init__(self, call_id: str, user_id: int, stream_sid: str, websocket: WebSocket, settings: Settings, metrics: "BridgeMetrics") -> None:
        self.call_id, self.user_id, self.stream_sid = call_id, user_id, stream_sid
        self.websocket, self.settings = websocket, settings
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
        self.draft_task: asyncio.Task[VoiceTurnResult] | None = None
        self.draft_transcript = ""
        self.draft_turn_index: int | None = None
        self.finalized_turn_indexes: set[int] = set()
        self.eager_end_times: dict[int, float] = {}
        self.response_started_at = 0.0
        self.tts_socket: Any | None = None
        self.tts_lock = asyncio.Lock()
        self.tts_reader_task: asyncio.Task[None] | None = None
        self.tts_done_event: asyncio.Event | None = None
        self.tts_first_audio_recorded = False
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
            linear16, self.stt_resample_state = twilio_mulaw_to_deepgram_linear16(
                twilio_frame,
                self.stt_resample_state,
            )
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
                if self.response_task and not self.response_task.done():
                    self.response_task.cancel()
                    await asyncio.gather(self.response_task, return_exceptions=True)
                self.interrupted = False
                self.response_started_at = time.monotonic()
                self.response_task = asyncio.create_task(self._respond_final(transcript, turn_index), name=f"twilio-response-{self.call_id}-{turn_index}")
                self.response_task.add_done_callback(self._observe_response_task)

    async def _start_draft(self, transcript: str, turn_index: int) -> None:
        if self.draft_task and not self.draft_task.done():
            if self.draft_turn_index == turn_index and self._same_transcript(self.draft_transcript, transcript):
                return
            self.draft_task.cancel()
            await asyncio.gather(self.draft_task, return_exceptions=True)
        self.draft_transcript, self.draft_turn_index = transcript, turn_index
        self.draft_task = asyncio.create_task(self._request_agent(transcript), name=f"twilio-draft-{self.call_id}-{turn_index}")

    def _observe_response_task(self, task: asyncio.Task[None]) -> None:
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
        active = [task for task in (self.response_task, self.draft_task) if task and not task.done()]
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
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        if self.response_task in active:
            self.response_task = None
        if self.draft_task in active:
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
        if not self.settings.tts_model.startswith("flux-"):
            raise RuntimeError("VOICE_TTS_MODEL must be a Flux streaming model (for example flux-haley-en)")
        url = twilio_deepgram_speak_url(self.settings.tts_model)
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
                        first_audio_ms = int((time.monotonic() - self.response_started_at) * 1000)
                        self.metrics.tts_first_audio_ms_total += first_audio_ms
                        self.metrics.tts_first_audio_count += 1
                        self.metrics.end_of_turn_to_first_audio_samples.append(first_audio_ms)
                        self.tts_first_audio_recorded = True
                    twilio_audio, self.tts_resample_state = deepgram_linear16_to_twilio_mulaw(
                        raw,
                        self.tts_resample_state,
                    )
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

    async def _request_agent_stream(self, transcript: str) -> VoiceTurnResult:
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
                json={"callId": self.call_id, "userId": self.user_id, "transcript": transcript, "speculative": False},
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

    async def _respond_final(self, transcript: str, turn_index: int) -> None:
        try:
            matching_draft = bool(
                self.draft_task
                and self.draft_turn_index == turn_index
                and self._same_transcript(self.draft_transcript, transcript)
            )
            result = await resolve_speculative_draft(self.draft_task, transcript_matches=matching_draft)
            stream_spoke = result is None
            if result is None:
                if self.draft_task and not self.draft_task.done():
                    self.draft_task.cancel()
                    await asyncio.gather(self.draft_task, return_exceptions=True)
                result = await self._request_agent_stream(transcript)
            if not result.text or self.interrupted:
                return
            response = await self.http.post(
                f"{self.settings.chusky_turn_url[:-len('/turn')]}/commit-turn",
                headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                json={"callId": self.call_id, "userId": self.user_id, "transcript": transcript, "text": result.text, "cost": result.cost, "turnId": f"{turn_index}"},
                timeout=httpx.Timeout(15.0, connect=5.0),
            )
            response.raise_for_status()
            if not self.interrupted and not stream_spoke:
                await self._speak_persistent_text(result.text)
        except Exception:
            # A provider or bridge deployment may not have the new streaming
            # route yet. Keep the call usable through the proven batch path.
            if not self.interrupted:
                try:
                    await self._close_persistent_tts()
                    result = await self._request_agent(transcript)
                    if result.text:
                        response = await self.http.post(
                            f"{self.settings.chusky_turn_url[:-len('/turn')]}/commit-turn",
                            headers={"Authorization": f"Bearer {self.settings.bridge_secret}"},
                            json={"callId": self.call_id, "userId": self.user_id, "transcript": transcript, "text": result.text, "cost": result.cost, "turnId": f"{turn_index}"},
                            timeout=httpx.Timeout(15.0, connect=5.0),
                        )
                        response.raise_for_status()
                        await self._speak(result.text)
                except Exception:
                    self.metrics.agent_failures += 1
                    LOG.warning("Twilio streamed response failed", extra={"call_id": self.call_id})
        finally:
            self.draft_task, self.draft_transcript, self.draft_turn_index = None, "", None

    async def _speak(self, text: str) -> None:
        if not self.settings.tts_model.startswith("flux-"):
            raise RuntimeError("VOICE_TTS_MODEL must be a Flux streaming model (for example flux-haley-en)")
        url = twilio_deepgram_speak_url(self.settings.tts_model)
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
                        twilio_audio, resample_state = deepgram_linear16_to_twilio_mulaw(
                            raw,
                            resample_state,
                        )
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
    tts_first_audio_count: int = 0
    tts_first_audio_ms_total: int = 0
    flux_eager_to_final_samples: deque[int] = field(default_factory=lambda: deque(maxlen=256))
    agent_first_delta_samples: deque[int] = field(default_factory=lambda: deque(maxlen=256))
    end_of_turn_to_first_audio_samples: deque[int] = field(default_factory=lambda: deque(maxlen=256))

    def snapshot(self, active_twilio: int, active_facetime: int) -> dict[str, Any]:
        return {
            "twilio": {
                "active": active_twilio,
                "started": self.twilio_started,
                "completed": self.twilio_completed,
                "failed": self.twilio_failed,
                "bargeIns": self.barge_ins,
                "droppedInboundFrames": self.dropped_inbound_frames,
                "agentFailures": self.agent_failures,
                "averageAgentTurnMs": round(self.agent_turn_ms_total / self.agent_turns) if self.agent_turns else None,
                "averageTtsFirstAudioMs": round(self.tts_first_audio_ms_total / self.tts_first_audio_count) if self.tts_first_audio_count else None,
                "latencyMs": {
                    "fluxEagerToFinal": latency_summary(list(self.flux_eager_to_final_samples)),
                    "agentFirstDelta": latency_summary(list(self.agent_first_delta_samples)),
                    "endOfTurnToFirstAudio": latency_summary(list(self.end_of_turn_to_first_audio_samples)),
                },
            },
            "facetime": {"active": active_facetime},
        }


class CallManager:
    def __init__(self) -> None:
        self.calls: dict[str, VoiceCall] = {}
        self.twilio_calls: set[str] = set()
        self.lock = asyncio.Lock()

    async def start(self, request: StartCall, settings: Settings) -> VoiceCall:
        async with self.lock:
            existing = self.calls.get(request.callId)
            if existing:
                return existing
            if len(self.calls) + len(self.twilio_calls) >= settings.max_active_calls:
                raise RuntimeError("voice bridge call capacity reached")
            call = VoiceCall(request, settings)
            self.calls[request.callId] = call
            task = asyncio.create_task(call.run(), name=f"facetime-{request.callId}")
            task.add_done_callback(lambda _task: self.calls.pop(request.callId, None))
            return call

    async def reserve_twilio(self, call_id: str, settings: Settings) -> bool:
        async with self.lock:
            if call_id in self.twilio_calls or len(self.calls) + len(self.twilio_calls) >= settings.max_active_calls:
                return False
            self.twilio_calls.add(call_id)
            return True

    async def release_twilio(self, call_id: str) -> None:
        async with self.lock:
            self.twilio_calls.discard(call_id)


app = FastAPI(title="Chusky Voice Media Bridge", docs_url=None, redoc_url=None)
calls = CallManager()
metrics = BridgeMetrics()


def authenticate(authorization: str | None, settings: Settings) -> None:
    expected = f"Bearer {settings.bridge_secret}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        settings = Settings.from_env()
        twilio_ready = bool(settings.twilio_auth_token and settings.twilio_media_stream_url.startswith("wss://"))
        return {"ok": True, "checks": {"twilioWebSocket": "configured" if twilio_ready else "misconfigured", "fluxStt": "configured" if settings.stt_model.startswith("flux-") else "misconfigured", "fluxTts": "configured" if settings.tts_model.startswith("flux-") else "misconfigured"}, "metrics": metrics.snapshot(len(calls.twilio_calls), len(calls.calls))}
    except RuntimeError:
        return {"ok": False, "checks": {"configuration": "misconfigured"}, "metrics": metrics.snapshot(len(calls.twilio_calls), len(calls.calls))}


@app.post("/calls", status_code=202)
async def start_call(request: StartCall, authorization: str | None = Header(default=None)) -> dict[str, str]:
    """Retired legacy FaceTime entry point; all calls now originate in Twilio."""
    raise HTTPException(status_code=410, detail="FaceTime calling is retired; use Twilio voice")


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
        stream_sid = str(start.get("streamSid") or start_event.get("streamSid") or "").strip()
        media_format = start.get("mediaFormat") or {}
        if (not call_id.startswith("twc_") or user_id <= 0 or not stream_sid
                or media_format.get("encoding") != "audio/x-mulaw"
                or media_format.get("sampleRate") != 8000
                or media_format.get("channels") != 1
                or not valid_twilio_ticket(call_id, user_id, ticket, settings.bridge_secret)):
            await websocket.close(code=1008)
            return
        if not await calls.reserve_twilio(call_id, settings):
            await websocket.close(code=1013)
            return
        metrics.twilio_started += 1
        try:
            await TwilioVoiceCall(call_id, user_id, stream_sid, websocket, settings, metrics).run()
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
    )
