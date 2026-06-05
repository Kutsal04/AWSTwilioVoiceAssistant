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


class QueuedNovaClient(TimeoutNovaClient):
    def __init__(self) -> None:
        super().__init__()
        self.output_events: asyncio.Queue[NovaParsedEvent] = asyncio.Queue()

    async def receive_event(self) -> NovaParsedEvent:
        return await self.output_events.get()


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


def make_bridge(
    nova_client,
    *,
    settings: Settings | None = None,
    websocket: FakeWebSocket | None = None,
) -> TwilioNovaBridge:
    return TwilioNovaBridge(
        actor=make_actor(),
        websocket=websocket or FakeWebSocket(),
        stream_sid="MZ123",
        settings=settings or Settings(_env_file=None, nova_response_timeout_seconds=0.01),
        nova_client=nova_client,
        transcript_repository=FakeTranscriptRepository(),
        system_prompt="test prompt",
    )


def audio_output_event(content_name: str, audio_bytes: bytes | None = None) -> NovaParsedEvent:
    return NovaParsedEvent(
        event_type="audio_output",
        raw_event={"event": {"audioOutput": {"contentId": content_name}}},
        audio_bytes=audio_bytes or (b"\x00\x00" * 240),
        content_name=content_name,
    )


async def wait_for_message_count(websocket: FakeWebSocket, count: int) -> None:
    for _ in range(50):
        if len(websocket.messages) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} websocket messages, got {len(websocket.messages)}")


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


def test_barge_in_sends_clear_and_suppresses_interrupted_assistant_audio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        websocket = FakeWebSocket()
        nova_client = QueuedNovaClient()
        settings = Settings(
            _env_file=None,
            nova_response_timeout_seconds=0.5,
            barge_in_rms_threshold=500.0,
            barge_in_playback_grace_seconds=1.0,
        )
        bridge = make_bridge(nova_client, settings=settings, websocket=websocket)

        with caplog.at_level(logging.INFO, logger="app.twilio.bridge"):
            await bridge.start()
            await nova_client.output_events.put(audio_output_event("assistant-response-1"))
            await wait_for_message_count(websocket, 2)

            loud_caller_audio = (2000).to_bytes(2, "little", signed=True) * 160
            await bridge.observe_inbound_audio(loud_caller_audio)

            await nova_client.output_events.put(audio_output_event("assistant-response-1"))
            await asyncio.sleep(0.03)
            await nova_client.output_events.put(audio_output_event("assistant-response-2"))
            await wait_for_message_count(websocket, 5)
            await bridge.stop()

        assert websocket.messages[0]["event"] == "media"
        assert websocket.messages[1]["event"] == "mark"
        assert websocket.messages[2] == {"event": "clear", "streamSid": "MZ123"}
        assert websocket.messages[3]["event"] == "media"
        assert websocket.messages[4]["event"] == "mark"
        assert "barge_in_detected" in caplog.messages

    asyncio.run(run())


def test_barge_in_does_not_trigger_for_quiet_audio_or_when_disabled() -> None:
    async def run() -> None:
        quiet_websocket = FakeWebSocket()
        quiet_client = QueuedNovaClient()
        quiet_bridge = make_bridge(
            quiet_client,
            settings=Settings(
                _env_file=None,
                nova_response_timeout_seconds=0.5,
                barge_in_rms_threshold=500.0,
                barge_in_playback_grace_seconds=1.0,
            ),
            websocket=quiet_websocket,
        )
        await quiet_bridge.start()
        await quiet_client.output_events.put(audio_output_event("assistant-response-1"))
        await wait_for_message_count(quiet_websocket, 2)
        await quiet_bridge.observe_inbound_audio((100).to_bytes(2, "little", signed=True) * 160)
        await asyncio.sleep(0.01)
        await quiet_bridge.stop()

        disabled_websocket = FakeWebSocket()
        disabled_client = QueuedNovaClient()
        disabled_bridge = make_bridge(
            disabled_client,
            settings=Settings(
                _env_file=None,
                nova_response_timeout_seconds=0.5,
                barge_in_enabled=False,
                barge_in_rms_threshold=500.0,
                barge_in_playback_grace_seconds=1.0,
            ),
            websocket=disabled_websocket,
        )
        await disabled_bridge.start()
        await disabled_client.output_events.put(audio_output_event("assistant-response-1"))
        await wait_for_message_count(disabled_websocket, 2)
        await disabled_bridge.observe_inbound_audio((2000).to_bytes(2, "little", signed=True) * 160)
        await asyncio.sleep(0.01)
        await disabled_bridge.stop()

        assert [message["event"] for message in quiet_websocket.messages] == ["media", "mark"]
        assert [message["event"] for message in disabled_websocket.messages] == ["media", "mark"]

    asyncio.run(run())


def test_outbound_audio_sends_smoothly_without_waiting_for_mark_ack() -> None:
    async def run() -> None:
        websocket = FakeWebSocket()
        nova_client = QueuedNovaClient()
        bridge = make_bridge(
            nova_client,
            settings=Settings(
                _env_file=None,
                nova_response_timeout_seconds=0.5,
            ),
            websocket=websocket,
        )

        await bridge.start()
        await nova_client.output_events.put(audio_output_event("assistant-response-1"))
        await nova_client.output_events.put(audio_output_event("assistant-response-2"))
        await wait_for_message_count(websocket, 4)
        await bridge.stop()

        assert [message["event"] for message in websocket.messages] == ["media", "mark", "media", "mark"]

    asyncio.run(run())
