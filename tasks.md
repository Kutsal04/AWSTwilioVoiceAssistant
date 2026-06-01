# Implementation Roadmap

This roadmap follows `AGENTS.md` and `docs/0001-real-time-voice-agent-architecture.md`.

The implementation order intentionally isolates risk. Do not integrate Twilio, Nova, persistence, observability, and infrastructure all at once. Each phase should produce a small, testable result before moving forward.

## Guiding Principles

- Build the media bridge incrementally.
- Keep unit tests free of real Twilio, AWS, and Nova calls.
- Use real Twilio/ngrok/Nova only for integration checkpoints.
- Prefer clear, boring modules over clever abstractions.
- Keep infrastructure deployment until the local runtime path is stable.
- Do not implement barge-in until the required turn-taking path works end to end.
- Do not add public transcript/admin HTTP endpoints.

## Phase 0: Repository Skeleton and Tooling

Goal: create a maintainable project shape before adding integration complexity.

Tasks:

- Create the Python package layout under `app/`.
- Create `scripts/`, `infra/`, and `tests/` directories.
- Add `requirements.txt` with pinned runtime and test dependencies.
- Add a plain `Dockerfile` for the FastAPI service.
- Add `.env.example` with non-secret sample configuration names.
- Add initial `README.md` with current architecture summary and local setup placeholders.

Suggested modules:

- `app/main.py`
- `app/config.py`
- `app/logging.py`
- `app/metrics.py`
- `app/twilio/`
- `app/nova/`
- `app/audio/`
- `app/sessions/`
- `app/personas/`
- `app/transcripts/`

Verification:

- `python -m pytest` runs, even if only a placeholder smoke test exists.
- `uvicorn app.main:app --host 0.0.0.0 --port 8080` starts locally.
- `GET /health` returns a simple healthy response.

Do not proceed until:

- The app starts without Twilio, Nova, or AWS configuration.
- The project can run tests locally without cloud access.

## Phase 1: Configuration, Logging, and Test Harness

Goal: establish safe defaults and testable boundaries before touching real services.

Tasks:

- Implement typed configuration loading from environment variables.
- Support local `.env` loading without committing secrets.
- Add structured JSON logging helpers.
- Add a test fixture strategy for fake repositories and fake external clients.
- Add config flags for:
  - `ENV_NAME`
  - `PUBLIC_BASE_URL`
  - `DEFAULT_PERSONA_ID`
  - `VERIFY_TWILIO_SIGNATURE`
  - DynamoDB table names
  - Bedrock/Nova region

Verification:

- Unit tests cover config defaults and required config validation.
- Logs are JSON-shaped.
- No log helper accepts raw transcript text by default.

Do not proceed until:

- Local config can run without production secrets.
- Tests can override config deterministically.

## Phase 2: Twilio Webhook Without Media Streaming

Goal: validate the inbound-call control path before handling audio.

Tasks:

- Implement `POST /twilio/voice`.
- Generate TwiML with the Twilio Python helper library.
- Generate an internal UUID `session_id`.
- Read optional `persona_id` from the webhook query parameter.
- Fall back to `DEFAULT_PERSONA_ID` when `persona_id` is missing.
- Include `session_id` and `persona_id` as Twilio Stream Parameters.
- Add configurable Twilio signature verification.
- Keep signature verification enabled by default for deployed environments and disable-able for local ngrok development.

Verification:

- Unit tests verify TwiML contains a `<Connect><Stream>` URL.
- Unit tests verify Stream Parameters include `session_id` and `persona_id`.
- Unit tests verify persona query parameter fallback behavior.
- Unit tests verify signature verification can be enabled/disabled.

Integration checkpoint:

- Run app locally.
- Expose it through ngrok.
- Configure Twilio webhook to `/twilio/voice`.
- Call the Twilio number and confirm Twilio receives valid TwiML.

Do not proceed until:

- Twilio can reach the local webhook through ngrok.
- Twilio accepts the generated TwiML.

