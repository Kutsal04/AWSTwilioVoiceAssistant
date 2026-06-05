import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings, get_settings
from app.main import app
from app.nova import NovaParsedEvent
from app.sessions import SessionActor, SessionState, active_sessions, new_session_record
from app.transcripts import TranscriptRepository, TranscriptTurn, get_transcript_repository
from app.twilio.media import (
    TwilioMediaProtocolError,
    extract_mark_name,
    extract_media_payload,
    extract_start_metadata,
    get_nova_client_factory,
    parse_twilio_event,
)
from tests.test_twilio_webhook import (
    FakeSessionRepository,
    make_persona_repository,
    override_persona_repository,
    override_session_repository,
    override_settings,
)


class FakeNovaClient:
    def __init__(self, output_events: list[NovaParsedEvent] | None = None) -> None:
        self.opened = False
        self.closed = False
        self.sent_events: list[dict] = []
        self.output_events = output_events or []

    async def open(self) -> None:
        self.opened = True

    async def send_event(self, event: dict) -> None:
        self.sent_events.append(event)

    async def receive_event(self) -> NovaParsedEvent:
        if self.output_events:
            return self.output_events.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class FakeTranscriptRepository:
    def __init__(self, fail_writes: int = 0) -> None:
        self.turns: list[TranscriptTurn] = []
        self.fail_writes = fail_writes

    def put_turn(self, turn: TranscriptTurn) -> None:
        if self.fail_writes:
            self.fail_writes -= 1
            raise RuntimeError("put failed")
        self.turns.append(turn)

    def list_turns(self, session_id: str) -> list[TranscriptTurn]:
        return [turn for turn in self.turns if turn.session_id == session_id]


@contextmanager
def override_nova_client(fake_client: FakeNovaClient) -> Iterator[None]:
    app.dependency_overrides[get_nova_client_factory] = lambda: lambda: fake_client
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_nova_client_factory, None)


@contextmanager
def override_transcript_repository(repository: TranscriptRepository | None = None) -> Iterator[TranscriptRepository]:
    fake_repository = repository or FakeTranscriptRepository()
    app.dependency_overrides[get_transcript_repository] = lambda: fake_repository
    try:
        yield fake_repository
    finally:
        app.dependency_overrides.pop(get_transcript_repository, None)


def connected_event() -> dict[str, object]:
    return {"event": "connected", "protocol": "Call", "version": "1.0.0"}


def start_event(
    session_id: str = "session-123",
    persona_id: str = "appointment_reminder",
    call_sid: str = "CA123",
    stream_sid: str = "MZ123",
) -> dict[str, object]:
    return {
        "event": "start",
        "streamSid": stream_sid,
        "start": {
            "streamSid": stream_sid,
            "callSid": call_sid,
            "customParameters": {
                "session_id": session_id,
                "persona_id": persona_id,
            },
        },
    }


def media_event() -> dict[str, object]:
    return {
        "event": "media",
        "streamSid": "MZ123",
        "media": {
            "track": "inbound",
            "chunk": "1",
            "timestamp": "1",
            "payload": "/w==",
        },
    }


def stop_event() -> dict[str, object]:
    return {"event": "stop", "streamSid": "MZ123", "stop": {"accountSid": "AC123", "callSid": "CA123"}}


def media_session_repository(
    *,
    session_id: str = "session-123",
    call_sid: str = "CA123",
    persona_id: str = "appointment_reminder",
):
    repository = FakeSessionRepository()
    repository.create_session(
        new_session_record(
            session_id=session_id,
            call_sid=call_sid,
            persona_id=persona_id,
        )
    )
    return repository


def mark_event(name: str) -> dict[str, object]:
    return {"event": "mark", "streamSid": "MZ123", "mark": {"name": name}}


def receive_outbound_media_and_ack_mark(websocket) -> dict:
    outbound = websocket.receive_json()
    mark = websocket.receive_json()
    assert mark["event"] == "mark"
    websocket.send_json(mark_event(mark["mark"]["name"]))
    return outbound


