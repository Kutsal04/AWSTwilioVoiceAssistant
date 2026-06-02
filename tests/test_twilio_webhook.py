from collections.abc import Iterator
from contextlib import contextmanager
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.config import Settings, get_settings
from app.main import app
from app.personas import Persona, get_persona_repository
from app.twilio.webhook import media_stream_url, select_persona_id


@contextmanager
def override_settings(settings: Settings) -> Iterator[None]:
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_settings, None)


class FakePersonaRepository:
    def __init__(self, personas: list[Persona]) -> None:
        self.personas = {persona.persona_id: persona for persona in personas}

    def get_persona(self, persona_id: str) -> Persona | None:
        return self.personas.get(persona_id)

    def put_persona(self, persona: Persona) -> None:
        self.personas[persona.persona_id] = persona


def make_persona_repository(*, appointment_active: bool = True) -> FakePersonaRepository:
    return FakePersonaRepository(
        [
            Persona("warm_clinical_followup", "Warm Clinical Follow-up", "warm prompt"),
            Persona("appointment_reminder", "Appointment Reminder", "appointment prompt", active=appointment_active),
        ]
    )


@contextmanager
def override_persona_repository(repository: FakePersonaRepository | None = None) -> Iterator[FakePersonaRepository]:
    fake_repository = repository or make_persona_repository()
    app.dependency_overrides[get_persona_repository] = lambda: fake_repository
    try:
        yield fake_repository
    finally:
        app.dependency_overrides.pop(get_persona_repository, None)


def parse_twiml(xml: str) -> ElementTree.Element:
    return ElementTree.fromstring(xml)


def stream_parameters(root: ElementTree.Element) -> dict[str, str]:
    stream = root.find("./Connect/Stream")
    assert stream is not None
    return {parameter.attrib["name"]: parameter.attrib["value"] for parameter in stream.findall("./Parameter")}


def test_media_stream_url_uses_websocket_scheme() -> None:
    assert media_stream_url("https://voice.example.com") == "wss://voice.example.com/media"
    assert media_stream_url("http://localhost:8080") == "ws://localhost:8080/media"


def test_persona_selection_falls_back_to_default() -> None:
    settings = Settings(_env_file=None, default_persona_id="warm_clinical_followup")

    assert select_persona_id(None, settings) == "warm_clinical_followup"
    assert select_persona_id("   ", settings) == "warm_clinical_followup"
    assert select_persona_id(" appointment_reminder ", settings) == "appointment_reminder"


def test_voice_webhook_returns_connect_stream_twiml_with_parameters() -> None:
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        default_persona_id="warm_clinical_followup",
        verify_twilio_signature=False,
    )
    client = TestClient(app)

    with override_settings(settings), override_persona_repository():
        response = client.post("/twilio/voice?persona_id=appointment_reminder", data={"CallSid": "CA123"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")

    root = parse_twiml(response.text)
    stream = root.find("./Connect/Stream")
    assert stream is not None
    assert stream.attrib["url"] == "wss://voice.example.com/media"

    parameters = stream_parameters(root)
    assert parameters["persona_id"] == "appointment_reminder"
    assert parameters["session_id"]


def test_voice_webhook_uses_default_persona_when_query_parameter_is_missing() -> None:
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        default_persona_id="warm_clinical_followup",
        verify_twilio_signature=False,
    )
    client = TestClient(app)

    with override_settings(settings), override_persona_repository():
        response = client.post("/twilio/voice", data={"CallSid": "CA123"})

    assert response.status_code == 200
    assert stream_parameters(parse_twiml(response.text))["persona_id"] == "warm_clinical_followup"


def test_voice_webhook_skips_signature_check_when_disabled() -> None:
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        verify_twilio_signature=False,
    )
    client = TestClient(app)

    with override_settings(settings), override_persona_repository():
        response = client.post("/twilio/voice", data={"CallSid": "CA123"}, headers={"X-Twilio-Signature": "bad"})

    assert response.status_code == 200


def test_voice_webhook_accepts_valid_twilio_signature_when_enabled() -> None:
    token = "test-token"
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        verify_twilio_signature=True,
        twilio_auth_token=token,
    )
    form_data = {"CallSid": "CA123"}
    signature = RequestValidator(token).compute_signature("https://voice.example.com/twilio/voice", form_data)
    client = TestClient(app)

    with override_settings(settings), override_persona_repository():
        response = client.post("/twilio/voice", data=form_data, headers={"X-Twilio-Signature": signature})

    assert response.status_code == 200


def test_voice_webhook_accepts_valid_twilio_signature_with_query_parameter() -> None:
    token = "test-token"
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        verify_twilio_signature=True,
        twilio_auth_token=token,
    )
    form_data = {"CallSid": "CA123"}
    signature = RequestValidator(token).compute_signature(
        "https://voice.example.com/twilio/voice?persona_id=appointment_reminder",
        form_data,
    )
    client = TestClient(app)

    with override_settings(settings), override_persona_repository():
        response = client.post(
            "/twilio/voice?persona_id=appointment_reminder",
            data=form_data,
            headers={"X-Twilio-Signature": signature},
        )

    assert response.status_code == 200
    assert stream_parameters(parse_twiml(response.text))["persona_id"] == "appointment_reminder"


def test_voice_webhook_rejects_invalid_twilio_signature_when_enabled() -> None:
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        verify_twilio_signature=True,
        twilio_auth_token="test-token",
    )
    client = TestClient(app)

    with override_settings(settings), override_persona_repository():
        response = client.post("/twilio/voice", data={"CallSid": "CA123"}, headers={"X-Twilio-Signature": "bad"})

    assert response.status_code == 403


def test_voice_webhook_falls_back_when_requested_persona_is_missing() -> None:
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        default_persona_id="warm_clinical_followup",
        verify_twilio_signature=False,
    )
    client = TestClient(app)

    with override_settings(settings), override_persona_repository():
        response = client.post("/twilio/voice?persona_id=missing", data={"CallSid": "CA123"})

    assert response.status_code == 200
    assert stream_parameters(parse_twiml(response.text))["persona_id"] == "warm_clinical_followup"


def test_voice_webhook_rejects_inactive_default_persona() -> None:
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        default_persona_id="appointment_reminder",
        verify_twilio_signature=False,
    )
    repository = make_persona_repository(appointment_active=False)
    client = TestClient(app)

    with override_settings(settings), override_persona_repository(repository):
        response = client.post("/twilio/voice", data={"CallSid": "CA123"})

    assert response.status_code == 404
