import asyncio
import logging

import pytest

from app.sessions import InvalidSessionTransition, SessionActor, SessionRegistry, SessionState


def make_actor(session_id: str = "session-1", *, maxsize: int = 2) -> SessionActor:
    return SessionActor(
        session_id=session_id,
        call_sid=f"CA-{session_id}",
        persona_id="appointment_reminder",
        audio_queue_maxsize=maxsize,
    )


def drain_queue(queue: asyncio.Queue[bytes]) -> list[bytes]:
    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    return frames


def test_session_actor_lifecycle_transitions() -> None:
    async def run() -> None:
        actor = make_actor()

        assert actor.state == SessionState.STARTING
        await actor.activate()
        assert actor.state == SessionState.ACTIVE
        await actor.drain()
        assert actor.state == SessionState.DRAINING
        await actor.complete()
        assert actor.state == SessionState.COMPLETED

    asyncio.run(run())


def test_session_actor_rejects_invalid_lifecycle_transition() -> None:
    async def run() -> None:
        actor = make_actor()

        with pytest.raises(InvalidSessionTransition):
            await actor.complete()

    asyncio.run(run())


@pytest.mark.parametrize("terminal_action, expected_state", [("fail", SessionState.FAILED), ("abandon", SessionState.ABANDONED)])
def test_session_actor_terminal_states_cancel_tasks(terminal_action: str, expected_state: SessionState) -> None:
    async def run() -> None:
        actor = make_actor()
        await actor.activate()

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = actor.create_task(wait_forever(), name="test-session-task")
        await getattr(actor, terminal_action)()

        assert actor.state == expected_state
        assert task.cancelled()
        assert actor.tasks == set()

    asyncio.run(run())


def test_inbound_queue_overflow_drops_oldest_frame_and_logs_metric(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        actor = make_actor(maxsize=2)
        await actor.activate()

        await actor.enqueue_inbound_audio(b"oldest")
        await actor.enqueue_inbound_audio(b"middle")
        with caplog.at_level(logging.WARNING, logger="app.sessions.actor"):
            result = await actor.enqueue_inbound_audio(b"newest")

        assert result.accepted is True
        assert result.dropped_frames == 1
        assert result.queue_depth == 2
        assert result.metric == {
            "metric_name": "AudioFrameDropped",
            "value": 1,
            "dimensions": {"direction": "inbound", "persona_id": "appointment_reminder"},
            "unit": "Count",
        }
        assert actor.dropped_inbound_frames == 1
        assert drain_queue(actor.inbound_audio_queue) == [b"middle", b"newest"]

    asyncio.run(run())
    assert "audio_frame_dropped" in caplog.messages


def test_outbound_queue_overflow_is_independent_from_inbound_queue() -> None:
    async def run() -> None:
        actor = make_actor(maxsize=1)
        await actor.activate()

        await actor.enqueue_inbound_audio(b"inbound-1")
        result = await actor.enqueue_outbound_audio(b"outbound-1")
        overflow = await actor.enqueue_outbound_audio(b"outbound-2")

        assert result.dropped_frames == 0
        assert overflow.dropped_frames == 1
        assert actor.dropped_inbound_frames == 0
        assert actor.dropped_outbound_frames == 1
        assert drain_queue(actor.inbound_audio_queue) == [b"inbound-1"]
        assert drain_queue(actor.outbound_audio_queue) == [b"outbound-2"]

    asyncio.run(run())


def test_terminal_actor_rejects_new_audio_without_mutating_queue() -> None:
    async def run() -> None:
        actor = make_actor(maxsize=2)
        await actor.activate()
        await actor.enqueue_inbound_audio(b"before-complete")
        await actor.drain()
        await actor.complete()

        result = await actor.enqueue_inbound_audio(b"after-complete")

        assert result.accepted is False
        assert result.dropped_frames == 0
        assert drain_queue(actor.inbound_audio_queue) == [b"before-complete"]

    asyncio.run(run())


def test_session_registry_create_get_remove_cleanup() -> None:
    async def run() -> None:
        registry = SessionRegistry()
        actor = make_actor("session-registry")

        await registry.create(actor)

        assert await registry.get("session-registry") is actor
        assert await registry.count() == 1
        assert await registry.remove("session-registry") is actor
        assert await registry.get("session-registry") is None
        assert await registry.count() == 0

    asyncio.run(run())


def test_session_registry_rejects_duplicate_session_ids() -> None:
    async def run() -> None:
        registry = SessionRegistry()
        await registry.create(make_actor("duplicate"))

        with pytest.raises(ValueError):
            await registry.create(make_actor("duplicate"))

    asyncio.run(run())


def test_two_actors_do_not_share_mutable_call_state() -> None:
    async def run() -> None:
        actor_a = make_actor("session-a", maxsize=2)
        actor_b = make_actor("session-b", maxsize=2)

        async def drive_actor(actor: SessionActor, frame: bytes) -> None:
            await actor.activate()
            actor.transcript_buffer.partial_turns.append({"speaker": "caller", "item": actor.session_id})
            await actor.enqueue_inbound_audio(frame)
            await actor.drain()

        await asyncio.gather(
            drive_actor(actor_a, b"a-frame"),
            drive_actor(actor_b, b"b-frame"),
        )

        assert actor_a.state == SessionState.DRAINING
        assert actor_b.state == SessionState.DRAINING
        assert drain_queue(actor_a.inbound_audio_queue) == [b"a-frame"]
        assert drain_queue(actor_b.inbound_audio_queue) == [b"b-frame"]
        assert actor_a.transcript_buffer.partial_turns == [{"speaker": "caller", "item": "session-a"}]
        assert actor_b.transcript_buffer.partial_turns == [{"speaker": "caller", "item": "session-b"}]

    asyncio.run(run())
