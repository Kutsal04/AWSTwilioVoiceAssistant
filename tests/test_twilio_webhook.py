from collections.abc import Iterator
from contextlib import contextmanager
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.config import Settings, get_settings
from app.main import app
from app.personas import Persona, get_persona_repository
from app.sessions import (
    SessionRecord,
    SessionState,
    build_attached_session_record,
    get_session_repository,
)
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


class FakeSessionRepository:
    def __init__(self, fail_creates: int = 0) -> None:
        self.sessions: dict[str, SessionRecord] = {}
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.fail_creates = fail_creates
        self.create_attempts = 0

    def create_session(self, record: SessionRecord) -> None:
        self.create_attempts += 1
        if self.fail_creates:
            self.fail_creates -= 1
            raise RuntimeError("create failed")
        self.sessions[record.session_id] = record

    def update_session(self, session_id: str, **updates: object) -> None:
        self.updates.append((session_id, updates))
        record = self.sessions.get(session_id)
        if record is None:
            return
        changed = {
            field: value.value if isinstance(value, SessionState) else value
            for field, value in updates.items()
            if value is not None
        }
        item = record.__dict__ | changed
        item["status"] = SessionState(item["status"])
        self.sessions[session_id] = SessionRecord(**item)

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    def attach_media_stream(
        self,
        session_id: str,
        *,
        call_sid: str,
        persona_id: str,
        stream_sid: str,
    ) -> SessionRecord:
        record = build_attached_session_record(
            self.get_session(session_id),
            session_id=session_id,
            call_sid=call_sid,
            persona_id=persona_id,
            stream_sid=stream_sid,
        )
        self.sessions[session_id] = record
        self.updates.append(
            (
                session_id,
                {
                    "status": record.status,
                    "call_sid": record.call_sid,
                    "last_event_at": record.last_event_at,
                    "stream_sid": record.stream_sid,
                    "media_attach_count": record.media_attach_count,
                    "last_attach_at": record.last_attach_at,
                    "recovered_at": record.recovered_at,
                    "recovery_reason": record.recovery_reason,
                },
            )
        )
        return record


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


@contextmanager
def override_session_repository(repository: FakeSessionRepository | None = None) -> Iterator[FakeSessionRepository]:
    fake_repository = repository or FakeSessionRepository()
    app.dependency_overrides[get_session_repository] = lambda: fake_repository
    try:
        yield fake_repository
    finally:
        app.dependency_overrides.pop(get_session_repository, None)


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

    with override_settings(settings), override_persona_repository(), override_session_repository():
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

    with override_settings(settings), override_persona_repository(), override_session_repository() as sessions:
        response = client.post("/twilio/voice", data={"CallSid": "CA123"})

    assert response.status_code == 200
    parameters = stream_parameters(parse_twiml(response.text))
    assert parameters["persona_id"] == "warm_clinical_followup"
    assert sessions.sessions[parameters["session_id"]].call_sid == "CA123"
    assert sessions.sessions[parameters["session_id"]].persona_id == "warm_clinical_followup"
    assert sessions.sessions[parameters["session_id"]].status == "starting"


def test_voice_webhook_skips_signature_check_when_disabled() -> None:
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        verify_twilio_signature=False,
    )
    client = TestClient(app)

    with override_settings(settings), override_persona_repository(), override_session_repository():
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

    with override_settings(settings), override_persona_repository(), override_session_repository():
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

    with override_settings(settings), override_persona_repository(), override_session_repository():
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

    with override_settings(settings), override_persona_repository(), override_session_repository():
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

    with override_settings(settings), override_persona_repository(), override_session_repository():
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

    with override_settings(settings), override_persona_repository(repository), override_session_repository():
        response = client.post("/twilio/voice", data={"CallSid": "CA123"})

    assert response.status_code == 404


def test_voice_webhook_fails_when_session_create_fails_after_retries() -> None:
    settings = Settings(
        _env_file=None,
        public_base_url="https://voice.example.com",
        verify_twilio_signature=False,
        session_create_max_attempts=2,
        session_write_retry_delay_seconds=0,
    )
    client = TestClient(app)
    sessions = FakeSessionRepository(fail_creates=2)

    with override_settings(settings), override_persona_repository(), override_session_repository(sessions):
        response = client.post("/twilio/voice", data={"CallSid": "CA123"})

    assert response.status_code == 503
    assert sessions.create_attempts == 2
