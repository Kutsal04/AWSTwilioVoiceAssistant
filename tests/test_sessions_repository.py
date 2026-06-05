import asyncio

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.sessions import (
    DynamoSessionRepository,
    SessionAttachRejected,
    SessionPersistenceError,
    SessionRepositoryError,
    SessionState,
    attach_media_stream_with_retry,
    build_attached_session_record,
    create_session_with_retry,
    finalize_session_with_retry,
    new_session_record,
    session_from_item,
    session_to_item,
)


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.scan_pages: list[dict] | None = None
        self.scan_calls = 0

    def put_item(self, *, Item: dict, ConditionExpression: str | None = None) -> None:
        if ConditionExpression == "attribute_not_exists(session_id)" and Item["session_id"] in self.items:
            raise RuntimeError("conditional write failed")
        self.items[Item["session_id"]] = dict(Item)

    def get_item(self, *, Key: dict) -> dict:
        item = self.items.get(Key["session_id"])
        return {"Item": item} if item else {}

    def update_item(
        self,
        *,
        Key: dict,
        UpdateExpression: str,
        ExpressionAttributeNames: dict,
        ExpressionAttributeValues: dict,
        ConditionExpression: str | None = None,
    ) -> None:
        session_id = Key["session_id"]
        if ConditionExpression == "attribute_exists(session_id)" and session_id not in self.items:
            raise RuntimeError("conditional update failed")

        item = self.items[session_id]
        for assignment in UpdateExpression.removeprefix("SET ").split(", "):
            name_token, value_token = assignment.split(" = ")
            item[ExpressionAttributeNames[name_token]] = ExpressionAttributeValues[value_token]

    def scan(self, **scan_kwargs: object) -> dict:
        self.scan_calls += 1
        if self.scan_pages is not None:
            return self.scan_pages.pop(0)
        return {"Items": list(self.items.values())}


class FakeDynamoResource:
    def __init__(self, table: FakeTable) -> None:
        self.table = table

    def Table(self, table_name: str) -> FakeTable:
        return self.table


class FlakySessionRepository:
    def __init__(self, fail_creates: int = 0, fail_updates: int = 0) -> None:
        self.fail_creates = fail_creates
        self.fail_updates = fail_updates
        self.create_attempts = 0
        self.update_attempts = 0

    def create_session(self, record: object) -> None:
        self.create_attempts += 1
        if self.fail_creates:
            self.fail_creates -= 1
            raise RuntimeError("create failed")

    def update_session(self, session_id: str, **updates: object) -> None:
        self.update_attempts += 1
        if self.fail_updates:
            self.fail_updates -= 1
            raise RuntimeError("update failed")

    def attach_media_stream(self, session_id: str, **updates: object) -> object:
        self.update_attempts += 1
        if self.fail_updates:
            self.fail_updates -= 1
            raise RuntimeError("update failed")
        return new_session_record(
            session_id=session_id,
            call_sid=str(updates["call_sid"]),
            persona_id=str(updates["persona_id"]),
        )

    def get_session(self, session_id: str) -> None:
        return None

    def list_sessions(self) -> list:
        return []


class ClientErrorSessionRepository:
    def create_session(self, record: object) -> None:
        client_error = ClientError(
            {
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "table not found",
                }
            },
            "PutItem",
        )
        raise SessionRepositoryError("failed to create session") from client_error

    def update_session(self, session_id: str, **updates: object) -> None:
        return None

    def attach_media_stream(self, session_id: str, **updates: object) -> None:
        return None

    def get_session(self, session_id: str) -> None:
        return None

    def list_sessions(self) -> list:
        return []


def test_session_item_round_trip() -> None:
    record = new_session_record(
        session_id="session-123",
        call_sid="CA123",
        persona_id="appointment_reminder",
    )

    item = session_to_item(record)
    parsed = session_from_item(item)

    assert parsed == record
    assert item["status"] == "starting"
    assert item["schema_version"] == 1
    assert "ended_at" not in item


def test_session_item_rejects_missing_required_fields() -> None:
    with pytest.raises(SessionRepositoryError):
        session_from_item({"session_id": "missing-fields"})


def test_dynamo_session_repository_creates_updates_and_gets_items() -> None:
    table = FakeTable()
    repository = DynamoSessionRepository(table_name="sessions", dynamodb_resource=FakeDynamoResource(table))
    record = new_session_record(session_id="session-123", call_sid="CA123", persona_id="appointment_reminder")

    repository.create_session(record)
    repository.update_session(
        "session-123",
        status=SessionState.COMPLETED,
        ended_at="2026-06-01T00:00:01+00:00",
        last_event_at="2026-06-01T00:00:01+00:00",
        outcome_description="twilio_stop",
    )

    stored = repository.get_session("session-123")
    assert stored is not None
    assert stored.status == SessionState.COMPLETED
    assert stored.ended_at == "2026-06-01T00:00:01+00:00"
    assert stored.outcome_description == "twilio_stop"


def test_dynamo_session_repository_attaches_media_stream() -> None:
    table = FakeTable()
    repository = DynamoSessionRepository(table_name="sessions", dynamodb_resource=FakeDynamoResource(table))
    record = new_session_record(session_id="session-123", call_sid="CA123", persona_id="appointment_reminder")

    repository.create_session(record)
    attached = repository.attach_media_stream(
        "session-123",
        call_sid="CA123",
        persona_id="appointment_reminder",
        stream_sid="MZ123",
    )

    assert attached.status == SessionState.ACTIVE
    assert attached.stream_sid == "MZ123"
    assert attached.media_attach_count == 1
    assert attached.last_attach_at is not None
    assert attached.recovered_at is None
    stored = repository.get_session("session-123")
    assert stored is not None
    assert stored.media_attach_count == 1
    assert stored.stream_sid == "MZ123"