def test_parse_twilio_event_accepts_known_events() -> None:
    parsed = parse_twilio_event('{"event":"connected"}')

    assert parsed == {"event": "connected"}
    assert parse_twilio_event('{"event":"mark","mark":{"name":"audio-1"}}') == {
        "event": "mark",
        "mark": {"name": "audio-1"},
    }


def test_parse_twilio_event_rejects_malformed_json() -> None:
    with pytest.raises(TwilioMediaProtocolError):
        parse_twilio_event("not-json")


def test_parse_twilio_event_rejects_unknown_events() -> None:
    with pytest.raises(TwilioMediaProtocolError):
        parse_twilio_event('{"event":"dtmf"}')


def test_extract_start_metadata_reads_stream_parameters() -> None:
    metadata = extract_start_metadata(start_event())

    assert metadata.session_id == "session-123"
    assert metadata.persona_id == "appointment_reminder"
    assert metadata.call_sid == "CA123"
    assert metadata.stream_sid == "MZ123"


def test_extract_media_payload_reads_payload() -> None:
    assert extract_media_payload(media_event()) == "/w=="


def test_extract_mark_name_reads_mark_payload() -> None:
    assert extract_mark_name(mark_event("assistant-audio-1")) == "assistant-audio-1"


@pytest.mark.parametrize(
    "event",
    [
        {"event": "start", "start": {}},
        {"event": "start", "start": {"callSid": "CA123", "streamSid": "MZ123", "customParameters": {}}},
        {
            "event": "start",
            "start": {
                "callSid": "CA123",
                "streamSid": "MZ123",
                "customParameters": {"session_id": "session-123"},
            },
        },
        {
            "event": "start",
            "start": {
                "streamSid": "MZ123",
                "customParameters": {"session_id": "session-123", "persona_id": "appointment_reminder"},
            },
        },
    ],
)
def test_extract_start_metadata_rejects_malformed_start_events(event: dict[str, object]) -> None:
    with pytest.raises(TwilioMediaProtocolError):
        extract_start_metadata(event)


def test_media_websocket_lifecycle_connected_started_stopped(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None)
        fake_nova = FakeNovaClient()
        sessions = media_session_repository()

        with (
            override_settings(settings),
            override_persona_repository(),
            override_session_repository(sessions),
            override_transcript_repository(),
            override_nova_client(fake_nova),
            caplog.at_level(logging.INFO, logger="app.twilio.media"),
        ):
            with client.websocket_connect("/media") as websocket:
                websocket.send_json(connected_event())
                websocket.send_json(start_event())
                websocket.send_json(media_event())
                websocket.send_json(stop_event())
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()

        assert exc_info.value.code == 1000
        assert await active_sessions.count() == 0
        assert fake_nova.opened is True
        assert fake_nova.closed is True
        assert [next(iter(event["event"])) for event in fake_nova.sent_events] == [
            "sessionStart",
            "promptStart",
            "contentStart",
            "textInput",
            "contentEnd",
            "contentStart",
            "audioInput",
            "contentEnd",
            "promptEnd",
            "sessionEnd",
        ]
        text_inputs = [event["event"]["textInput"] for event in fake_nova.sent_events if "textInput" in event["event"]]
        assert text_inputs[0]["content"] == "appointment prompt"

    asyncio.run(run())

    assert "twilio_media_started" in caplog.messages
    assert "twilio_media_stopped" in caplog.messages
    assert "/w==" not in caplog.text
    structured_fields = [getattr(record, "fields", {}) for record in caplog.records]
    assert {
        "session_id": "session-123",
        "persona_id": "appointment_reminder",
        "call_sid": "CA123",
        "stream_sid": "MZ123",
    } in structured_fields


def test_media_websocket_closes_on_missing_stream_parameters() -> None:
    client = TestClient(app)
    settings = Settings(_env_file=None)

    with override_settings(settings), override_session_repository(), override_transcript_repository():
        with client.websocket_connect("/media") as websocket:
            websocket.send_json({"event": "start", "start": {"callSid": "CA123", "streamSid": "MZ123"}})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()

    assert exc_info.value.code == 1008


