import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal

from app.logging import log_event
from app.metrics import emit_audio_frame_dropped, metric_payload
from app.sessions.lifecycle import SessionState, TERMINAL_STATES, validate_transition

logger = logging.getLogger(__name__)

AudioDirection = Literal["inbound", "outbound"]


@dataclass(frozen=True)
class QueueWriteResult:
    accepted: bool
    dropped_frames: int
    queue_depth: int
    metric: dict[str, Any] | None = None


@dataclass
class TranscriptBuffer:
    partial_turns: list[dict[str, Any]] = field(default_factory=list)
    finalized_turns: list[dict[str, Any]] = field(default_factory=list)


class SessionActor:
    def __init__(
        self,
        *,
        session_id: str,
        call_sid: str,
        persona_id: str,
        audio_queue_maxsize: int = 50,
    ) -> None:
        if audio_queue_maxsize <= 0:
            raise ValueError("audio_queue_maxsize must be greater than zero")

        self.session_id = session_id
        self.call_sid = call_sid
        self.persona_id = persona_id
        self.state = SessionState.STARTING
        self.inbound_audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=audio_queue_maxsize)
        self.outbound_audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=audio_queue_maxsize)
        self.tasks: set[asyncio.Task[Any]] = set()
        self.transcript_buffer = TranscriptBuffer()
        self.dropped_inbound_frames = 0
        self.dropped_outbound_frames = 0
        self._lock = asyncio.Lock()

    async def activate(self) -> None:
        await self._transition(SessionState.ACTIVE)

    async def drain(self) -> None:
        await self._transition(SessionState.DRAINING)

    async def complete(self) -> None:
        await self._transition(SessionState.COMPLETED)
        await self.cancel_tasks()

    async def fail(self) -> None:
        await self._transition(SessionState.FAILED)
        await self.cancel_tasks()

    async def abandon(self) -> None:
        await self._transition(SessionState.ABANDONED)
        await self.cancel_tasks()

    async def enqueue_inbound_audio(self, frame: bytes) -> QueueWriteResult:
        return await self._enqueue_audio("inbound", self.inbound_audio_queue, frame)

    async def enqueue_outbound_audio(self, frame: bytes) -> QueueWriteResult:
        return await self._enqueue_audio("outbound", self.outbound_audio_queue, frame)

    def create_task(self, coroutine: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def cancel_tasks(self) -> None:
        if not self.tasks:
            return

        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.difference_update(tasks)

    async def _transition(self, next_state: SessionState) -> None:
        async with self._lock:
            validate_transition(self.state, next_state)
            self.state = next_state

    async def _enqueue_audio(
        self,
        direction: AudioDirection,
        queue: asyncio.Queue[bytes],
        frame: bytes,
    ) -> QueueWriteResult:
        async with self._lock:
            if self.state in TERMINAL_STATES:
                return QueueWriteResult(accepted=False, dropped_frames=0, queue_depth=queue.qsize())

            dropped_frames = 0
            if queue.full():
                queue.get_nowait()
                dropped_frames = 1
                if direction == "inbound":
                    self.dropped_inbound_frames += 1
                else:
                    self.dropped_outbound_frames += 1

            queue.put_nowait(frame)
            queue_depth = queue.qsize()

        metric = None
        if dropped_frames:
            metric = metric_payload(
                "AudioFrameDropped",
                dropped_frames,
                {"direction": direction, "persona_id": self.persona_id},
            )
            emit_audio_frame_dropped(direction, self.persona_id, dropped_frames)
            log_event(
                logger,
                logging.WARNING,
                "audio_frame_dropped",
                session_id=self.session_id,
                call_sid=self.call_sid,
                persona_id=self.persona_id,
                direction=direction,
                dropped_frames=dropped_frames,
                queue_depth=queue_depth,
            )

        return QueueWriteResult(
            accepted=True,
            dropped_frames=dropped_frames,
            queue_depth=queue_depth,
            metric=metric,
        )
