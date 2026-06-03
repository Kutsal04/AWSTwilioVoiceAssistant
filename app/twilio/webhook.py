import logging
from typing import Annotated
from urllib.parse import parse_qsl, urlparse, urlunparse
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from app.config import Settings, get_settings
from app.logging import log_event
from app.metrics import emit_error_count
from app.personas import PersonaRepository, PersonaSelectionError, get_persona_repository, resolve_persona
from app.sessions import (
    SessionPersistenceError,
    SessionRepository,
    create_session_with_retry,
    get_session_repository,
    new_session_record,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/twilio", tags=["twilio"])


def select_persona_id(requested_persona_id: str | None, settings: Settings) -> str:
    if requested_persona_id and requested_persona_id.strip():
        return requested_persona_id.strip()
    return settings.default_persona_id


def media_stream_url(public_base_url: str) -> str:
    parsed = urlparse(public_base_url)
    websocket_scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    return urlunparse((websocket_scheme, parsed.netloc, f"{base_path}/media", "", "", ""))


def public_request_url(request: Request, settings: Settings) -> str:
    base = urlparse(settings.public_base_url)
    return urlunparse((base.scheme, base.netloc, request.url.path, "", request.url.query, ""))


def build_voice_twiml(session_id: str, persona_id: str, settings: Settings) -> str:
    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=media_stream_url(settings.public_base_url))
    stream.parameter(name="session_id", value=session_id)
    stream.parameter(name="persona_id", value=persona_id)
    connect.append(stream)
    response.append(connect)
    return str(response)


async def form_params(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    return dict(parse_qsl(body, keep_blank_values=True))


async def verify_twilio_signature(request: Request, settings: Settings) -> None:
    if not settings.verify_twilio_signature:
        return

    if not settings.twilio_auth_token:
        emit_error_count("missing_twilio_auth_token")
        log_event(logger, logging.ERROR, "twilio_signature_config_missing", error_kind="missing_twilio_auth_token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Twilio signature verification is enabled but not configured",
        )

    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(settings.twilio_auth_token)
    if validator.validate(public_request_url(request, settings), await form_params(request), signature):
        return

    emit_error_count("invalid_twilio_signature")
    log_event(logger, logging.WARNING, "twilio_signature_invalid", error_kind="invalid_twilio_signature")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Twilio signature")


@router.post("/voice")
async def voice_webhook(
    request: Request,
    persona_id: Annotated[str | None, Query()] = None,
    settings: Settings = Depends(get_settings),
    persona_repository: PersonaRepository = Depends(get_persona_repository),
    session_repository: SessionRepository = Depends(get_session_repository),
) -> Response:
    await verify_twilio_signature(request, settings)

    session_id = str(uuid4())
    params = await form_params(request)
    call_sid = params.get("CallSid", "").strip() or "unknown"
    requested_persona_id = select_persona_id(persona_id, settings)
    try:
        selected_persona = await resolve_persona(
            requested_persona_id=requested_persona_id,
            settings=settings,
            repository=persona_repository,
        )
    except PersonaSelectionError as exc:
        emit_error_count(exc.error_kind)
        log_event(
            logger,
            logging.WARNING,
            "twilio_voice_persona_rejected",
            session_id=session_id,
            persona_id=requested_persona_id,
            error_kind=exc.error_kind,
        )
        raise HTTPException(status_code=persona_error_status(exc), detail="Persona is not available") from exc

    try:
        await create_session_with_retry(
            repository=session_repository,
            record=new_session_record(
                session_id=session_id,
                call_sid=call_sid,
                persona_id=selected_persona.persona_id,
            ),
            settings=settings,
        )
    except SessionPersistenceError as exc:
        emit_error_count(exc.error_kind)
        log_event(
            logger,
            logging.ERROR,
            "twilio_voice_session_create_failed",
            session_id=session_id,
            call_sid=call_sid,
            persona_id=selected_persona.persona_id,
            error_kind=exc.error_kind,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Session could not be created") from exc

    twiml = build_voice_twiml(session_id=session_id, persona_id=selected_persona.persona_id, settings=settings)

    log_event(
        logger,
        logging.INFO,
        "twilio_voice_webhook_accepted",
        session_id=session_id,
        call_sid=call_sid,
        persona_id=selected_persona.persona_id,
    )
    return Response(content=twiml, media_type="application/xml")


def persona_error_status(exc: PersonaSelectionError) -> int:
    if exc.error_kind in {"missing_persona", "inactive_persona"}:
        return status.HTTP_404_NOT_FOUND
    return status.HTTP_503_SERVICE_UNAVAILABLE
