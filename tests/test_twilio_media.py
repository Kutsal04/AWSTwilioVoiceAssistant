import logging

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings, get_settings
from app.main import app
from app.twilio.media import TwilioMediaProtocolError, extract_start_metadata, parse_twilio_event
from tests.test_twilio_webhook import override_settings


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
            "payload": "base64-audio-should-not-be-logged",
        },
    }


def stop_event() -> dict[str, object]:
    return {"event": "stop", "streamSid": "MZ123", "stop": {"accountSid": "AC123", "callSid": "CA123"}}


def test_parse_twilio_event_accepts_known_events() -> None:
    parsed = parse_twilio_event('{"event":"connected"}')

    assert parsed == {"event": "connected"}


def test_parse_twilio_event_rejects_malformed_json() -> None:
    with pytest.raises(TwilioMediaProtocolError):
        parse_twilio_event("not-json")


def test_parse_twilio_event_rejects_unknown_events() -> None:
    with pytest.raises(TwilioMediaProtocolError):
        parse_twilio_event('{"event":"mark"}')


def test_extract_start_metadata_reads_stream_parameters() -> None:
    metadata = extract_start_metadata(start_event())

    assert metadata.session_id == "session-123"
    assert metadata.persona_id == "appointment_reminder"
    assert metadata.call_sid == "CA123"
    assert metadata.stream_sid == "MZ123"


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
    client = TestClient(app)
    settings = Settings(_env_file=None)

    with override_settings(settings), caplog.at_level(logging.INFO, logger="app.twilio.media"):
        with client.websocket_connect("/media") as websocket:
            websocket.send_json(connected_event())
            websocket.send_json(start_event())
            websocket.send_json(media_event())
            websocket.send_json(stop_event())
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()

    assert exc_info.value.code == 1000
    assert "twilio_media_started" in caplog.messages
    assert "twilio_media_stopped" in caplog.messages
    assert "base64-audio-should-not-be-logged" not in caplog.text
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

    with override_settings(settings):
        with client.websocket_connect("/media") as websocket:
            websocket.send_json({"event": "start", "start": {"callSid": "CA123", "streamSid": "MZ123"}})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()

    assert exc_info.value.code == 1008


def test_media_websocket_closes_when_media_arrives_before_start() -> None:
    client = TestClient(app)
    settings = Settings(_env_file=None)

    with override_settings(settings):
        with client.websocket_connect("/media") as websocket:
            websocket.send_json(media_event())
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()

    assert exc_info.value.code == 1008


def test_media_websocket_idle_timeout() -> None:
    client = TestClient(app)
    settings = Settings(_env_file=None, media_idle_timeout_seconds=0.01)

    with override_settings(settings):
        with client.websocket_connect("/media") as websocket:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()

    assert exc_info.value.code == 1001
