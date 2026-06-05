"""Session lifecycle and per-call actor modules."""

from app.sessions.actor import QueueWriteResult, SessionActor, TranscriptBuffer
from app.sessions.dependencies import get_session_repository
from app.sessions.lifecycle import InvalidSessionTransition, SessionState
from app.sessions.repository import (
    DynamoSessionRepository,
    SessionAttachRejected,
    SessionPersistenceError,
    SessionRecord,
    SessionRepository,
    SessionRepositoryError,
    attach_media_stream_with_retry,
    build_attached_session_record,
    create_session_with_retry,
    finalize_session_with_retry,
    new_session_record,
    session_from_item,
    session_to_item,
    update_session_with_retry,
)
from app.sessions.registry import SessionRegistry, active_sessions

__all__ = [
    "DynamoSessionRepository",
    "InvalidSessionTransition",
    "QueueWriteResult",
    "SessionAttachRejected",
    "SessionActor",
    "SessionPersistenceError",
    "SessionRecord",
    "SessionRepository",
    "SessionRepositoryError",
    "SessionRegistry",
    "SessionState",
    "TranscriptBuffer",
    "active_sessions",
    "attach_media_stream_with_retry",
    "build_attached_session_record",
    "create_session_with_retry",
    "finalize_session_with_retry",
    "get_session_repository",
    "new_session_record",
    "session_from_item",
    "session_to_item",
    "update_session_with_retry",
]
