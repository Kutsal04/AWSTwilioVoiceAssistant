from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    env_name: str = "local"
    public_base_url: str = "http://localhost:8080"
    default_persona_id: str = "warm_clinical_followup"
    verify_twilio_signature: bool = False
    sessions_table_name: str = "sessions"
    personas_table_name: str = "personas"
    transcript_turns_table_name: str = "transcript_turns"
    bedrock_region: str = "us-east-1"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()

