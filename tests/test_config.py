import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def test_settings_defaults_without_env_file() -> None:
    settings = Settings(_env_file=None)

    assert settings.env_name == "local"
    assert settings.public_base_url == "http://localhost:8080"
    assert settings.default_persona_id == "warm_clinical_followup"
    assert settings.verify_twilio_signature is False
    assert settings.twilio_auth_token is None
    assert settings.media_idle_timeout_seconds == 30.0
    assert settings.audio_queue_maxsize == 50
    assert settings.nova_stream_open_timeout_seconds == 20.0
    assert settings.sessions_table_name == "sessions"
    assert settings.personas_table_name == "personas"
    assert settings.transcript_turns_table_name == "transcript_turns"
    assert settings.bedrock_region == "us-east-1"
    assert settings.nova_model_id == "amazon.nova-2-sonic-v1:0"


def test_signature_verification_defaults_on_for_deployed_envs() -> None:
    settings = Settings(_env_file=None, env_name="dev")

    assert settings.verify_twilio_signature is True


def test_signature_verification_can_be_disabled_explicitly_for_deployed_envs() -> None:
    settings = Settings(_env_file=None, env_name="dev", verify_twilio_signature=False)

    assert settings.verify_twilio_signature is False


def test_settings_validate_public_base_url() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, public_base_url="not-a-url")


def test_get_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_PERSONA_ID", "appointment_reminder")
    monkeypatch.setenv("VERIFY_TWILIO_SIGNATURE", "true")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "  test-token  ")

    settings = get_settings()

    assert settings.default_persona_id == "appointment_reminder"
    assert settings.verify_twilio_signature is True
    assert settings.twilio_auth_token == "test-token"


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("field_name", ["env_name", "bedrock_region"])
def test_required_string_fields_reject_empty_values(field_name: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: value})


def test_settings_strip_whitespace_before_validation() -> None:
    settings = Settings(_env_file=None, public_base_url="  http://localhost:8080  ")

    assert settings.public_base_url == "http://localhost:8080"
