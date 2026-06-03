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
- `PERSONA_LOOKUP_TIMEOUT_SECONDS`
- `PERSONA_LOOKUP_FALLBACK_ENABLED`
- `VERIFY_TWILIO_SIGNATURE`
- `TWILIO_AUTH_TOKEN`
- `MEDIA_IDLE_TIMEOUT_SECONDS`
- `AUDIO_QUEUE_MAXSIZE`
- `NOVA_STREAM_OPEN_TIMEOUT_SECONDS`
- `NOVA_RESPONSE_TIMEOUT_SECONDS`
- `GRACEFUL_SHUTDOWN_DRAIN_SECONDS`
- `SESSION_WRITE_TIMEOUT_SECONDS`
- `SESSION_WRITE_RETRY_DELAY_SECONDS`
- `SESSION_CREATE_MAX_ATTEMPTS`
- `SESSION_UPDATE_MAX_ATTEMPTS`
- `SESSION_FINALIZE_MAX_ATTEMPTS`
- `TRANSCRIPT_WRITE_TIMEOUT_SECONDS`
- `TRANSCRIPT_WRITE_RETRY_DELAY_SECONDS`
- `TRANSCRIPT_WRITE_MAX_ATTEMPTS`
- `SESSIONS_TABLE_NAME`
- `PERSONAS_TABLE_NAME`
- `TRANSCRIPT_TURNS_TABLE_NAME`
- `BEDROCK_REGION`
- `NOVA_MODEL_ID`

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

The webhook validates the selected persona against the `personas` table before returning TwiML. If `PERSONA_LOOKUP_FALLBACK_ENABLED=true`, a missing or inactive requested persona falls back to `DEFAULT_PERSONA_ID`; if the default persona is unavailable, the webhook fails clearly instead of starting a call with an unknown prompt.

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

## Nova Sonic Spike

Phase 6 adds an isolated Nova 2 Sonic adapter under `app/nova` and a manual validation script:

```bash
python scripts/nova_sonic_spike.py
```

The script opens a Bedrock bidirectional stream, sends initialization events, and closes the stream. With a raw 16 kHz mono PCM16 file, it can also send controlled audio:

```bash
python scripts/nova_sonic_spike.py --pcm16-file sample.pcm
```

Required local prerequisites:

- AWS credentials available through the standard environment credential chain.
- Bedrock model access for `amazon.nova-2-sonic-v1:0`.
- `BEDROCK_REGION` set to a region where your account has Nova 2 Sonic access.

Current API notes from AWS documentation:

- Nova 2 Sonic uses Bedrock Runtime `InvokeModelWithBidirectionalStream`.
- The Python SDK path is experimental and uses `aws-sdk-bedrock-runtime`.
- Input audio is PCM16 16 kHz mono, base64 encoded in `audioInput` events.
- Output audio is PCM16 24 kHz mono, base64 encoded in `audioOutput` events.

## Session Actors

Phase 5 introduces one process-local `SessionActor` per call. Each actor owns its lifecycle state, inbound and outbound bounded audio queues, cancellation task set, and in-memory transcript buffer. When an audio queue is full, the actor deterministically drops the oldest stale frame and logs an operational `audio_frame_dropped` event without caller content.

## Local Audio Bridge

Phase 7 connects Twilio Media Streams to one Nova 2 Sonic stream per call. Twilio inbound media payloads are decoded from μ-law 8 kHz to PCM16 16 kHz and queued into the session actor before being sent to Nova. Nova `audioOutput` events are converted from PCM16 24 kHz to Twilio μ-law 8 kHz outbound `media` messages.

Manual Phase 7 validation requires the local FastAPI service, ngrok, a Twilio inbound call, AWS credentials, and Nova 2 Sonic model access. Success means one call can complete at least one caller-speaks, agent-responds turn, caller hangup is handled cleanly, and logs show lifecycle identifiers without audio payloads or transcript text.

## Personas

Phase 8 stores configurable personas in DynamoDB. Seed or update the required local/dev personas with:

```bash
aws dynamodb create-table \
  --table-name personas \
  --attribute-definitions AttributeName=persona_id,AttributeType=S \
  --key-schema AttributeName=persona_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

```bash
python scripts/seed_personas.py
```

Skip the create-table command if the table already exists. Use `--table-name <name>` to override `PERSONAS_TABLE_NAME`. The script upserts `warm_clinical_followup` and `appointment_reminder` and does not print prompt text.

At call start, the selected persona prompt is loaded server-side and sent to Nova as the system prompt. Prompt text is not passed through Twilio Stream Parameters or logged.

## Sessions

Phase 9 persists call/session state in DynamoDB. Create the local/dev sessions table with:

```bash
aws dynamodb create-table \
  --table-name sessions \
  --attribute-definitions AttributeName=session_id,AttributeType=S \
  --key-schema AttributeName=session_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

Skip the create-table command if the table already exists. Use `SESSIONS_TABLE_NAME` to target a different table.

