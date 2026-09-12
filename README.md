# Chusky voice bridge

This private service bridges Chusky to live voice calls. Twilio Media Streams
provide the telephone calling path; an optional legacy Sendblue FaceTime path
uses Agora. Chusky remains the agent brain: the bridge sends speech turns to
Chusky's authenticated internal endpoint and streams its responses back to the
caller using Deepgram.

The bridge runs separately from Chusky and does not replace its model, memory,
history, or tools. For telephone calls, Chusky retains committed text turns in
the owner's private conversation history. Recall meeting turns use a separate,
bounded per-meeting text history and never receive private chat history. In
owner-enabled representative mode, Chusky can proactively contribute and call
only the exact owner-granted connected-app actions and native reminder/task
tools. It does not receive private memory or arbitrary Composio tools. Neither
transport persists raw audio in this bridge.

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

## Recall.ai meetings

Recall Output Media runs [`recall_media.html`](recall_media.html) as the bot's
camera webpage. The page receives meeting audio through the microphone
permission Recall grants its bot browser, sends 48 kHz mono PCM over an
authenticated websocket, and plays streamed 24 kHz Flux TTS PCM back into the
meeting. The bot requests the name **Chusky Meeting Assistant** and speaks a
short AI disclosure when it joins. Authenticated Google Meet bots ignore
Recall's `bot_name` and show the connected Google account name; use an
appropriately branded account. Recall Output Media always includes a
camera/webpage feed; this page shows a simple Chusky identity card.

Chusky creates interactive Zoom, Google Meet, Microsoft Teams, or Webex bots and
handles scheduled cancellation versus live leave through Recall's documented
endpoints. Recall supports GoTo Meeting bots, but its Output Media support
matrix does not list GoTo, so this interactive voice path deliberately rejects
those links until Recall documents and we verify support. A
verified Recall bot-status webhook updates owner-scoped state. The bridge
requires both an expiring, meeting-scoped HMAC ticket and a server-authenticated check that
the matching meeting is currently in-call. The ticket is carried in the URL
fragment (not sent in HTTP request URLs) and then the first websocket frame.
Uvicorn access logging is disabled so ticket-bearing paths cannot appear in its
standard request logs.

Configure Chusky's root service:

```ini
RECALL_MEETINGS_ENABLED=true
RECALL_API_KEY=<Recall API key>
RECALL_REGION=us-west-2
RECALL_BOT_NAME=Chusky Meeting Assistant
RECALL_MEDIA_PAGE_URL=https://<voice-service-host>/recall/media
RECALL_WEBHOOK_SECRET=<Recall Svix signing secret>
RECALL_MEDIA_BRIDGE_SECRET=<long random secret shared only with chusky-voice>
```

Configure the separate `chusky-voice` service, leaving its Twilio variables
intact:

```ini
RECALL_MEETINGS_ENABLED=true
RECALL_MEDIA_BRIDGE_SECRET=<same value as Chusky root>
DEEPGRAM_API_KEY=<Deepgram server API key>
VOICE_STT_MODEL=flux-general-en
VOICE_TTS_MODEL=flux-haley-en
CHUSKY_RECALL_TURN_STREAM_URL=https://<chusky-host>/internal/recall/turn-stream
CHUSKY_RECALL_COMMIT_TURN_URL=https://<chusky-host>/internal/recall/commit-turn
CHUSKY_RECALL_MEDIA_AUTHORIZE_URL=https://<chusky-host>/internal/recall/media-authorize
RECALL_MAX_MEETING_SECONDS=7200
RECALL_MAX_ACTIVE_MEETINGS=4
RECALL_COPILOT_MIN_INTERVAL_SECONDS=8
RECALL_COPILOT_MAX_EVALUATIONS=120
```

In the Recall dashboard for the chosen region, register a **Bot Status Change**
webhook pointing to `https://<chusky-host>/recall/webhook`, then configure its
`whsec_` signing secret as `RECALL_WEBHOOK_SECRET`. Chusky explicitly disables
Recall recording/transcript retention. A rolling window of up to 12 recent
utterances/6,000 characters is held in the bridge process for at most five
minutes; ambient speech is never written to persistent storage. Only turns
Chusky answers and its replies are retained in the owner's bounded meeting
history. Tell participants the AI assistant is joining; the page and spoken
intro explain live processing and retention. Platform waiting rooms and host
admission policies still apply. Webex may need workspace-side setup.

