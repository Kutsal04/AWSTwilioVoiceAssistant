from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    env_name: str = Field(default="local", min_length=1)
    public_base_url: str = Field(default="http://localhost:8080", min_length=1)
    default_persona_id: str = Field(default="warm_clinical_followup", min_length=1)
    persona_lookup_timeout_seconds: float = Field(default=2.0, gt=0)
    persona_lookup_fallback_enabled: bool = True
    verify_twilio_signature: bool = False
    twilio_auth_token: str | None = None
    media_idle_timeout_seconds: float = Field(default=30.0, gt=0)
    audio_queue_maxsize: int = Field(default=50, gt=0)
    nova_stream_open_timeout_seconds: float = Field(default=20.0, gt=0)
    nova_response_timeout_seconds: float = Field(default=30.0, gt=0)
    barge_in_enabled: bool = True
    barge_in_rms_threshold: float = Field(default=500.0, ge=0)
    barge_in_playback_grace_seconds: float = Field(default=0.75, gt=0)
    graceful_shutdown_drain_seconds: float = Field(default=5.0, gt=0)
    session_write_timeout_seconds: float = Field(default=2.0, gt=0)
    session_write_retry_delay_seconds: float = Field(default=0.1, ge=0)
    session_create_max_attempts: int = Field(default=3, gt=0)
    session_update_max_attempts: int = Field(default=2, gt=0)
    session_finalize_max_attempts: int = Field(default=5, gt=0)
    transcript_write_timeout_seconds: float = Field(default=1.0, gt=0)
    transcript_write_retry_delay_seconds: float = Field(default=0.1, ge=0)
    transcript_write_max_attempts: int = Field(default=2, gt=0)
    sessions_table_name: str = Field(default="sessions", min_length=1)
    personas_table_name: str = Field(default="personas", min_length=1)
    transcript_turns_table_name: str = Field(default="transcript_turns", min_length=1)
    bedrock_region: str = Field(default="us-east-1", min_length=1)
    nova_model_id: str = Field(default="amazon.nova-2-sonic-v1:0", min_length=1)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator(
        "env_name",
        "public_base_url",
        "default_persona_id",
        "sessions_table_name",
        "personas_table_name",
        "transcript_turns_table_name",
        "bedrock_region",
        "nova_model_id",
    )
    @classmethod
    def strip_and_validate_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be empty")
        return stripped

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("public_base_url must be an absolute http or https URL")
        return value

    @field_validator("twilio_auth_token")
    @classmethod
    def strip_optional_secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def default_signature_verification_for_deployed_envs(self) -> "Settings":
        if self.env_name != "local" and "verify_twilio_signature" not in self.model_fields_set:
            self.verify_twilio_signature = True
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
