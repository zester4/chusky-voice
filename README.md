# Chusky voice bridge

This private service bridges Chusky to live voice calls. Twilio Media Streams
provide the telephone calling path; an optional legacy Sendblue FaceTime path
uses Agora. Chusky remains the agent brain: the bridge sends speech turns to
Chusky's authenticated internal endpoint and streams its responses back to the
caller using Deepgram.

The bridge runs separately from Chusky and does not replace its model, memory,
history, or tools. It does not persist raw audio, provider credentials, or phone
numbers. Chusky retains only committed text turns in the owner's existing
private conversation history so calls continue the same context as chat.

It also accepts a separate **Twilio bidirectional Media Stream** at
`/twilio/stream`. Twilio's wire format remains base64 `audio/x-mulaw` at 8 kHz.
For this route, the bridge decodes and resamples caller audio to 48 kHz linear16
for Deepgram Flux, then resamples Deepgram's 24 kHz linear16 speech and encodes
it back to Twilio's required 8 kHz μ-law. Chusky's authenticated backend remains
the only agent brain and supplies its existing history, memory, and tools.

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

## Railway deployment

This bridge is a separate Railway service from Chusky. Deploy the
`chusky-voice` GitHub repository, or point a new Railway service at this
repository's root. The included `railway.toml` starts `python app.py` and uses
`/health` as the healthcheck.

Generate a Railway domain for the bridge, for example:

```text
https://chusky-voice-production.up.railway.app
```

Add these variables to the bridge service:

```ini
FACETIME_MEDIA_BRIDGE_SECRET=<same random secret configured in Chusky>
DEEPGRAM_API_KEY=<Deepgram server API key>
CHUSKY_VOICE_TURN_URL=https://chusky.up.railway.app/internal/facetime/turn
CHUSKY_VOICE_STATUS_URL=https://chusky.up.railway.app/internal/facetime/status
VOICE_BRIDGE_HOST=0.0.0.0
```

Do not set a fixed `VOICE_BRIDGE_PORT` on Railway. The bridge uses Railway's
injected `PORT`; `VOICE_BRIDGE_PORT=3004` remains the local/Oracle fallback.

Generate the shared bridge secret in PowerShell. Run this once, then paste the
same output into both Railway services as `FACETIME_MEDIA_BRIDGE_SECRET`:

```powershell
$bytes = [byte[]]::new(32)
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$rng.Dispose()
[Convert]::ToBase64String($bytes)
```

On the Chusky Railway service, configure:

```ini
SENDBLUE_FACETIME_ENABLED=true
SENDBLUE_FACETIME_NUMBER=<Sendblue FaceTime-enabled number>
FACETIME_MEDIA_BRIDGE_URL=https://chusky-voice-production.up.railway.app
FACETIME_MEDIA_BRIDGE_SECRET=<the same generated secret>
```

For Twilio Media Streams, also set the bridge's `TWILIO_AUTH_TOKEN` and:

```ini
TWILIO_MEDIA_STREAM_URL=wss://chusky-voice-production.up.railway.app/twilio/stream
```

The Chusky service's `TWILIO_MEDIA_STREAM_URL` must use the same WSS URL. The
bridge's public domain must support WebSocket upgrades. Do not expose the
private bridge secret or place it in `.env.example`.

The Sendblue FaceTime bridge URL is not the Sendblue receive webhook. Normal
Sendblue messages still use:

```text
https://chusky.up.railway.app/sendblue/webhook
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
VOICE_STT_EOT_TIMEOUT_MS=800
VOICE_TTS_MODEL=flux-haley-en
VOICE_BARGE_IN_MIN_CHARS=2
VOICE_GREETING=Hi, this is Chusky. How can I help?
```

The bridge uses Deepgram Flux conversational STT (`/v2/listen`) at 48 kHz
linear16 and streaming Flux TTS (`/v2/speak`) at 24 kHz linear16 for Twilio.
The bridge converts Twilio's 8 kHz μ-law frames to the STT format, and converts
TTS frames back to Twilio's required 8 kHz μ-law. Resampling state is preserved
across frames. Upsampling the telephone audio does not restore detail that was
not present in Twilio's 8 kHz source; this format choice is not itself a promise
of lower end-to-end latency. The optional Agora/FaceTime path retains its
separate audio settings and is unaffected by the Twilio conversion.

On `EagerEndOfTurn` the bridge starts a private, read-only draft; `TurnResumed`
cancels it, and only the definitive `EndOfTurn` is committed to Chusky memory
and usage. This overlaps model time with end-of-turn detection without creating
duplicate history. When caller speech resumes while Chusky is speaking, the
bridge cancels the active response, sends Deepgram `Interrupt`, then Twilio
`clear`: this is barge-in. `mark` events are emitted after complete responses
for playback tracking. `/health` exposes only aggregate latency/error/barging
counters.

The Twilio health response also reports bounded rolling p50/p95 timings under
`metrics.twilio.latencyMs`: `fluxEagerToFinal` measures the Flux confirmation
window, `agentFirstDelta` measures bridge-to-first streamed Chusky text, and
`endOfTurnToFirstAudio` measures caller-finished-to-first-audio. These contain
timing samples only, not transcripts or phone numbers. Use them before changing
Flux thresholds or the voice model so tuning targets the actual slow stage.
They measure separate parts of the live path; use real calls to evaluate
end-to-end responsiveness because local audio-conversion tests do not measure
Twilio, Deepgram, network, or model latency.

In the Twilio Console, set the purchased Twilio number's **A call comes in**
webhook to `https://chusky.selithub.shop/twilio/inbound`, method `POST`. The
route is deliberately private-first: it rejects any caller not listed in
`TWILIO_INBOUND_ALLOWED_CALLERS`. It maps approved calls to the configured
Telegram owner, so only that owner's Chusky memory is available during the
call. Add another caller only when you deliberately want that person to enter
the same private voice context.

## Run the bridge tests

With the bridge dependencies installed in the active Python environment, run
from the repository root:

```bash
python -m unittest discover -s tests -v
```

The tests cover Twilio/Deepgram sample-rate contracts, stateful frame
conversion, the Twilio WebSocket audio boundary, and latency helper behavior.
They use mocked provider sockets and do not place a real call. To measure
end-to-end latency, make a test call and inspect the aggregate p50/p95 metrics
at `/health`; never enable transcript or raw-audio logging for this purpose.

## Safety boundary

`POST /calls` requires `Authorization: Bearer <FACETIME_MEDIA_BRIDGE_SECRET>`.
The bridge can call only `/internal/facetime/turn`,
`/internal/facetime/commit-turn`, and `/internal/facetime/status` with the
same secret. Chusky validates the call ID and owner, uses the owner's existing
memory, limits tools to read-only calls, and stores only committed text turns
in normal history.