## Phase 3: Twilio Media WebSocket Protocol Capture

Goal: understand Twilio Media Streams behavior before connecting Nova.

Tasks:

- Implement `WebSocket /media`.
- Accept a Twilio WebSocket connection.
- Parse Twilio `connected`, `start`, `media`, and `stop` events.
- Extract `session_id`, `persona_id`, and `call_sid` from the `start` event.
- Log lifecycle events without logging audio payloads or caller content.
- Close the WebSocket gracefully on malformed start events.
- Add basic idle timeout behavior.

Verification:

- Unit tests cover Twilio event parsing.
- Unit tests cover malformed/missing Stream Parameters.
- Unit tests cover lifecycle transition from connected to started to stopped.

Integration checkpoint:

- Call the Twilio number through ngrok.
- Confirm the app receives `start`, `media`, and `stop` events.
- Confirm logs include `session_id`, `call_sid`, and `persona_id`.
- Confirm logs do not include media payloads.

Do not proceed until:

- Twilio media events are parsed reliably.
- The app can handle a call connect/disconnect without Nova or DynamoDB.

## Phase 4: Audio Codec and Resampling Module

Goal: isolate audio correctness before bridging systems.

Tasks:

- Implement Twilio inbound conversion:
  - base64 payload
  - μ-law 8 kHz
  - PCM16
  - resample 8 kHz to 16 kHz
- Implement Nova outbound conversion:
  - PCM16 16 kHz
  - resample 16 kHz to 8 kHz
  - μ-law
  - base64 payload
- Handle empty, malformed, and short frames safely.
- Keep audio conversion pure and independent of Twilio/Nova clients.

Verification:

- Unit tests cover μ-law decode/encode.
- Unit tests cover 8 kHz to 16 kHz and 16 kHz to 8 kHz resampling.
- Unit tests cover malformed frame behavior.
- Unit tests cover round-trip sanity for known sample frames.

Do not proceed until:

- Audio conversion is testable without Twilio or Nova.
- Bad input frames fail safely and observably.

## Phase 5: Session Actor and Queue Isolation

Goal: create per-call orchestration without external services.

Tasks:

- Implement `SessionActor`.
- Give each actor its own:
  - `session_id`
  - `call_sid`
  - `persona_id`
  - lifecycle state
  - inbound audio queue
  - outbound audio queue
  - task group/cancellation state
  - transcript buffer
- Implement explicit lifecycle states:
  - `starting`
  - `active`
  - `draining`
  - `completed`
  - `failed`
  - `abandoned`
- Use bounded audio queues.
- Drop stale frames when queues are full.
- Emit structured log/metric events for dropped frames.
- Add a process-local active session registry keyed by `session_id`.

Verification:

- Unit tests cover lifecycle transitions.
- Unit tests cover actor creation and cleanup.
- Unit tests cover queue overflow behavior.
- Unit tests prove two actors do not share mutable call state.

Do not proceed until:

- Two fake sessions can run concurrently in tests without state contamination.
- Queue overflow behavior is deterministic.

## Phase 6: Nova Sonic Client Spike

Goal: validate Nova SDK/API behavior separately from Twilio.

Tasks:

- Run the AWS Nova 2 Sonic sample outside the app.
- Confirm Bedrock model access and required region.
- Create a minimal `NovaClient` wrapper for one bidirectional stream.
- Identify actual Nova input audio format, event names, transcript text events, and output audio event shape.
- Document any mismatches between expected and actual SDK behavior.
- Keep this spike isolated from the Twilio WebSocket until stable.

Verification:

- A local script can open a Nova stream and close it cleanly.
- A controlled audio input can produce a Nova response or a known error.
- Nova event parsing is covered by unit tests using captured/fake event fixtures.

Do not proceed until:

- The app has a clear, tested adapter boundary for Nova events.
- Any API instability is documented in README notes or implementation comments.