The Twilio webhook creates a `starting` session record before returning TwiML. Session creation is critical: if the write fails after retries, the webhook returns an error instead of starting a media stream with no durable session record. When the media WebSocket starts, the record is marked `active`; Twilio `stop` finalizes it as `completed`; disconnects and error paths finalize it as `abandoned` or `failed`.

## Transcripts

Phase 10 stores finalized Nova transcript turns in DynamoDB. Create the local/dev transcript table with:

```bash
aws dynamodb create-table \
  --table-name transcript_turns \
  --attribute-definitions \
    AttributeName=session_id,AttributeType=S \
    AttributeName=turn_index,AttributeType=N \
  --key-schema \
    AttributeName=session_id,KeyType=HASH \
    AttributeName=turn_index,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST
```

Skip the create-table command if the table already exists. Use `TRANSCRIPT_TURNS_TABLE_NAME` to target a different table.

The runtime buffers partial Nova transcript events in memory and persists only finalized turns. Transcript writes retry briefly and log operational failure metadata if they still fail, but they do not crash an active call. Application logs do not include transcript text.

Retrieve an ordered transcript with:

```bash
python scripts/get_transcript.py --session-id <session-id>
```

## Reporting

Phase 11 adds a CLI-only report over the `sessions` table:

```bash
python scripts/report.py
```

The report includes sessions per persona, average call length, error count, and error rate. It uses a simple DynamoDB table scan, which is appropriate for assignment/dev data volume. A larger production analytics path would export or stream records into a dedicated analytical store rather than scanning the operational table.

## Observability

Phase 12 emits structured JSON logs and CloudWatch Embedded Metric Format metrics from the runtime path. Call-related logs include `session_id`, `call_sid`, and `persona_id` once Twilio sends the media `start` event. Logs intentionally exclude raw transcript text, caller utterances, phone numbers, and media payloads.

Current EMF metrics:

- `CallCount`, dimensioned by `persona_id`.
- `TurnResponseLatencyMs`, dimensioned by `persona_id`.
- `ErrorCount`, dimensioned by `error_kind`.
- `AudioFrameDropped`, dimensioned by `direction` and `persona_id`.

For local validation, make a Twilio/ngrok call and confirm the logs include lifecycle events such as `twilio_media_started`, `nova_stream_started`, and `twilio_media_stopped`, plus top-level EMF JSON records containing `_aws`.

## Reliability

Phase 13 makes failure paths explicit and bounded. Persona lookup, session writes, transcript writes, Nova stream open, Nova response waits, Twilio media idle, and service shutdown drain all have configured timeouts. DynamoDB session and transcript writes retry briefly according to their write criticality.

If Nova response events stall, the bridge logs `nova_response_timeout` and keeps the call process alive. If Nova receive fails, the bridge logs `nova_receive_error` and avoids crashing the process. Twilio disconnects remove active actors from the process-local registry, and service shutdown uses FastAPI lifespan cleanup to abandon/finalize any active sessions as `abandoned` with `service_shutdown` where practical.

## Container

Phase 14 packages the FastAPI service with Python 3.12 slim and runtime-only dependencies:

```bash
docker build -t aws-twilio-voice-assistant:local .
```

Run the container locally:

```bash
docker run --rm -p 8080:8080 --env-file .env aws-twilio-voice-assistant:local
```

Verify health:

```bash
curl http://localhost:8080/health
```

The container command is `uvicorn app.main:app --host 0.0.0.0 --port 8080`, and the image includes a Docker health check against `/health`.

## CDK Deployment

Phase 15 adds one environment-parameterized CDK stack under `infra/`. It creates:

- DynamoDB tables for sessions, personas, and transcript turns.
- ECS Fargate service running the Docker image from this repository.
- Public ALB and target group with `/health` checks.
- CloudWatch log group and an `ErrorCount` alarm.
- Least-privilege task permissions for DynamoDB, Bedrock Runtime, SSM config reads, and optional Twilio secret reads.
- SSM parameters under `/voice-agent/<env>/` for deployed non-secret config.

Install the infrastructure dependencies once:

```bash
pip install -r infra/requirements.txt
```

Synthesize the stack:

```bash
cd infra
npx cdk synth -c env=dev
```

Deploy and destroy in a dev account:

```bash
npx cdk deploy -c env=dev
npx cdk destroy -c env=dev
```

Useful context parameters:

```bash
npx cdk deploy \
  -c env=dev \
  -c defaultPersonaId=warm_clinical_followup \
  -c bedrockRegion=us-east-1 \
  -c twilioAuthTokenSecretArn=arn:aws:secretsmanager:us-east-1:123456789012:secret:twilio-auth-token-AbCdEf \
  -c domainName=voice.example.com \
  -c certificateArn=arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000
```

`ENV_NAME` is non-local in ECS, so Twilio signature verification is enabled. Provide `twilioAuthTokenSecretArn` before using the deployed Twilio webhook. Without a custom domain/certificate, the stack exposes HTTP through the ALB for health checks; production Twilio media should use an HTTPS/WSS domain backed by ACM.

## Current Status

Phase 15 adds CDK infrastructure for the deployed ECS/Fargate path. The next phase validates a deployed end-to-end call.
