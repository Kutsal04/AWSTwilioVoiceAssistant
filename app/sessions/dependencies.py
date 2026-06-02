from fastapi import Depends

from app.config import Settings, get_settings
from app.sessions.repository import DynamoSessionRepository, SessionRepository


def get_session_repository(settings: Settings = Depends(get_settings)) -> SessionRepository:
    return DynamoSessionRepository(table_name=settings.sessions_table_name)
