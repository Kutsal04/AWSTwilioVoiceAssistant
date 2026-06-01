import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status

from app.config import Settings, get_settings
from app.logging import log_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

TwilioEventName = Literal["connected", "start", "media", "stop"]


@dataclass(frozen=True)
class TwilioStartMetadata:
    session_id: str
    persona_id: str
    call_sid: str
    stream_sid: str


class TwilioMediaProtocolError(ValueError):
    pass


def parse_twilio_event(raw_message: str) -> dict[str, Any]:
    try:
        event = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        raise TwilioMediaProtocolError("message is not valid JSON") from exc

    if not isinstance(event, dict):
        raise TwilioMediaProtocolError("message must be a JSON object")

    event_name = event.get("event")
    if event_name not in {"connected", "start", "media", "stop"}:
        raise TwilioMediaProtocolError("unsupported Twilio media event")

    return event


def extract_start_metadata(event: dict[str, Any]) -> TwilioStartMetadata:
    if event.get("event") != "start":
        raise TwilioMediaProtocolError("expected start event")

    start = event.get("start")
    if not isinstance(start, dict):
        raise TwilioMediaProtocolError("start event is missing start payload")

    custom_parameters = start.get("customParameters")
    if not isinstance(custom_parameters, dict):
        raise TwilioMediaProtocolError("start event is missing custom parameters")

    session_id = _required_string(custom_parameters, "session_id")
    persona_id = _required_string(custom_parameters, "persona_id")
    call_sid = _required_string(start, "callSid")
    stream_sid = _optional_string(start, "streamSid") or _optional_string(event, "streamSid")
    if not stream_sid:
        raise TwilioMediaProtocolError("start event is missing streamSid")

    return TwilioStartMetadata(
        session_id=session_id,
        persona_id=persona_id,
        call_sid=call_sid,
        stream_sid=stream_sid,
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TwilioMediaProtocolError(f"start event is missing {key}")
    return value.strip()


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@router.websocket("/media")
async def media_websocket(websocket: WebSocket, settings: Settings = Depends(get_settings)) -> None:
    await websocket.accept()

    metadata: TwilioStartMetadata | None = None
    connected_seen = False
    started = False
    media_frames = 0

    try:
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=settings.media_idle_timeout_seconds,
                )
            except TimeoutError:
                log_event(logger, logging.WARNING, "twilio_media_idle_timeout", **_metadata_fields(metadata))
                await websocket.close(code=status.WS_1001_GOING_AWAY)
                return

            event = parse_twilio_event(raw_message)
            event_name = event["event"]

            if event_name == "connected":
                connected_seen = True
                log_event(logger, logging.INFO, "twilio_media_connected", event_name="connected")
                continue

            if event_name == "start":
                metadata = extract_start_metadata(event)
                started = True
                log_event(logger, logging.INFO, "twilio_media_started", **_metadata_fields(metadata))
                continue

            if event_name == "media":
                if not started:
                    log_event(logger, logging.WARNING, "twilio_media_before_start", error_kind="media_before_start")
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                media_frames += 1
                continue

            if event_name == "stop":
                log_event(
                    logger,
                    logging.INFO,
                    "twilio_media_stopped",
                    media_frames=media_frames,
                    **_metadata_fields(metadata),
                )
                await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                return

    except TwilioMediaProtocolError as exc:
        log_event(logger, logging.WARNING, "twilio_media_protocol_error", error_kind=type(exc).__name__, **_metadata_fields(metadata))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    except WebSocketDisconnect:
        if started:
            log_event(
                logger,
                logging.INFO,
                "twilio_media_disconnected",
                status="abandoned",
                media_frames=media_frames,
                **_metadata_fields(metadata),
            )
        elif connected_seen:
            log_event(logger, logging.INFO, "twilio_media_disconnected", status="abandoned")


def _metadata_fields(metadata: TwilioStartMetadata | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    return {
        "session_id": metadata.session_id,
        "persona_id": metadata.persona_id,
        "call_sid": metadata.call_sid,
        "stream_sid": metadata.stream_sid,
    }