def test_session_attach_marks_existing_active_session_as_recovered() -> None:
    record = new_session_record(session_id="session-123", call_sid="CA123", persona_id="appointment_reminder")
    first_attach = build_attached_session_record(
        record,
        session_id="session-123",
        call_sid="CA123",
        persona_id="appointment_reminder",
        stream_sid="MZ-old",
    )

    reattached = build_attached_session_record(
        first_attach,
        session_id="session-123",
        call_sid="CA123",
        persona_id="appointment_reminder",
        stream_sid="MZ-new",
    )

    assert reattached.media_attach_count == 2
    assert reattached.stream_sid == "MZ-new"
    assert reattached.recovered_at is not None
    assert reattached.recovery_reason == "media_reattach"


@pytest.mark.parametrize(
    ("record", "error_kind"),
    [
        (None, "missing_session"),
        (
            new_session_record(session_id="session-123", call_sid="CA-other", persona_id="appointment_reminder"),
            "session_call_sid_mismatch",
        ),
        (
            new_session_record(session_id="session-123", call_sid="CA123", persona_id="warm_clinical_followup"),
            "session_persona_mismatch",
        ),
    ],
)
def test_session_attach_rejects_invalid_sessions(record: object, error_kind: str) -> None:
    with pytest.raises(SessionAttachRejected) as exc_info:
        build_attached_session_record(
            record,  # type: ignore[arg-type]
            session_id="session-123",
            call_sid="CA123",
            persona_id="appointment_reminder",
            stream_sid="MZ123",
        )

    assert exc_info.value.error_kind == error_kind


def test_session_attach_rejects_terminal_sessions() -> None:
    record = new_session_record(session_id="session-123", call_sid="CA123", persona_id="appointment_reminder")
    terminal_record = record.__class__(
        **{
            **record.__dict__,
            "status": SessionState.COMPLETED,
            "ended_at": "2026-06-01T00:00:01+00:00",
        }
    )

    with pytest.raises(SessionAttachRejected) as exc_info:
        build_attached_session_record(
            terminal_record,
            session_id="session-123",
            call_sid="CA123",
            persona_id="appointment_reminder",
            stream_sid="MZ123",
        )

    assert exc_info.value.error_kind == "terminal_session"


def test_dynamo_session_repository_scans_all_pages() -> None:
    table = FakeTable()
    first = session_to_item(new_session_record(session_id="session-1", call_sid="CA1", persona_id="warm"))
    second = session_to_item(new_session_record(session_id="session-2", call_sid="CA2", persona_id="reminder"))
    table.scan_pages = [
        {"Items": [first], "LastEvaluatedKey": {"session_id": "session-1"}},
        {"Items": [second]},
    ]
    repository = DynamoSessionRepository(table_name="sessions", dynamodb_resource=FakeDynamoResource(table))

    sessions = repository.list_sessions()

    assert [session.session_id for session in sessions] == ["session-1", "session-2"]
    assert table.scan_calls == 2


def test_create_session_retry_policy_retries_then_succeeds() -> None:
    async def run() -> None:
        settings = Settings(
            _env_file=None,
            session_create_max_attempts=3,
            session_write_retry_delay_seconds=0,
        )
        repository = FlakySessionRepository(fail_creates=2)
        record = new_session_record(session_id="session-123", call_sid="CA123", persona_id="appointment_reminder")

        await create_session_with_retry(repository=repository, record=record, settings=settings)

        assert repository.create_attempts == 3

    asyncio.run(run())


def test_finalize_session_retry_policy_raises_after_configured_attempts() -> None:
    async def run() -> None:
        settings = Settings(
            _env_file=None,
            session_finalize_max_attempts=4,
            session_write_retry_delay_seconds=0,
        )
        repository = FlakySessionRepository(fail_updates=4)

        with pytest.raises(SessionPersistenceError) as exc_info:
            await finalize_session_with_retry(
                repository=repository,
                session_id="session-123",
                status=SessionState.FAILED,
                settings=settings,
                error_kind="NovaClientError",
            )

        assert exc_info.value.operation == "finalize"
        assert repository.update_attempts == 4

    asyncio.run(run())


def test_attach_media_stream_retry_policy_retries_then_succeeds() -> None:
    async def run() -> None:
        settings = Settings(
            _env_file=None,
            session_update_max_attempts=3,
            session_write_retry_delay_seconds=0,
        )
        repository = FlakySessionRepository(fail_updates=2)

        await attach_media_stream_with_retry(
            repository=repository,
            session_id="session-123",
            call_sid="CA123",
            persona_id="appointment_reminder",
            stream_sid="MZ123",
            settings=settings,
        )

        assert repository.update_attempts == 3

    asyncio.run(run())


def test_retry_policy_reports_wrapped_dynamodb_error_code() -> None:
    async def run() -> None:
        settings = Settings(
            _env_file=None,
            session_create_max_attempts=1,
            session_write_retry_delay_seconds=0,
        )
        record = new_session_record(session_id="session-123", call_sid="CA123", persona_id="appointment_reminder")

        with pytest.raises(SessionPersistenceError) as exc_info:
            await create_session_with_retry(
                repository=ClientErrorSessionRepository(),
                record=record,
                settings=settings,
            )

        assert exc_info.value.error_kind == "ResourceNotFoundException"

    asyncio.run(run())