Optional meeting-chat support is configured on the **Chusky root service**,
not this bridge: set `RECALL_REALTIME_SECRET` to the Recall workspace
verification secret, and ensure root has Redis, QStash, and its public HTTPS
`WEBHOOK_URL`. Keep `RECALL_REALTIME_SECRET` separate from the bridge and from
the dashboard/Svix secret unless Recall explicitly provides one shared workspace
secret for both. The root service sends the AI/audio disclosure in supported
meeting chats and handles `/chusky` commands; this voice service does not need
any meeting-chat credentials.

### Staging smoke test

Run this against a separate staging Chusky deployment with its own Redis and a
dedicated test meeting; never validate by joining an unapproved production or
customer call. Confirm the status webhook is registered with the exact
staging URL and its signing secret is configured only on the staging service.

1. Schedule a bot at least ten minutes ahead. Confirm one provider bot is
   created, the stored state is `scheduled`, and repeating the same URL/time
   does not create a second bot. Reuse the URL with a different scheduled time
   and confirm it creates a separate instance.
2. Admit the bot to the test call. Confirm signed callbacks move state through
   `joining` and `in_call`; after the call ends, confirm it reaches `ended`.
   Check that an out-of-order older callback does not regress the state.
3. Send `/chusky status`, then an explicitly addressed chat question where the
   platform supports meeting chat. Confirm the status/reply and use
   `/chusky leave` to verify the bot exits. Create a second scheduled test bot
   and cancel it before dispatch; confirm the provider bot is deleted.
4. Keep a real signed Recall status webhook delivery in a short-lived,
   access-controlled JSON fixture outside the repository. It should contain
   `rawBody` (the exact request bytes as a string) and `headers` (at minimum
   `webhook-id`, `webhook-timestamp`, and `webhook-signature`). Verify it with
   the gated `tests/recallStaging.test.ts` signature test. Do not commit the
   fixture or include participant details, meeting URLs, or tickets in logs.

To verify a captured fixture, set `RECALL_STAGING_STATUS_FIXTURE` to its local
path and `RECALL_WEBHOOK_SECRET` to the staging Svix secret, then run
`npx tsx --test tests/recallStaging.test.ts`. To exercise a real scheduled
create/cancel contract, additionally set `RECALL_STAGING_CONFIRM` exactly to
`I_AUTHORIZE_STAGING_BOT`, plus `RECALL_STAGING_MEETING_URL`, `RECALL_API_KEY`,
`RECALL_REGION`, `RECALL_MEDIA_PAGE_URL`, and `RECALL_MEDIA_BRIDGE_SECRET`.
That opt-in test contacts Recall and creates then cancels a bot; run it only
with a dedicated staging meeting URL that you are authorized to use.

Recall's 507 capacity behavior is verified deterministically by
`tests/recallMeetings.test.ts`: it simulates 507 responses and checks the
30-second backoff, ten-attempt cap, and cancellation. Do not try to induce a
real 507 against a customer or production meeting. Unit coverage does not
substitute for the staging lifecycle checks above; mark those complete only
after observing callbacks from an actual staging bot.

The tool supports `addressed`, `copilot`, and owner-configured `representative`
modes. `addressed` calls the model when someone says “Chusky.” Copilot evaluates
eligible turns at most once every configured interval, speaks only with a
`SPEAK` verdict, and stays silent otherwise. Representative mode uses the
owner's configured company objective, approved knowledge, action allowlist, and
account aliases; it is similarly budgeted and fails silent when it has no useful
contribution. The Chusky root service enforces the configured evaluation limit
atomically in Redis across bridge reconnects and replicas (default 120 per
meeting); the voice service's local gate is an optimization only. At the cap it
falls back to addressed-only while direct wake-word requests continue to work.
Meeting model calls still count against the owner's normal rate and spend
limits. Set
`RECALL_COPILOT_MIN_INTERVAL_SECONDS` and `RECALL_COPILOT_MAX_EVALUATIONS` to
the same values on both Chusky and `chusky-voice`. Meeting runs never receive
private memories or account metadata. Only an enabled representative receives
a Composio session, limited to the owner's exact direct action grants; arbitrary
Composio discovery/execution remains unavailable. Recall's real-time
transcription webhooks are intentionally not used for live conversation;
Chusky uses Output Media and Deepgram Flux with its existing configured voice
model.

Twilio remains unchanged and is not routed through Recall. `/recall/health`
reports only configuration and active-session counts. The media page and all
bridge endpoints require HTTPS in deployment; use a public HTTPS tunnel when
testing with a local Chusky server.

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
