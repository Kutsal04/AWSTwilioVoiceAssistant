import asyncio
import logging
from collections.abc import Callable
from time import monotonic
from typing import Protocol
from uuid import uuid4

from fastapi import WebSocket

from app.audio import nova_pcm16_to_twilio_payload
from app.config import Settings
from app.logging import log_event
from app.metrics import emit_error_count, emit_turn_response_latency
from app.nova import (
    NovaParsedEvent,
    audio_content_start_event,
    audio_input_event,
    content_end_event,
    prompt_end_event,
    prompt_start_event,
    session_end_event,
    session_start_event,
    system_prompt_events,
)
from app.sessions import SessionActor
from app.transcripts import (
    TranscriptPersistenceError,
    TranscriptRepository,
    TranscriptTurnBuffer,
    put_transcript_turn_with_retry,
)

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are a concise real-time voice assistant. Keep responses brief, natural, and suitable for a phone call."
)


class NovaStreamClient(Protocol):
    async def open(self) -> None:
        ...

    async def send_event(self, event: dict) -> None:
        ...

    async def receive_event(self) -> NovaParsedEvent:
        ...

    async def close(self) -> None:
        ...


NovaClientFactory = Callable[[], NovaStreamClient]


class TwilioNovaBridge:
    def __init__(
        self,
        *,
        actor: SessionActor,
        websocket: WebSocket,
        stream_sid: str,
        settings: Settings,
        nova_client: NovaStreamClient,
        transcript_repository: TranscriptRepository,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self.actor = actor
        self.websocket = websocket
        self.stream_sid = stream_sid
        self.settings = settings
        self.nova_client = nova_client
        self.transcript_repository = transcript_repository
        self.system_prompt = system_prompt
        self.transcript_buffer = TranscriptTurnBuffer(session_id=actor.session_id)
        self.prompt_name = str(uuid4())
        self.system_content_name = str(uuid4())
        self.audio_content_name = str(uuid4())
        self._started = False
        self._stopping = asyncio.Event()
        self._stopped = False
        self._input_task: asyncio.Task | None = None
        self._output_task: asyncio.Task | None = None
        self._nova_events = 0
        self._latest_caller_turn_at: float | None = None

    async def start(self) -> None:
        await asyncio.wait_for(self.nova_client.open(), timeout=self.settings.nova_stream_open_timeout_seconds)
        await self.nova_client.send_event(session_start_event())
        await self.nova_client.send_event(prompt_start_event(self.prompt_name))
        for event in system_prompt_events(self.prompt_name, self.system_content_name, self.system_prompt):
            await self.nova_client.send_event(event)
        await self.nova_client.send_event(audio_content_start_event(self.prompt_name, self.audio_content_name))

        self._started = True
        self._input_task = self.actor.create_task(
            self._send_inbound_audio_to_nova(),
            name=f"{self.actor.session_id}-nova-input",
        )
        self._output_task = self.actor.create_task(
            self._send_nova_audio_to_twilio(),
            name=f"{self.actor.session_id}-nova-output",
        )
        log_event(
            logger,
            logging.INFO,
            "nova_stream_started",
            session_id=self.actor.session_id,
            call_sid=self.actor.call_sid,
            persona_id=self.actor.persona_id,
            stream_sid=self.stream_sid,
        )

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True

        if not self._started:
            await self.nova_client.close()
            return

        self._stopping.set()

        await self._wait_or_cancel(self._input_task, timeout=1.0, task_name="nova_input")
        try:
            await self.nova_client.send_event(content_end_event(self.prompt_name, self.audio_content_name))
            await self.nova_client.send_event(prompt_end_event(self.prompt_name))
            await self.nova_client.send_event(session_end_event())
        finally:
            await self.nova_client.close()

        await self._wait_or_cancel(self._output_task, timeout=0.1, task_name="nova_output")
        log_event(
            logger,
            logging.INFO,
            "nova_stream_stopped",
            session_id=self.actor.session_id,
            call_sid=self.actor.call_sid,
            persona_id=self.actor.persona_id,
            stream_sid=self.stream_sid,
            nova_events=self._nova_events,
        )

    async def _send_inbound_audio_to_nova(self) -> None:
        while True:
            if self._stopping.is_set() and self.actor.inbound_audio_queue.empty():
                return
            try:
                pcm16_audio = await asyncio.wait_for(self.actor.inbound_audio_queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            await self.nova_client.send_event(
                audio_input_event(self.prompt_name, self.audio_content_name, pcm16_audio)
            )

    async def _send_nova_audio_to_twilio(self) -> None:
        while not self._stopping.is_set():
            try:
                event = await asyncio.wait_for(
                    self.nova_client.receive_event(),
                    timeout=self.settings.nova_response_timeout_seconds,
                )
            except TimeoutError:
                emit_error_count("nova_response_timeout")
                log_event(
                    logger,
                    logging.WARNING,
                    "nova_response_timeout",
                    session_id=self.actor.session_id,
                    call_sid=self.actor.call_sid,
                    persona_id=self.actor.persona_id,
                    stream_sid=self.stream_sid,
                    error_kind="nova_response_timeout",
                )
                continue
            except Exception as exc:
                error_kind = type(exc).__name__
                emit_error_count(error_kind)
                log_event(
                    logger,
                    logging.WARNING,
                    "nova_receive_error",
                    session_id=self.actor.session_id,
                    call_sid=self.actor.call_sid,
                    persona_id=self.actor.persona_id,
                    stream_sid=self.stream_sid,
                    error_kind=error_kind,
                )
                return

            self._nova_events += 1
            await self._handle_transcript_event(event)
            if event.event_type != "audio_output" or event.audio_bytes is None:
                continue

            payload = nova_pcm16_to_twilio_payload(event.audio_bytes)
            await self.websocket.send_json(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": payload},
                }
            )

    async def _handle_transcript_event(self, event: NovaParsedEvent) -> None:
        turn = self.transcript_buffer.handle_nova_event(event)
        if turn is None:
            return

        self.actor.transcript_buffer.finalized_turns.append(
            {
                "turn_index": turn.turn_index,
                "speaker": turn.speaker,
                "transcript_item_id": turn.transcript_item_id,
            }
        )
        self._record_turn_latency(turn.speaker)
        try:
            await put_transcript_turn_with_retry(
                repository=self.transcript_repository,
                turn=turn,
                settings=self.settings,
            )
        except TranscriptPersistenceError as exc:
            emit_error_count(exc.error_kind)
            log_event(
                logger,
                logging.WARNING,
                "transcript_turn_persist_failed",
                session_id=self.actor.session_id,
                call_sid=self.actor.call_sid,
                persona_id=self.actor.persona_id,
                turn_index=turn.turn_index,
                error_kind=exc.error_kind,
            )

    def _record_turn_latency(self, speaker: str) -> None:
        now = monotonic()
        if speaker == "caller":
            self._latest_caller_turn_at = now
            return
        if speaker != "assistant" or self._latest_caller_turn_at is None:
            return

        latency_ms = (now - self._latest_caller_turn_at) * 1000
        emit_turn_response_latency(self.actor.persona_id, latency_ms)
        log_event(
            logger,
            logging.INFO,
            "turn_response_latency_recorded",
            session_id=self.actor.session_id,
            call_sid=self.actor.call_sid,
            persona_id=self.actor.persona_id,
            latency_ms=round(latency_ms, 2),
        )
        self._latest_caller_turn_at = None

    async def _wait_or_cancel(self, task: asyncio.Task | None, *, timeout: float, task_name: str) -> None:
        if task is None or task.done():
            if task is not None:
                self._log_task_exception(task, task_name=task_name)
            return
        done, pending = await asyncio.wait({task}, timeout=timeout)
        for completed in done:
            self._log_task_exception(completed, task_name=task_name)
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _log_task_exception(self, task: asyncio.Task, *, task_name: str) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            emit_error_count(type(exc).__name__)
            log_event(
                logger,
                logging.WARNING,
                "nova_bridge_task_error",
                session_id=self.actor.session_id,
                call_sid=self.actor.call_sid,
                persona_id=self.actor.persona_id,
                stream_sid=self.stream_sid,
                task_name=task_name,
                error_kind=type(exc).__name__,
            )
