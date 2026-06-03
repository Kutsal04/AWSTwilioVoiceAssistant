import asyncio
import logging

import pytest

from app.config import Settings
from app.nova import NovaParsedEvent
from app.sessions import SessionActor
from app.transcripts import TranscriptTurn
from app.twilio.bridge import TwilioNovaBridge


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)


class TimeoutNovaClient:
    def __init__(self) -> None:
        self.closed = False
        self.sent_events: list[dict] = []

    async def open(self) -> None:
        return None

    async def send_event(self, event: dict) -> None:
        self.sent_events.append(event)

    async def receive_event(self) -> NovaParsedEvent:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class ErrorNovaClient(TimeoutNovaClient):
    async def receive_event(self) -> NovaParsedEvent:
        raise RuntimeError("nova receive failed")


class FakeTranscriptRepository:
    def put_turn(self, turn: TranscriptTurn) -> None:
        return None

    def list_turns(self, session_id: str) -> list[TranscriptTurn]:
        return []


def make_actor() -> SessionActor:
    return SessionActor(
        session_id="session-bridge",
        call_sid="CA123",
        persona_id="warm_clinical_followup",
        audio_queue_maxsize=2,
    )


def make_bridge(nova_client) -> TwilioNovaBridge:
    return TwilioNovaBridge(
        actor=make_actor(),
        websocket=FakeWebSocket(),
        stream_sid="MZ123",
        settings=Settings(_env_file=None, nova_response_timeout_seconds=0.01),
        nova_client=nova_client,
        transcript_repository=FakeTranscriptRepository(),
        system_prompt="test prompt",
    )


def test_nova_response_wait_timeout_is_bounded_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        bridge = make_bridge(TimeoutNovaClient())

        with caplog.at_level(logging.WARNING, logger="app.twilio.bridge"):
            await bridge.start()
            await asyncio.sleep(0.03)
            await bridge.stop()

        assert "nova_response_timeout" in caplog.messages

    asyncio.run(run())


def test_nova_receive_error_is_logged_without_raising_to_process(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        bridge = make_bridge(ErrorNovaClient())

        with caplog.at_level(logging.WARNING, logger="app.twilio.bridge"):
            await bridge.start()
            await asyncio.sleep(0.01)
            await bridge.stop()

        assert "nova_receive_error" in caplog.messages

    asyncio.run(run())
