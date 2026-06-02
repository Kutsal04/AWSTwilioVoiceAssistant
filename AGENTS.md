# AGENTS.md

## Project Context

This repository implements a production-style real-time voice agent for a technical interview project. The system connects Twilio Media Streams to Amazon Nova 2 Sonic using bidirectional streaming, then hardens the prototype with AWS-managed persistence, observability, and CDK deployment.

Before making architectural changes, read:

- `docs/0001-real-time-voice-agent-architecture.md`

## Architectural Guardrails

- Use Python 3.12.
- Use a single FastAPI service for the runtime backend.
- Run the deployed media path on ECS Fargate, not Lambda.
- Use ngrok for local Twilio development.
- Use ALB + ACM/domain for the production-style deployed WebSocket endpoint when available.
- Keep Twilio call handling inbound-only unless explicitly asked to add outbound support.
- Generate TwiML with the Twilio Python helper library.
- Use Twilio Stream Parameters to pass `session_id` and `persona_id` to the media WebSocket.
- Generate an internal UUID `session_id`; store Twilio `CallSid` separately as `call_sid`.
- Use one Nova 2 Sonic stream per phone call.
- Implement clean turn-taking first. Treat barge-in as optional bonus work only after the required path is stable.
- Use lightweight Python audio conversion/resampling for Twilio μ-law 8 kHz <-> Nova PCM 16 kHz.
- Use bounded audio queues. Do not allow unbounded buffering in the media path.

## Data Model Rules

Use three DynamoDB tables:

- `sessions`
- `personas`
- `transcript_turns`

Do not replace this with a single-table design, S3/Athena, RDS, Redis, or another persistence model unless explicitly requested.

Transcript behavior:

- Buffer partial transcript events in memory.
- Persist finalized turns during the call.
- Store transcript turns with deterministic ordering by `session_id` and `turn_index`.
- Keep transcript retrieval and reporting CLI-only unless explicitly requested.

Persona behavior:

- Store personas/system prompts in DynamoDB.
- Select persona from the Twilio webhook query parameter when provided.
- Fall back to `DEFAULT_PERSONA_ID`.
- Validate `persona_id` before using it.

## Security and Privacy Rules

- Do not log raw transcript text, caller utterances, phone numbers, PHI, or free-form caller content.
- Logs may include `session_id`, `call_sid`, `persona_id`, lifecycle state, event names, latency values, counters, and error kinds.
- Make Twilio request signature verification configurable.
- Enable Twilio signature verification in deployed environments.
- Allow disabling signature verification for local ngrok development if needed.
- Use `.env` for local-only configuration, but do not commit secrets.
- Use Secrets Manager for deployed secrets.
- Use SSM parameters or ECS environment variables for non-secret deployed config.

## Reliability Rules

Use explicit session lifecycle states:

- `starting`
- `active`
- `draining`
- `completed`
- `failed`
- `abandoned`

Do not pretend to support live-call recovery after Fargate task death. Persist enough state to diagnose/finalize the session, but document that true live-call continuation is out of scope.

DynamoDB write policy:

- Session creation is critical; fail the call if it cannot be created after retries.
- Transcript writes should retry briefly, log/metric failures, and avoid crashing an active call.
- Session finalization should retry harder and emit an error metric if it still fails.

## Observability Rules

- Use structured JSON logs.
- Emit CloudWatch metrics through Embedded Metric Format.
- Required metrics:
  - `CallCount`, dimensioned by `persona_id`.
  - `TurnResponseLatencyMs`, dimensioned by `persona_id`.
  - `ErrorCount`, dimensioned by `error_kind`.
- Create at least one CloudWatch alarm on `ErrorCount`.

## Testing Expectations

Do not require real Twilio, AWS, or Nova calls in unit tests.

Unit tests should cover nontrivial logic, including:

- Persona selection and fallback behavior.
- Transcript turn ordering and formatting.
- Session lifecycle transitions.
- Audio codec/resampling helpers.
- DynamoDB repository behavior through fakes/mocks where practical.

Integration testing may use real Twilio, ngrok, Nova Sonic, and AWS dev resources, but keep that separate from unit tests.

## Documentation Expectations

Keep documentation practical and concise.

Update documentation when changing:

- Runtime architecture.
- Data model.
- Deployment steps.
- Twilio setup.
- Persona management.
- Transcript retrieval/reporting commands.
- Known limitations or tradeoffs.

Avoid creating new ADRs unless a decision is significant, likely to be revisited, or materially changes the accepted architecture.
