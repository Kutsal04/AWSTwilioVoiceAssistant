# ADR 0001: Real-Time Voice Agent Architecture

## Status

Accepted

## Context

The project requires a production-style real-time AI voice agent that connects a real Twilio phone call to Amazon Nova 2 Sonic using bidirectional streaming. The system must support a natural speech-to-speech conversation, persist session state, store transcripts, support configurable personas, emit operational telemetry, and be deployable to AWS using the AWS CDK.

The assignment explicitly warns that the media path is a long-lived bidirectional WebSocket carrying real-time audio, which is not a good fit for AWS Lambda or API Gateway WebSocket APIs. It also emphasizes production realism, maintainability, and engineering judgment over hyperscale design.

The architecture should therefore optimize for:

- Correct handling of the real-time Twilio-to-Nova audio bridge.
- Simple, explainable production-shaped infrastructure.
- Clear per-call state isolation.
- Durable session/persona/transcript storage.
- Meaningful observability.
- A feasible implementation scope for a two-week project.

## Decision

Use a single Python 3.12 FastAPI service as the voice-agent backend.

For local development, Twilio will connect to the local service through ngrok. For deployed AWS execution, Twilio will connect to an HTTPS/WSS endpoint backed by an Application Load Balancer and an ECS Fargate service. If a custom domain is available, the ALB will use ACM for TLS. Domain and Twilio account setup remain documented manual prerequisites.

The service will handle:

- `GET /health`
- `POST /twilio/voice`
- `WebSocket /media`
- Twilio signature verification, configurable by environment.
- TwiML generation using the Twilio helper library.
- Twilio Media Streams handling.
- One per-call `SessionActor`.
- One Nova 2 Sonic bidirectional stream per phone call.
- Audio conversion between Twilio μ-law 8 kHz and Nova PCM 16 kHz.
- Persona lookup.
- Session state persistence.
- Transcript persistence.
- Structured logging and CloudWatch EMF metrics.

Non-runtime admin and retrieval operations will be implemented as CLI scripts:

- Seed/update personas.
- Retrieve a transcript by `session_id`.
- Run simple reports.

The data model will use three DynamoDB tables:

- `sessions`
- `personas`
- `transcript_turns`

The core conversation flow will implement clean turn-taking first. Barge-in will be treated as a later optional enhancement, not a dependency for Phase 1 success.

## Alternatives Considered

### ECS Fargate Media Service

ECS Fargate is the selected compute target for the deployed media path.

It supports long-lived WebSocket connections, avoids EC2 host management, works well with an ALB, and is straightforward to provision with CDK. It is production-realistic without introducing Kubernetes or host-level operations.

### EC2 Media Service

EC2 would also support long-lived WebSockets and may be easier to debug through SSH. It provides lower-level control over networking, process supervision, and runtime behavior.

It was not selected because it adds host management, patching, AMI concerns, process supervision, and more operational burden than this assignment needs.

### Lambda or API Gateway WebSocket Media Path

Lambda and API Gateway WebSocket APIs were rejected for the media path. The Twilio media stream is long-lived, bidirectional, latency-sensitive, and audio-frame oriented. Lambda duration, connection lifecycle, binary framing, and per-message invocation semantics are a poor fit.

This approach also conflicts with the project guidance.

### Single Backend Service

A single service was selected instead of separate media, admin, reporting, and transcript services.

The single-service approach keeps deployment simple and lets the implementation focus on the hard part: reliable real-time audio bridging and per-session isolation. Internal modules will still separate Twilio, Nova, audio, session, persona, transcript, and observability concerns.

### Separate Admin or Reporting Lambdas

Separate Lambdas for transcript retrieval, reporting, or persona administration would provide cleaner separation between hot-path media and non-hot-path operations.

They were not selected initially because the specification allows CLI retrieval/reporting, and public admin/report endpoints would introduce authentication and security concerns that are not central to the assignment.

### Three DynamoDB Tables

Three DynamoDB tables were selected:

- `sessions` for call/session metadata.
- `personas` for runtime-configurable prompts and persona settings.
- `transcript_turns` for ordered conversation turns.

This is explicit, easy to test, and easy to explain. It avoids clever single-table design where the access patterns are simple and the data volume is small.

### Single-Table DynamoDB Design

