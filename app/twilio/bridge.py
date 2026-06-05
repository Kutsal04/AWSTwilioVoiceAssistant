import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from uuid import uuid4

from fastapi import WebSocket

from app.audio import has_voice_activity, nova_pcm16_to_twilio_payload
from app.config import Settings
from app.logging import log_event
from app.metrics import emit_audio_frame_dropped, emit_barge_in_count, emit_error_count, emit_turn_response_latency
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


@dataclass(frozen=True)
class OutboundAudioFrame:
    payload: str
    content_name: str | None


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
        self._twilio_sender_task: asyncio.Task | None = None
        self._outbound_audio_queue: asyncio.Queue[OutboundAudioFrame] = asyncio.Queue(
            maxsize=settings.audio_queue_maxsize
        )
        self._nova_events = 0
        self._latest_caller_turn_at: float | None = None
        self._twilio_send_lock = asyncio.Lock()
        self._pending_marks: dict[str, str | None] = {}
        self._mark_sequence = 0
        self._assistant_audio_active_until: float | None = None
        self._active_assistant_audio_content_name: str | None = None
        self._interrupted_audio_content_names: set[str] = set()
        self._barge_in_count = 0

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
        self._twilio_sender_task = self.actor.create_task(
            self._send_outbound_audio_to_twilio(),
            name=f"{self.actor.session_id}-twilio-output",
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

    async def observe_inbound_audio(self, pcm16_audio: bytes) -> None:
        if not self.settings.barge_in_enabled or not self._assistant_audio_is_active():
            return
        if not has_voice_activity(pcm16_audio, rms_threshold=self.settings.barge_in_rms_threshold):
            return

        content_name = self._active_assistant_audio_content_name
        if content_name is not None:
            self._interrupted_audio_content_names.add(content_name)
        self._assistant_audio_active_until = None
        self._barge_in_count += 1
        cleared_local_frames = self._clear_outbound_audio_queue()
        self._clear_pending_marks()

        await self._send_twilio_clear()
        emit_barge_in_count(self.actor.persona_id)
        log_event(
            logger,
            logging.INFO,
            "barge_in_detected",
            session_id=self.actor.session_id,
            call_sid=self.actor.call_sid,
            persona_id=self.actor.persona_id,
            stream_sid=self.stream_sid,
            interrupted_content_name=content_name,
            barge_in_count=self._barge_in_count,
            cleared_local_frames=cleared_local_frames,
        )

    async def handle_twilio_mark(self, mark_name: str) -> None:
        self._pending_marks.pop(mark_name, None)
        if not self._pending_marks and self._outbound_audio_queue.empty():
            self._assistant_audio_active_until = None

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
        await self._wait_or_cancel(self._twilio_sender_task, timeout=0.5, task_name="twilio_output")
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
            if self._is_interrupted_audio_event(event):
                continue

            payload = nova_pcm16_to_twilio_payload(event.audio_bytes)
            await self._enqueue_outbound_audio(OutboundAudioFrame(
                payload=payload,
                content_name=event.content_name,
            ))

    async def _send_outbound_audio_to_twilio(self) -> None:
        while not self._stopping.is_set():
            try:
                frame = await asyncio.wait_for(self._outbound_audio_queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            if frame.content_name is not None and frame.content_name in self._interrupted_audio_content_names:
                continue
            if self._stopping.is_set():
                return
            await self._send_twilio_media(frame)

    async def _enqueue_outbound_audio(self, frame: OutboundAudioFrame) -> None:
        dropped_frames = 0
        if self._outbound_audio_queue.full():
            self._outbound_audio_queue.get_nowait()
            dropped_frames = 1
            self.actor.dropped_outbound_frames += 1
        self._outbound_audio_queue.put_nowait(frame)
        if dropped_frames:
            emit_audio_frame_dropped("outbound", self.actor.persona_id, dropped_frames)
            log_event(
                logger,
                logging.WARNING,
                "audio_frame_dropped",
                session_id=self.actor.session_id,
                call_sid=self.actor.call_sid,
                persona_id=self.actor.persona_id,
                direction="outbound",
                dropped_frames=dropped_frames,
                queue_depth=self._outbound_audio_queue.qsize(),
            )

    def _clear_outbound_audio_queue(self) -> int:
        cleared_frames = 0
        while True:
            try:
                self._outbound_audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                return cleared_frames
            cleared_frames += 1

    def _clear_pending_marks(self) -> None:
        self._pending_marks.clear()

    def _assistant_audio_is_active(self) -> bool:
        if self._pending_marks or not self._outbound_audio_queue.empty():
            return True
        return self._assistant_audio_active_until is not None and monotonic() <= self._assistant_audio_active_until

    def _is_interrupted_audio_event(self, event: NovaParsedEvent) -> bool:
        return event.content_name is not None and event.content_name in self._interrupted_audio_content_names

    async def _send_twilio_media(self, frame: OutboundAudioFrame) -> None:
        mark_name = self._next_mark_name()
        async with self._twilio_send_lock:
            if frame.content_name is not None and frame.content_name in self._interrupted_audio_content_names:
                return
            self._pending_marks[mark_name] = frame.content_name
            await self.websocket.send_json(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": frame.payload},
                }
            )
            await self.websocket.send_json(
                {
                    "event": "mark",
                    "streamSid": self.stream_sid,
                    "mark": {"name": mark_name},
                }
            )
            self._active_assistant_audio_content_name = frame.content_name
            self._assistant_audio_active_until = monotonic() + self.settings.barge_in_playback_grace_seconds

    async def _send_twilio_clear(self) -> None:
        async with self._twilio_send_lock:
            await self.websocket.send_json(
                {
                    "event": "clear",
                    "streamSid": self.stream_sid,
                }
            )

    def _next_mark_name(self) -> str:
        self._mark_sequence += 1
        return f"{self.actor.session_id}:assistant-audio:{self._mark_sequence}"

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
