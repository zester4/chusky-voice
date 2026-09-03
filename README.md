# Chusky FaceTime media bridge

This service is the private media participant for outbound Sendblue FaceTime
calls. Chusky starts the call and gives it short-lived Agora credentials. The
bridge joins that Agora room, receives 16 kHz PCM audio, sends final speech to
Chusky's authenticated internal agent endpoint, synthesizes the response with
Deepgram, and publishes PCM audio back to the call.

It is deliberately a separate process from Chusky. It never persists audio,
Agora credentials, or phone numbers. Chusky retains bounded text turns in the
owner's existing private conversation history so a call continues the same
context as their chat.

It also accepts a separate **Twilio bidirectional Media Stream** at
`/twilio/stream`. Twilio sends and receives base64 `audio/x-mulaw` at 8 kHz;
the bridge passes that codec directly to/from Deepgram and uses the same
private Chusky voice-turn route for memory and safe read-only agent behavior.

## Required environment

```ini
FACETIME_MEDIA_BRIDGE_SECRET=<same random secret configured in Chusky>
DEEPGRAM_API_KEY=<Deepgram server API key>
CHUSKY_VOICE_TURN_URL=http://127.0.0.1:3003/internal/facetime/turn
CHUSKY_VOICE_STATUS_URL=http://127.0.0.1:3003/internal/facetime/status
VOICE_BRIDGE_HOST=127.0.0.1
VOICE_BRIDGE_PORT=3004
VOICE_BRIDGE_MAX_ACTIVE_CALLS=4
```

## Oracle installation

```bash
cd ~/chusky/chusky-voice
cp .env.example .env
# Edit .env: set the same FACETIME_MEDIA_BRIDGE_SECRET as Chusky and a Deepgram key.
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
pm2 start ecosystem.config.cjs
pm2 save
curl -i http://127.0.0.1:3004/health
```

The Nginx virtual host for `voice.selithub.shop` must proxy `/` to
`http://127.0.0.1:3004`; it should not expose port 3004 publicly.

For Twilio Media Streams, preserve WebSocket upgrades:

```nginx
location / {
    proxy_pass http://127.0.0.1:3004;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_buffering off;
}
```

## Twilio telephone calls

Twilio credentials belong in Chusky's root `.env`. The bridge needs only the
Twilio Auth Token and public WSS URL for WebSocket signature validation:

```ini
TWILIO_VOICE_ENABLED=true
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_CALLER_ID=+<Twilio-verified caller ID>
TWILIO_WEBHOOK_BASE_URL=https://chusky.selithub.shop
TWILIO_MEDIA_STREAM_URL=wss://voice.selithub.shop/twilio/stream
TWILIO_INBOUND_ENABLED=true
TWILIO_INBOUND_OWNER_USER_ID=<your Telegram numeric user ID>
TWILIO_INBOUND_ALLOWED_CALLERS=+233550472834
```

Chusky validates Twilio's signed TwiML and status callbacks. Its TwiML sends a
short-lived HMAC ticket as a Stream parameter; the bridge rejects connections
without it. The bridge also validates Twilio's `x-twilio-signature` WSS
handshake with Twilio's official Python helper, including Twilio's documented
trailing-slash compatibility check. Neither credentials nor audio are sent to
a browser or persisted in Chusky.

Add these to `chusky-voice/.env`:

```ini
TWILIO_AUTH_TOKEN=<same root Twilio auth token>
TWILIO_MEDIA_STREAM_URL=wss://voice.selithub.shop/twilio/stream
VOICE_STT_MODEL=flux-general-en
VOICE_STT_EAGER_EOT_THRESHOLD=0.45
VOICE_STT_EOT_THRESHOLD=0.65
VOICE_STT_EOT_TIMEOUT_MS=1200
VOICE_TTS_MODEL=flux-haley-en
VOICE_BARGE_IN_MIN_CHARS=2
VOICE_GREETING=Hi, this is Chusky. How can I help?
```

The bridge uses Deepgram Flux conversational STT (`/v2/listen`) and streaming
Flux TTS in raw 8 kHz μ-law, which Twilio plays without transcoding. On
`EagerEndOfTurn` the bridge starts a private, read-only draft; `TurnResumed`
cancels it, and only the definitive `EndOfTurn` is committed to Chusky memory
and usage. This overlaps model time with end-of-turn detection without creating
duplicate history. When caller speech resumes while Chusky is speaking, the
bridge cancels the active response, sends Deepgram `Interrupt`, then Twilio
`clear`: this is barge-in. `mark` events are emitted after complete responses
for playback tracking. `/health` exposes only aggregate latency/error/barging
counters.

In the Twilio Console, set the purchased Twilio number's **A call comes in**
webhook to `https://chusky.selithub.shop/twilio/inbound`, method `POST`. The
route is deliberately private-first: it rejects any caller not listed in
`TWILIO_INBOUND_ALLOWED_CALLERS`. It maps approved calls to the configured
Telegram owner, so only that owner's Chusky memory is available during the
call. Add another caller only when you deliberately want that person to enter
the same private voice context.

## Safety boundary

`POST /calls` requires `Authorization: Bearer <FACETIME_MEDIA_BRIDGE_SECRET>`.
The bridge can call only `/internal/facetime/turn`,
`/internal/facetime/commit-turn`, and `/internal/facetime/status` with the
same secret. Chusky validates the call ID and owner, uses the owner's existing
memory, limits tools to read-only calls, and stores only committed text turns
in normal history.