A single-table design could reduce table count and demonstrate DynamoDB modeling sophistication.

It was rejected because it adds cognitive overhead without meaningful benefit for this scope. Clarity is more valuable than table-count minimization here.

### S3/Athena Transcript Storage

S3 JSONL plus Athena would be better for large-scale transcript analytics and long-term append-only storage.

It was rejected for the initial design because transcript retrieval by `session_id` and ordered turn display are simpler with DynamoDB. Athena/Glue would add infrastructure that does not improve the required demo.

### HTTP Transcript and Report Endpoints

HTTP endpoints would make transcript retrieval easy for reviewers.

They were rejected in favor of CLI scripts because transcript/report access is not part of the public call path and may expose sensitive conversation data if left unauthenticated. The project explicitly permits CLI retrieval.

### Barge-In From the Start

Barge-in would improve conversational naturalness and is a strong signal if implemented correctly.

It was rejected as a core dependency because it adds significant complexity around caller speech detection, response cancellation, Twilio `clear`, outbound audio flushing, and Nova stream state. The system will be designed with hooks for future barge-in but will first prioritize reliable turn-taking.

## Tradeoffs

### Simplicity vs Production Separation

The selected architecture keeps all runtime behavior in one service. This reduces deployment and coordination complexity but means the media path and Twilio webhook live in the same process.

This is acceptable for the assignment because the workload is small, the public runtime surface is minimal, and internal module boundaries can still keep the code maintainable.

### Fargate vs EC2 Control

Fargate reduces host-level operational work but gives less direct control than EC2. For this project, managed container runtime and CDK deployability are more valuable than low-level host control.

### DynamoDB Simplicity vs Analytical Flexibility

DynamoDB is excellent for the required session and transcript access patterns. It is less ideal for large analytical queries. The simple report can scan small project data. If production analytics became important, transcript events could later be streamed or exported to S3/Athena.

### Turn-Taking vs Barge-In

Clean turn-taking is less natural than true interruption support, but it is much less risky. The architecture keeps barge-in optional so Phase 1 success does not depend on the most complex interaction behavior.

### Runtime Query Parameter Persona Selection

Persona selection will use `persona_id` from the Twilio webhook query parameter, falling back to `DEFAULT_PERSONA_ID`. This allows persona switching without redeployment.

The tradeoff is that Twilio webhook configuration or demo URLs must be managed carefully. The `persona_id` must be validated against DynamoDB before use.

## Consequences

### Positive Consequences

- The architecture fits the real-time WebSocket nature of Twilio Media Streams.
- The system can be developed locally through ngrok before AWS deployment.
- The deployed system has a production-realistic shape without excessive infrastructure.
- Per-call `SessionActor` isolation avoids shared mutable call state.
- DynamoDB provides durable session, persona, and transcript storage.
- CLI-only admin/reporting keeps transcript access off the public service surface.
- Structured logs and EMF metrics satisfy observability requirements without making direct metric API calls in the audio path.
- The architecture is straightforward to explain in a technical walkthrough.

### Negative Consequences

- A single Fargate task is not highly available unless desired count is increased.
- If a task dies during a call, the live media stream is likely lost. DynamoDB can preserve/finalize state, but true live-call continuation is out of scope.
- CLI-only transcript/report access is less convenient than HTTP endpoints.
- DynamoDB scans for reporting are acceptable only because assignment data volume is small.
- Barge-in is not included in the required path and may not be delivered if time runs short.
- ALB + ACM requires domain/certificate setup for the final clean deployed WSS endpoint.

## Rejected Approaches

### Kubernetes / EKS

Rejected as overengineered. EKS would add cluster management, ingress, Helm/manifests, autoscaling concerns, and cost without improving the core Twilio-to-Nova integration.

### Multi-Service Microservice Architecture

Rejected because the assignment does not need independently deployed persona, transcript, reporting, and media services. Splitting too early would increase latency surfaces, IAM complexity, deployment complexity, and debugging effort.

### Kafka, MSK, or Event Bus for Audio

Rejected because real-time audio should not be routed through a general-purpose event bus for this project. It would add latency and complexity. Audio frames should remain in the live session process.

### Separate STT and TTS Services

Rejected because Nova 2 Sonic is the required speech-to-speech model. Adding separate STT/TTS services would complicate the design and move away from the assignment’s intended integration.

