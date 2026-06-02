from fastapi import Depends

from app.config import Settings, get_settings
from app.personas.repository import DynamoPersonaRepository, PersonaRepository


def get_persona_repository(settings: Settings = Depends(get_settings)) -> PersonaRepository:
    return DynamoPersonaRepository(table_name=settings.personas_table_name)