## Phase 7: Local Twilio-to-Nova Audio Bridge

Goal: create the first end-to-end voice path with minimal dependencies.

Tasks:

- Connect Twilio inbound media events to the SessionActor inbound queue.
- Convert Twilio audio to Nova input format.
- Open one Nova Sonic stream per call.
- Send inbound audio frames to Nova.
- Receive Nova output audio.
- Convert Nova audio to Twilio media payloads.
- Send Twilio outbound `media` events.
- Implement clean turn-taking first.
- Do not add persistence yet beyond in-memory buffers/logs.
- Do not add barge-in yet.

Verification:

- Manual call through Twilio/ngrok reaches Nova.
- Caller can hear at least one Nova response.
- The service survives caller hangup.
- The service closes the Nova stream when the Twilio stream ends.
- Logs show lifecycle events and errors without transcript/audio payloads.

Debugging rule:

- If this phase fails, debug only Twilio media parsing, audio conversion, Nova event handling, and actor orchestration.
- Do not add DynamoDB, CDK, or CloudWatch alarm work while this path is unstable.

Do not proceed until:

- A real call can complete at least one user-speaks, agent-responds turn.

## Phase 8: Persona Repository and CLI Seeding

Goal: add configurable behavior without changing the media bridge.

Tasks:

- Implement `personas` DynamoDB repository.
- Add `scripts/seed_personas.py`.
- Seed at least two personas:
  - `warm_clinical_followup`
  - `appointment_reminder`
- Validate `persona_id` at call start.
- Fail clearly or fall back safely when persona lookup fails, according to config.
- Apply the selected persona system prompt to the Nova session.

Verification:

- Unit tests cover persona selection from query parameter.
- Unit tests cover fallback to `DEFAULT_PERSONA_ID`.
- Unit tests cover missing/inactive persona behavior.
- CLI can seed/update personas in a dev table.

Integration checkpoint:

- Run two calls with different `persona_id` values.
- Confirm behavior changes without redeploying code.

Do not proceed until:

- Persona switching works through configuration, not code changes.

## Phase 9: Session Persistence

Goal: persist call/session state while keeping the media path stable.

Tasks:

- Implement `sessions` DynamoDB repository.
- Create session record before returning/using the media stream.
- Store:
  - `session_id`
  - `call_sid`
  - `persona_id`
  - `status`
  - `started_at`
  - `ended_at`
  - `last_event_at`
  - `outcome_description`
  - `error_kind`
  - `schema_version`
- Apply write policy:
  - session creation is critical
  - session finalization retries harder
- Finalize sessions on Twilio disconnect.
- Mark failed/abandoned sessions on error paths.

Verification:

- Unit tests cover session create/update/finalize behavior using fakes/mocks.
- Unit tests cover retry policy for critical and finalization writes.
- Manual call creates and finalizes a DynamoDB session record.

Do not proceed until:

- Every completed manual call leaves a coherent session record.
- Failed calls are visible as failed/abandoned, not silently missing.

## Phase 10: Transcript Persistence and Retrieval CLI

Goal: store useful conversation data without coupling transcript writes to audio frames.

Tasks:

- Implement `transcript_turns` DynamoDB repository.
- Buffer partial Nova transcript events in memory.
- Persist finalized turns during the call.
- Assign deterministic `turn_index` values.
- Store:
  - `session_id`
  - `turn_index`
  - `speaker`
  - `text`
  - `transcript_item_id`
  - `confidence` when available
  - `created_at`
- Add `scripts/get_transcript.py --session-id ...`.
- Ensure CLI returns transcript ordered by turn.
- Do not log transcript text in application logs.

Verification:

- Unit tests cover transcript ordering.
- Unit tests cover partial-to-final turn buffering.
- Unit tests cover transcript CLI formatting using fake repository data.
- Manual call writes transcript turns and retrieval CLI returns them in order.

Do not proceed until:

- Transcript retrieval works from DynamoDB by `session_id`.
- Transcript text is not present in CloudWatch-style application logs.

## Phase 11: Simple Reporting CLI

Goal: satisfy reporting requirements with the smallest useful operator path.

Tasks:

- Add `scripts/report.py`.
- Report:
  - sessions per persona
  - average call length
  - error rate
- Use simple DynamoDB reads/scans suitable for assignment data volume.
- Document that larger production analytics would need a different path.

Verification:

- Unit tests cover report aggregation over fake session records.
- CLI runs against dev data and returns expected counts.

Do not proceed until:

- Reports work without adding public HTTP endpoints.

## Phase 12: Observability

Goal: add production visibility after core behavior is working.

Tasks:

- Implement structured JSON logs consistently across modules.
- Add CloudWatch Embedded Metric Format helpers.
- Emit:
  - `CallCount`, dimensioned by `persona_id`
  - `TurnResponseLatencyMs`, dimensioned by `persona_id`
  - `ErrorCount`, dimensioned by `error_kind`
- Add audio-frame-drop log/metric events.
- Ensure all call logs include `session_id` and, where available, `call_sid` and `persona_id`.
- Add privacy guardrails so raw transcripts and media payloads are not logged.

Verification:

- Unit tests verify log/metric payload shape.
- Manual call emits expected lifecycle logs.
- Manual call emits `CallCount`.
- At least one forced error emits `ErrorCount`.

Do not proceed until:

- Logs are useful for tracing one call without exposing sensitive content.

## Phase 13: Reliability Hardening

Goal: make known failure paths explicit and bounded.

Tasks:

- Add bounded timeouts for:
  - persona lookup
  - session create/update
  - Nova stream connect
  - Nova response wait
  - DynamoDB transcript/session writes
  - Twilio media idle
  - graceful shutdown drain
- Add small retry policy for DynamoDB writes.
- Handle Twilio disconnects gracefully.
- Handle Nova stream errors without crashing the process.
- Ensure active sessions are removed from the registry on completion/failure.
- Add SIGTERM/shutdown handling for ECS task draining where practical.

Verification:

- Unit tests cover timeout behavior with fake slow dependencies.
- Unit tests cover DynamoDB retry behavior.
- Unit tests cover Twilio disconnect finalization.
- Manual disconnect leaves finalized or abandoned session state.

Do not proceed until:

- Failure paths produce clear state, logs, and metrics.

## Phase 14: Containerization

Goal: package the already-working service for ECS without changing behavior.

Tasks:

- Finalize `Dockerfile`.
- Ensure container runs with:
  - `uvicorn app.main:app --host 0.0.0.0 --port 8080`
- Add container health check support through `/health`.
- Verify runtime dependencies needed for audio conversion are available.
- Keep image small enough for practical deploys.

Verification:

- Build container locally.
- Run container locally with environment variables.
- `GET /health` works in container.
- A local/ngrok call can reach the containerized app if practical.

Do not proceed until:

- Container behavior matches local non-container behavior.

## Phase 15: CDK Infrastructure

Goal: deploy the known-good service with production-realistic AWS resources.

Tasks:

- Create one environment-parameterized CDK stack.
- Add DynamoDB tables:
  - `sessions`
  - `personas`
  - `transcript_turns`
- Add ECS cluster and Fargate service.
- Add task role with least-privilege access to DynamoDB, Bedrock, Secrets Manager, SSM, and CloudWatch logs as needed.
- Add ALB and target group.
- Add CloudWatch log group.
- Add CloudWatch alarm on `ErrorCount`.
- Add Secrets Manager/SSM references for deployed config.
- Parameterize:
  - environment name
  - default persona ID
  - Bedrock region
  - optional domain/certificate settings

Verification:

- `cdk synth` succeeds.
- `cdk deploy -c env=dev` succeeds in a dev account.
- Deployed `/health` endpoint works through ALB.
- `cdk destroy -c env=dev` tears down managed resources cleanly.

