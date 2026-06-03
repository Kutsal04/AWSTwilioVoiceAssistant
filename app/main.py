import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.logging import configure_logging
from app.logging import log_event
from app.metrics import emit_error_count
from app.sessions import (
    SessionState,
    active_sessions,
    finalize_session_with_retry,
    get_session_repository,
)
from app.twilio.media import router as media_router
from app.twilio.webhook import router as twilio_router

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await shutdown_active_sessions()


app = FastAPI(title="AWS Twilio Voice Assistant", lifespan=lifespan)
app.include_router(media_router)
app.include_router(twilio_router)


@app.get("/health")
async def health() -> dict[str, str]:
    get_settings()
    return {"status": "healthy"}


async def shutdown_active_sessions() -> None:
    settings = get_settings()
    sessions = await active_sessions.list()
    if not sessions:
        return

    repository = get_session_repository(settings)
    try:
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    _abandon_active_session_on_shutdown(actor, repository, settings)
                    for actor in sessions
                ),
                return_exceptions=True,
            ),
            timeout=settings.graceful_shutdown_drain_seconds,
        )
    except TimeoutError:
        emit_error_count("shutdown_drain_timeout")
        log_event(logger, logging.ERROR, "shutdown_drain_timeout", error_kind="shutdown_drain_timeout")


async def _abandon_active_session_on_shutdown(actor, repository, settings) -> None:
    try:
        await actor.abandon()
        await finalize_session_with_retry(
            repository=repository,
            session_id=actor.session_id,
            status=SessionState.ABANDONED,
            settings=settings,
            call_sid=actor.call_sid,
            outcome_description="service_shutdown",
            error_kind="service_shutdown",
        )
    finally:
        await active_sessions.remove(actor.session_id)
        log_event(
            logger,
            logging.INFO,
            "session_abandoned_on_shutdown",
            session_id=actor.session_id,
            call_sid=actor.call_sid,
            persona_id=actor.persona_id,
            error_kind="service_shutdown",
        )