def test_media_websocket_closes_when_media_arrives_before_start() -> None:
    client = TestClient(app)
    settings = Settings(_env_file=None)

    with override_settings(settings), override_session_repository(), override_transcript_repository():
        with client.websocket_connect("/media") as websocket:
            websocket.send_json(media_event())
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()

    assert exc_info.value.code == 1008


def test_media_websocket_idle_timeout() -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None, media_idle_timeout_seconds=0.01)

        with override_settings(settings), override_session_repository(), override_transcript_repository():
            with client.websocket_connect("/media") as websocket:
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()

        assert exc_info.value.code == 1001
        assert await active_sessions.count() == 0

    asyncio.run(run())


def test_media_websocket_sends_nova_audio_back_to_twilio() -> None:
    client = TestClient(app)
    settings = Settings(_env_file=None)
    sessions = media_session_repository()
    fake_nova = FakeNovaClient(
        [
            NovaParsedEvent(
                event_type="audio_output",
                raw_event={"event": {"audioOutput": {}}},
                audio_bytes=b"\x00\x00" * 240,
                content_name="audio-1",
            )
        ]
    )

    with override_settings(settings), override_persona_repository(), override_session_repository(sessions), override_transcript_repository(), override_nova_client(fake_nova):
        with client.websocket_connect("/media") as websocket:
            websocket.send_json(connected_event())
            websocket.send_json(start_event())
            outbound = receive_outbound_media_and_ack_mark(websocket)
            websocket.send_json(stop_event())
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()

    assert outbound["event"] == "media"
    assert outbound["streamSid"] == "MZ123"
    assert outbound["media"]["payload"]
    assert exc_info.value.code == 1000


def test_media_websocket_closes_on_bad_audio_payload() -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None)
        fake_nova = FakeNovaClient()
        sessions = media_session_repository()

        with override_settings(settings), override_persona_repository(), override_session_repository(sessions), override_transcript_repository(), override_nova_client(fake_nova):
            with client.websocket_connect("/media") as websocket:
                websocket.send_json(connected_event())
                websocket.send_json(start_event())
                websocket.send_json({"event": "media", "streamSid": "MZ123", "media": {"payload": "bad payload"}})
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()

        assert exc_info.value.code == 1008
        assert fake_nova.closed is True
        assert await active_sessions.count() == 0

    asyncio.run(run())


def test_media_websocket_closes_when_persona_is_inactive() -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None, default_persona_id="appointment_reminder")
        fake_nova = FakeNovaClient()
        personas = make_persona_repository(appointment_active=False)
        sessions = media_session_repository()

        with override_settings(settings), override_persona_repository(personas), override_session_repository(sessions), override_transcript_repository(), override_nova_client(fake_nova):
            with client.websocket_connect("/media") as websocket:
                websocket.send_json(connected_event())
                websocket.send_json(start_event())
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()

        assert exc_info.value.code == 1008
        assert fake_nova.opened is False
        assert sessions.updates[-1][1]["status"] == SessionState.FAILED
        assert sessions.updates[-1][1]["error_kind"] == "inactive_persona"
        assert await active_sessions.count() == 0

    asyncio.run(run())


def test_media_websocket_closes_when_session_is_missing() -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None)
        fake_nova = FakeNovaClient()
        sessions = FakeSessionRepository()

        with override_settings(settings), override_persona_repository(), override_session_repository(sessions), override_transcript_repository(), override_nova_client(fake_nova):
            with client.websocket_connect("/media") as websocket:
                websocket.send_json(connected_event())
                websocket.send_json(start_event())
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()

        assert exc_info.value.code == 1008
        assert fake_nova.opened is False
        assert await active_sessions.count() == 0

    asyncio.run(run())