Do not proceed until:

- Deploy/destroy are repeatable.
- IAM permissions are scoped to created resources where practical.

## Phase 16: Deployed End-to-End Call

Goal: prove the production-style path works after local integration is stable.

Tasks:

- Configure deployed public webhook URL in Twilio.
- Enable Twilio signature verification for deployed environment.
- Seed deployed personas.
- Make inbound call through Twilio to deployed service.
- Verify:
  - call reaches ECS service
  - one Nova stream opens
  - agent responds
  - session record is created/finalized
  - transcript turns are persisted
  - metrics/logs appear in CloudWatch

Verification:

- Complete one real deployed call.
- Retrieve transcript with CLI.
- Run report CLI.
- Confirm CloudWatch logs contain identifiers but no raw transcript/caller content.

Do not proceed until:

- The deployed system can complete the required demo path.

## Phase 17: Documentation and Demo Prep

Goal: make the project reviewable and defensible.

Tasks:

- Complete `README.md`:
  - architecture summary
  - local setup
  - ngrok/Twilio setup
  - Bedrock model access
  - persona seeding
  - transcript retrieval
  - reporting
  - tests
  - CDK deploy/destroy
  - known limitations
- Add or update `docs/architecture.md` if needed.
- Add sample stored conversation records or sanitized examples.
- Document timeout budget.
- Document production limitations:
  - no true live-call recovery after task death
  - no HIPAA-grade compliance
  - no EMR/scheduling integration
  - barge-in optional/not required
- Record the required demo video.

Verification:

- A reviewer can follow README from fresh checkout.
- Demo video shows a real call.
- Known limitations are honest and specific.

## Phase 18: Optional Bonus Work Only After Required Path Is Stable

Goal: add differentiators without destabilizing the required system.

Only start this phase after Phase 16 and Phase 17 are complete.

Candidate bonus tasks:

- Barge-in:
  - detect caller speech while agent audio is playing
  - clear outbound audio queue
  - send Twilio `clear`
  - handle Nova response cancellation/reset safely
- Concurrency demo:
  - run two simultaneous calls
  - prove separate `SessionActor` state
  - confirm no transcript/persona contamination
- Better shutdown handling:
  - task drain hooks
  - graceful session finalization on SIGTERM
- Prompt/persona versioning:
  - store persona version on session record
  - preserve prompt used for historical calls without logging sensitive caller data

Verification:

- Bonus behavior has tests or a clear manual verification path.
- Required turn-taking path still works after bonus changes.
- README distinguishes required features from bonus/partial features.

## Suggested Two-Week Timeline

This is a planning guide, not a strict schedule.

- Days 1-2: Phases 0-3. Project skeleton, Twilio webhook, Twilio media capture.
- Days 3-4: Phases 4-6. Audio module, session actor, Nova spike.
- Days 5-6: Phase 7. Local Twilio-to-Nova bridge.
- Days 7-8: Phases 8-11. Personas, sessions, transcripts, CLI retrieval/reporting.
- Days 9-10: Phases 12-14. Observability, reliability, containerization.
- Days 11-12: Phases 15-16. CDK infrastructure and deployed end-to-end call.
- Days 13-14: Phase 17 and selected Phase 18 items only if safe. Documentation, demo, optional bonus.

## Final Completion Criteria

The project is ready for submission when:

- A real inbound Twilio call reaches the agent.
- The agent completes at least one clean conversational turn through Nova 2 Sonic.
- Sessions are persisted in DynamoDB.
- Personas are configurable through DynamoDB and selected by query parameter/default fallback.
- Transcript turns are persisted and retrievable by CLI in order.
- Simple report CLI returns sessions per persona, average call length, and error rate.
- Structured logs and required metrics exist without sensitive caller content.
- CDK deploys and destroys the AWS stack.
- Unit tests cover nontrivial logic.
- README explains setup, operation, tradeoffs, and limitations.
