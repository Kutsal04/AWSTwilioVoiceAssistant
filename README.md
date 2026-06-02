# AWS Twilio Voice Assistant

Production-style real-time voice agent for connecting inbound Twilio Media Streams to Amazon Nova 2 Sonic. The service is intentionally shaped as a single Python 3.12 FastAPI runtime so the local and deployed media paths stay easy to reason about.

## Architecture

- Runtime backend: one FastAPI service.
- Local Twilio development: ngrok to the local service.
- Deployed media path: ECS Fargate behind an ALB, with ACM/domain support when available.
- Persistence: DynamoDB tables for `sessions`, `personas`, and `transcript_turns`.
- Observability: structured JSON logs and CloudWatch Embedded Metric Format metrics.
- Non-runtime operations: CLI scripts for persona seeding, transcript retrieval, and reporting.

The accepted architecture is documented in `docs/0001-real-time-voice-agent-architecture.md`.

## Local Setup

Create a virtual environment with Python 3.12 and install dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy local configuration placeholders:

```bash
cp .env.example .env
```

The current local settings are:

- `ENV_NAME`
- `PUBLIC_BASE_URL`
- `DEFAULT_PERSONA_ID`
- `VERIFY_TWILIO_SIGNATURE`
- `TWILIO_AUTH_TOKEN`
- `MEDIA_IDLE_TIMEOUT_SECONDS`
- `SESSIONS_TABLE_NAME`
- `PERSONAS_TABLE_NAME`
- `TRANSCRIPT_TURNS_TABLE_NAME`
- `BEDROCK_REGION`

`Settings` loads these from environment variables and `.env` during local development.

Run tests:

```bash
python -m pytest
```

Start the local service:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Check health:

```bash
curl http://localhost:8080/health
```

Expected response:

```json
{"status":"healthy"}
```

## Twilio Webhook

The inbound voice webhook is:

```text
POST /twilio/voice
```

For local Twilio testing, expose the service with ngrok and set `PUBLIC_BASE_URL` to the ngrok HTTPS URL. Configure the Twilio phone number voice webhook to:

```text
https://<ngrok-host>/twilio/voice
```

Pass a persona for testing by adding `?persona_id=appointment_reminder`; otherwise the service falls back to `DEFAULT_PERSONA_ID`.

`VERIFY_TWILIO_SIGNATURE=false` is suitable for local ngrok development. For non-local `ENV_NAME` values, signature verification defaults on unless explicitly disabled, and `TWILIO_AUTH_TOKEN` must be configured.

## Twilio Media Stream

The Twilio Media Streams WebSocket endpoint is:

```text
GET /media
```

Phase 3 captures and logs Twilio `connected`, `start`, `media`, and `stop` lifecycle events. Logs include operational identifiers such as `session_id`, `call_sid`, `persona_id`, and `stream_sid`; media payloads are intentionally not logged.

During the Phase 3 manual checkpoint, call the Twilio number through ngrok and confirm the app logs `twilio_media_started` and `twilio_media_stopped`. The call will not produce agent audio until the Nova bridge is added in later phases.

## Audio Conversion

Phase 4 audio conversion helpers live under `app/audio`. They convert Twilio base64 μ-law 8 kHz frames to Nova PCM16 16 kHz audio, and Nova PCM16 16 kHz audio back to Twilio base64 μ-law 8 kHz payloads. The module is pure Python and independent of Twilio, Nova, and AWS clients.

## Current Status

Phase 4 establishes Twilio/Nova audio codec and resampling helpers. Nova streaming, DynamoDB, CDK, and production observability integrations are added in later phases.
