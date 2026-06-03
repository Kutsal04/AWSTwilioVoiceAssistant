from fastapi import Depends

from app.config import Settings, get_settings
from app.transcripts.repository import DynamoTranscriptRepository, TranscriptRepository


def get_transcript_repository(settings: Settings = Depends(get_settings)) -> TranscriptRepository:
    return DynamoTranscriptRepository(table_name=settings.transcript_turns_table_name)
