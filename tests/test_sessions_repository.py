import asyncio

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.sessions import (
    DynamoSessionRepository,
    SessionPersistenceError,
    SessionRepositoryError,
    SessionState,
    create_session_with_retry,
    finalize_session_with_retry,
    new_session_record,
    session_from_item,
    session_to_item,
)


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

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

    def get_session(self, session_id: str) -> None:
        return None


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

    def get_session(self, session_id: str) -> None:
        return None


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