def test_media_websocket_closes_on_session_call_sid_mismatch() -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None)
        fake_nova = FakeNovaClient()
        sessions = media_session_repository(call_sid="CA-other")

        with override_settings(settings), override_persona_repository(), override_session_repository(sessions), override_transcript_repository(), override_nova_client(fake_nova):
            with client.websocket_connect("/media") as websocket:
                websocket.send_json(connected_event())
                websocket.send_json(start_event())
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()

        assert exc_info.value.code == 1008
        assert fake_nova.opened is False
        assert await active_sessions.count() == 0

    asyncio.run(run())


def test_media_websocket_closes_on_session_persona_mismatch() -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None)
        fake_nova = FakeNovaClient()
        sessions = media_session_repository(persona_id="warm_clinical_followup")

        with override_settings(settings), override_persona_repository(), override_session_repository(sessions), override_transcript_repository(), override_nova_client(fake_nova):
            with client.websocket_connect("/media") as websocket:
                websocket.send_json(connected_event())
                websocket.send_json(start_event(persona_id="appointment_reminder"))
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()

        assert exc_info.value.code == 1008
        assert fake_nova.opened is False
        assert await active_sessions.count() == 0

    asyncio.run(run())


def test_media_websocket_closes_on_duplicate_active_actor() -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None)
        fake_nova = FakeNovaClient()
        sessions = media_session_repository()
        actor = SessionActor(
            session_id="session-123",
            call_sid="CA123",
            persona_id="appointment_reminder",
            audio_queue_maxsize=2,
        )
        await actor.activate()
        await active_sessions.create(actor)

        try:
            with override_settings(settings), override_persona_repository(), override_session_repository(sessions), override_transcript_repository(), override_nova_client(fake_nova):
                with client.websocket_connect("/media") as websocket:
                    websocket.send_json(connected_event())
                    websocket.send_json(start_event())
                    with pytest.raises(WebSocketDisconnect) as exc_info:
                        websocket.receive_text()

            assert exc_info.value.code == 1008
            assert fake_nova.opened is False
            assert await active_sessions.count() == 1
        finally:
            await active_sessions.clear()

    asyncio.run(run())


def test_media_websocket_reattaches_existing_active_session(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None)
        fake_nova = FakeNovaClient()
        sessions = media_session_repository()
        sessions.attach_media_stream(
            "session-123",
            call_sid="CA123",
            persona_id="appointment_reminder",
            stream_sid="MZ-old",
        )

        with (
            override_settings(settings),
            override_persona_repository(),
            override_session_repository(sessions),
            override_transcript_repository(),
            override_nova_client(fake_nova),
            caplog.at_level(logging.INFO, logger="app.twilio.media"),
        ):
            with client.websocket_connect("/media") as websocket:
                websocket.send_json(connected_event())
                websocket.send_json(start_event(stream_sid="MZ-new"))
                websocket.send_json(stop_event())
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()

        assert exc_info.value.code == 1000
        assert fake_nova.opened is True
        assert fake_nova.closed is True
        assert await active_sessions.count() == 0
        assert sessions.sessions["session-123"].media_attach_count == 2
        assert sessions.sessions["session-123"].stream_sid == "MZ-new"
        assert sessions.sessions["session-123"].recovery_reason == "media_reattach"

    asyncio.run(run())

    assert "twilio_media_reattached" in caplog.messages


def test_media_websocket_finalizes_completed_session_on_stop() -> None:
    async def run() -> None:
        await active_sessions.clear()
        client = TestClient(app)
        settings = Settings(_env_file=None)
        fake_nova = FakeNovaClient()
        sessions = media_session_repository()

        with override_settings(settings), override_persona_repository(), override_session_repository(sessions), override_transcript_repository(), override_nova_client(fake_nova):
            with client.websocket_connect("/media") as websocket:
                websocket.send_json(connected_event())
                websocket.send_json(start_event())
                websocket.send_json(stop_event())
                with pytest.raises(WebSocketDisconnect):
                    websocket.receive_text()

        assert sessions.updates[0][1]["status"] == SessionState.ACTIVE
        assert sessions.updates[-1][1]["status"] == SessionState.COMPLETED
        assert sessions.updates[-1][1]["call_sid"] == "CA123"
        assert sessions.updates[-1][1]["outcome_description"] == "twilio_stop"

    asyncio.run(run())