### Redis / ElastiCache Session Coordination

Rejected for the initial design. Active session actors are process-local, and durable metadata lives in DynamoDB. Redis would only be justified if the system needed distributed active-session routing or reconnect coordination across multiple media tasks.

### Public Admin API

Rejected initially to avoid exposing transcript or persona operations over the public ALB. CLI scripts provide the required retrieval/reporting/admin paths with less security risk.

## Operational Implications

### Deployment

The AWS deployment will be managed by one environment-parameterized CDK stack:

- VPC/networking.
- ALB.
- ECS cluster.
- ECS Fargate task definition and service.
- DynamoDB tables.
- CloudWatch log group.
- CloudWatch metrics/alarm.
- IAM roles and policies.
- Secrets Manager / SSM references.

Expected commands:

```bash
cdk deploy -c env=dev
cdk destroy -c env=dev
```

Twilio phone number setup, Twilio webhook configuration, Bedrock model access, and optional domain registration are documented prerequisites.

### Local Development

Local integration development will use:

- Local FastAPI service.
- ngrok public HTTPS/WSS tunnel.
- Real Twilio phone call.
- Real Nova 2 Sonic.
- AWS credentials for DynamoDB/Bedrock, or test fakes for unit tests.

Unit tests must not call Twilio, AWS, or Nova. They should cover persona selection, transcript ordering, session lifecycle transitions, and audio conversion.

### Security

Twilio webhook signature verification will be configurable:

- Enabled in deployed AWS environments.
- Optionally disabled for local ngrok development.

Secrets will not be checked into source control:

- Local secrets use `.env`.
- Deployed secrets use Secrets Manager.
- Non-secret configuration uses SSM parameters or ECS environment variables.

Logs must not contain raw transcript text, caller utterances, phone numbers, PHI, or free-form caller content. Logs may include session IDs, call SIDs, persona IDs, event names, latency values, lifecycle states, counters, and error kinds.

### Observability

The service will emit structured JSON logs to CloudWatch. Every call-related log line should include `session_id` and, when available, `call_sid` and `persona_id`.

Metrics will be emitted through CloudWatch Embedded Metric Format:

- `CallCount`, dimensioned by `persona_id`.
- `TurnResponseLatencyMs`, dimensioned by `persona_id`.
- `ErrorCount`, dimensioned by `error_kind`.

At least one CloudWatch alarm will be created for `ErrorCount`. For the take-home, a sensitive threshold is acceptable. In production, thresholds would be tuned based on actual traffic volume and alert fatigue.

### Failure Handling

The session lifecycle will use explicit states:

- `starting`
- `active`
- `draining`
- `completed`
- `failed`
- `abandoned`

Timeouts will be defined for:

- Persona lookup.
- Session create/update.
- Nova stream connect.
- Nova response wait.
- DynamoDB transcript/session writes.
- Twilio media idle.
- Graceful shutdown drain.

DynamoDB failure policy:

- Session creation is critical. If it fails after retries, the call fails.
- Transcript turn writes are important but should not crash an active call after brief retries.
- Session finalization retries harder and logs/metrics failures if still unsuccessful.

### Audio Backpressure

Audio queues will be bounded. If the system falls behind, stale audio frames may be dropped and logged. For real-time voice, late audio is often worse than missing audio.

Inbound and outbound queue drops should increment an audio-frame-dropped metric or structured log event. If barge-in is implemented later, outbound audio can be cleared and Twilio can receive a `clear` message to flush already-buffered audio.

### Scaling

The initial deployment can run with one Fargate task for cost and simplicity. The design supports multiple simultaneous calls within one process through per-session actors.

If scaling beyond the assignment:

- Increase Fargate desired count.
- Ensure session creation and stream routing remain task-local for each WebSocket.
- Consider ALB stickiness only if multiple related WebSockets must land on the same task.
- Consider Redis or another coordination layer only if true cross-task active-session recovery becomes required.

### Known Limitations

- No true live-call recovery after Fargate task death.
- No full barge-in in the required path.
- Reporting uses simple DynamoDB reads/scans suitable for small assignment data.
- No HIPAA-grade compliance, audit logging, or regulated data handling.
- No real EMR, scheduling system, or external business-system integration.
