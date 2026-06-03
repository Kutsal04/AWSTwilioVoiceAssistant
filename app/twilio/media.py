import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status

from app.audio import AudioConversionError, twilio_payload_to_nova_pcm16
from app.config import Settings, get_settings
from app.logging import log_event
from app.nova import NovaClient
from app.personas import PersonaRepository, PersonaSelectionError, get_persona_repository, resolve_persona
from app.sessions import (
    SessionActor,
    SessionPersistenceError,
    SessionRepository,
    SessionState,
    active_sessions,
    finalize_session_with_retry,
    get_session_repository,
    update_session_with_retry,
)
from app.transcripts import TranscriptRepository, get_transcript_repository
from app.twilio.bridge import NovaClientFactory, TwilioNovaBridge

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


def get_nova_client_factory(settings: Settings = Depends(get_settings)) -> NovaClientFactory:
    return lambda: NovaClient(model_id=settings.nova_model_id, region=settings.bedrock_region)


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


def extract_media_payload(event: dict[str, Any]) -> str:
    if event.get("event") != "media":
        raise TwilioMediaProtocolError("expected media event")
    media = event.get("media")
    if not isinstance(media, dict):
        raise TwilioMediaProtocolError("media event is missing media payload")
    payload = media.get("payload")
    if not isinstance(payload, str):
        raise TwilioMediaProtocolError("media event is missing payload")
    return payload


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
async def media_websocket(
    websocket: WebSocket,
    settings: Settings = Depends(get_settings),
    nova_client_factory: NovaClientFactory = Depends(get_nova_client_factory),
    persona_repository: PersonaRepository = Depends(get_persona_repository),
    session_repository: SessionRepository = Depends(get_session_repository),
    transcript_repository: TranscriptRepository = Depends(get_transcript_repository),
) -> None:
    await websocket.accept()

    metadata: TwilioStartMetadata | None = None
    actor: SessionActor | None = None
    bridge: TwilioNovaBridge | None = None
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
                await _abandon_and_cleanup(actor, bridge, session_repository, settings, metadata)
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
                persona = await resolve_persona(
                    requested_persona_id=metadata.persona_id,
                    settings=settings,
                    repository=persona_repository,
                )
                actor = SessionActor(
                    session_id=metadata.session_id,
                    call_sid=metadata.call_sid,
                    persona_id=persona.persona_id,
                    audio_queue_maxsize=settings.audio_queue_maxsize,
                )
                await active_sessions.create(actor)
                await actor.activate()
                await _update_session(
                    repository=session_repository,
                    settings=settings,
                    metadata=metadata,
                    status=SessionState.ACTIVE,
                )
                bridge = TwilioNovaBridge(
                    actor=actor,
                    websocket=websocket,
                    stream_sid=metadata.stream_sid,
                    settings=settings,
                    nova_client=nova_client_factory(),
                    transcript_repository=transcript_repository,
                    system_prompt=persona.system_prompt,
                )
                await bridge.start()
                started = True
                log_event(logger, logging.INFO, "twilio_media_started", **_metadata_fields(metadata))
                continue

            if event_name == "media":
                if not started or actor is None:
                    log_event(logger, logging.WARNING, "twilio_media_before_start", error_kind="media_before_start")
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                payload = extract_media_payload(event)
                pcm16_audio = twilio_payload_to_nova_pcm16(payload)
                await actor.enqueue_inbound_audio(pcm16_audio)
                media_frames += 1
                continue

            if event_name == "stop":
                if actor is not None and bridge is not None:
                    await actor.drain()
                    await bridge.stop()
                    await actor.complete()
                    await _finalize_session(
                        repository=session_repository,
                        settings=settings,
                        metadata=metadata,
                        status=SessionState.COMPLETED,
                        outcome_description="twilio_stop",
                    )
                    await active_sessions.remove(actor.session_id)
                log_event(
                    logger,
                    logging.INFO,
                    "twilio_media_stopped",
                    media_frames=media_frames,
                    **_metadata_fields(metadata),
                )
                await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                return

    except (TwilioMediaProtocolError, AudioConversionError) as exc:
        log_event(logger, logging.WARNING, "twilio_media_protocol_error", error_kind=type(exc).__name__, **_metadata_fields(metadata))
        await _fail_and_cleanup(actor, bridge, session_repository, settings, metadata, type(exc).__name__)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    except PersonaSelectionError as exc:
        log_event(
            logger,
            logging.WARNING,
            "twilio_media_persona_rejected",
            error_kind=exc.error_kind,
            **_metadata_fields(metadata),
        )
        await _fail_and_cleanup(actor, bridge, session_repository, settings, metadata, exc.error_kind)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    except WebSocketDisconnect:
        if started:
            await _abandon_and_cleanup(actor, bridge, session_repository, settings, metadata)
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
    except Exception as exc:
        log_event(logger, logging.ERROR, "twilio_media_bridge_error", error_kind=type(exc).__name__, **_metadata_fields(metadata))
        await _fail_and_cleanup(actor, bridge, session_repository, settings, metadata, type(exc).__name__)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)