def test_media_websocket_persists_finalized_nova_transcript_turn_without_logging_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(app)
    settings = Settings(_env_file=None)
    transcript_repository = FakeTranscriptRepository()
    sessions = media_session_repository()
    fake_nova = FakeNovaClient(
        [
            NovaParsedEvent(
                event_type="content_start",
                raw_event={"event": {"contentStart": {"role": "ASSISTANT", "contentId": "assistant-1"}}},
                role="ASSISTANT",
                generation_stage="FINAL",
                content_type="TEXT",
                content_name="assistant-1",
            ),
            NovaParsedEvent(
                event_type="text_output",
                raw_event={"event": {"textOutput": {"contentId": "assistant-1", "content": "Sensitive answer."}}},
                content="Sensitive ",
                content_name="assistant-1",
            ),
            NovaParsedEvent(
                event_type="text_output",
                raw_event={"event": {"textOutput": {"contentId": "assistant-1", "content": "answer."}}},
                content="answer.",
                content_name="assistant-1",
            ),
            NovaParsedEvent(
                event_type="content_end",
                raw_event={"event": {"contentEnd": {"contentId": "assistant-1"}}},
                content_name="assistant-1",
            ),
            NovaParsedEvent(
                event_type="audio_output",
                raw_event={"event": {"audioOutput": {}}},
                audio_bytes=b"\x00\x00" * 240,
                content_name="audio-1",
            ),
        ]
    )

    with (
        override_settings(settings),
        override_persona_repository(),
        override_session_repository(sessions),
        override_transcript_repository(transcript_repository),
        override_nova_client(fake_nova),
        caplog.at_level(logging.INFO),
    ):
        with client.websocket_connect("/media") as websocket:
            websocket.send_json(connected_event())
            websocket.send_json(start_event())
            receive_outbound_media_and_ack_mark(websocket)
            websocket.send_json(stop_event())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()

    assert len(transcript_repository.turns) == 1
    turn = transcript_repository.turns[0]
    assert turn.session_id == "session-123"
    assert turn.turn_index == 0
    assert turn.speaker == "assistant"
    assert turn.text == "Sensitive answer."
    assert "Sensitive answer." not in caplog.text


def test_media_websocket_does_not_persist_speculative_nova_transcript_turn() -> None:
    client = TestClient(app)
    settings = Settings(_env_file=None)
    transcript_repository = FakeTranscriptRepository()
    sessions = media_session_repository()
    fake_nova = FakeNovaClient(
        [
            NovaParsedEvent(
                event_type="content_start",
                raw_event={"event": {"contentStart": {"role": "ASSISTANT", "contentId": "assistant-1"}}},
                role="ASSISTANT",
                generation_stage="SPECULATIVE",
                content_type="TEXT",
                content_name="assistant-1",
            ),
            NovaParsedEvent(
                event_type="text_output",
                raw_event={"event": {"textOutput": {"contentId": "assistant-1", "content": "Preview."}}},
                content="Preview.",
                content_name="assistant-1",
            ),
            NovaParsedEvent(
                event_type="content_end",
                raw_event={"event": {"contentEnd": {"contentId": "assistant-1"}}},
                content_name="assistant-1",
            ),
            NovaParsedEvent(
                event_type="audio_output",
                raw_event={"event": {"audioOutput": {}}},
                audio_bytes=b"\x00\x00" * 240,
                content_name="audio-1",
            ),
        ]
    )

    with (
        override_settings(settings),
        override_persona_repository(),
        override_session_repository(sessions),
        override_transcript_repository(transcript_repository),
        override_nova_client(fake_nova),
    ):
        with client.websocket_connect("/media") as websocket:
            websocket.send_json(connected_event())
            websocket.send_json(start_event())
            receive_outbound_media_and_ack_mark(websocket)
            websocket.send_json(stop_event())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()

    assert transcript_repository.turns == []
