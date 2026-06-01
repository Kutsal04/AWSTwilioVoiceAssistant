import logging
from typing import Annotated
from urllib.parse import parse_qsl, urlparse, urlunparse
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from app.config import Settings, get_settings
from app.logging import log_event

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
        log_event(logger, logging.ERROR, "twilio_signature_config_missing", error_kind="missing_twilio_auth_token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Twilio signature verification is enabled but not configured",
        )

    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(settings.twilio_auth_token)
    if validator.validate(public_request_url(request, settings), await form_params(request), signature):
        return

    log_event(logger, logging.WARNING, "twilio_signature_invalid", error_kind="invalid_twilio_signature")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Twilio signature")


@router.post("/voice")
async def voice_webhook(
    request: Request,
    persona_id: Annotated[str | None, Query()] = None,
    settings: Settings = Depends(get_settings),
) -> Response:
    await verify_twilio_signature(request, settings)

    session_id = str(uuid4())
    selected_persona_id = select_persona_id(persona_id, settings)
    twiml = build_voice_twiml(session_id=session_id, persona_id=selected_persona_id, settings=settings)

    log_event(logger, logging.INFO, "twilio_voice_webhook_accepted", session_id=session_id, persona_id=selected_persona_id)
    return Response(content=twiml, media_type="application/xml")