def _metadata_fields(metadata: TwilioStartMetadata | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    return {
        "session_id": metadata.session_id,
        "persona_id": metadata.persona_id,
        "call_sid": metadata.call_sid,
        "stream_sid": metadata.stream_sid,
    }


async def _update_session(
    *,
    repository: SessionRepository,
    settings: Settings,
    metadata: TwilioStartMetadata | None,
    status: SessionState,
    error_kind: str | None = None,
) -> None:
    if metadata is None:
        return
    try:
        await update_session_with_retry(
            repository=repository,
            session_id=metadata.session_id,
            settings=settings,
            status=status,
            call_sid=metadata.call_sid,
            error_kind=error_kind,
        )
    except SessionPersistenceError as exc:
        log_event(
            logger,
            logging.ERROR,
            "session_update_failed",
            error_kind=exc.error_kind,
            **_metadata_fields(metadata),
        )


async def _finalize_session(
    *,
    repository: SessionRepository,
    settings: Settings,
    metadata: TwilioStartMetadata | None,
    status: SessionState,
    outcome_description: str,
    error_kind: str | None = None,
) -> None:
    if metadata is None:
        return
    try:
        await finalize_session_with_retry(
            repository=repository,
            session_id=metadata.session_id,
            status=status,
            settings=settings,
            call_sid=metadata.call_sid,
            outcome_description=outcome_description,
            error_kind=error_kind,
        )
    except SessionPersistenceError as exc:
        log_event(
            logger,
            logging.ERROR,
            "session_finalize_failed",
            error_kind=exc.error_kind,
            **_metadata_fields(metadata),
        )


async def _fail_and_cleanup(
    actor: SessionActor | None,
    bridge: TwilioNovaBridge | None,
    repository: SessionRepository,
    settings: Settings,
    metadata: TwilioStartMetadata | None,
    error_kind: str,
) -> None:
    if bridge is not None:
        await bridge.stop()
    if actor is not None:
        await actor.fail()
    await _finalize_session(
        repository=repository,
        settings=settings,
        metadata=metadata,
        status=SessionState.FAILED,
        outcome_description="media_error",
        error_kind=error_kind,
    )
    if actor is not None:
        await active_sessions.remove(actor.session_id)


async def _abandon_and_cleanup(
    actor: SessionActor | None,
    bridge: TwilioNovaBridge | None,
    repository: SessionRepository,
    settings: Settings,
    metadata: TwilioStartMetadata | None,
) -> None:
    if bridge is not None:
        await bridge.stop()
    if actor is not None:
        await actor.abandon()
        await _finalize_session(
            repository=repository,
            settings=settings,
            metadata=metadata,
            status=SessionState.ABANDONED,
            outcome_description="twilio_disconnect",
        )
        await active_sessions.remove(actor.session_id)
