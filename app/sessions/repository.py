import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import boto3

from app.config import Settings
from app.sessions.lifecycle import SessionState


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    call_sid: str
    persona_id: str
    status: SessionState
    started_at: str
    ended_at: str | None
    last_event_at: str
    outcome_description: str | None
    error_kind: str | None
    schema_version: int = SCHEMA_VERSION


class SessionRepositoryError(RuntimeError):
    pass


class SessionPersistenceError(RuntimeError):
    def __init__(self, *, operation: str, error_kind: str) -> None:
        super().__init__(f"session persistence failed during {operation}: {error_kind}")
        self.operation = operation
        self.error_kind = error_kind


class SessionRepository(Protocol):
    def create_session(self, record: SessionRecord) -> None:
        ...

    def update_session(
        self,
        session_id: str,
        *,
        status: SessionState | None = None,
        call_sid: str | None = None,
        ended_at: str | None = None,
        last_event_at: str | None = None,
        outcome_description: str | None = None,
        error_kind: str | None = None,
    ) -> None:
        ...

    def get_session(self, session_id: str) -> SessionRecord | None:
        ...

    def list_sessions(self) -> list[SessionRecord]:
        ...


class DynamoSessionRepository:
    def __init__(self, *, table_name: str, dynamodb_resource: Any | None = None) -> None:
        self.table_name = table_name
        self._dynamodb_resource = dynamodb_resource
        self._table: Any | None = None

    @property
    def table(self) -> Any:
        if self._table is None:
            resource = self._dynamodb_resource or boto3.resource("dynamodb")
            self._table = resource.Table(self.table_name)
        return self._table

    def create_session(self, record: SessionRecord) -> None:
        try:
            self.table.put_item(
                Item=session_to_item(record),
                ConditionExpression="attribute_not_exists(session_id)",
            )
        except Exception as exc:
            raise SessionRepositoryError("failed to create session") from exc

    def update_session(
        self,
        session_id: str,
        *,
        status: SessionState | None = None,
        call_sid: str | None = None,
        ended_at: str | None = None,
        last_event_at: str | None = None,
        outcome_description: str | None = None,
        error_kind: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status.value
        if call_sid is not None:
            updates["call_sid"] = call_sid
        if ended_at is not None:
            updates["ended_at"] = ended_at
        if last_event_at is not None:
            updates["last_event_at"] = last_event_at
        if outcome_description is not None:
            updates["outcome_description"] = outcome_description
        if error_kind is not None:
            updates["error_kind"] = error_kind

        if not updates:
            return

        expression_names = {f"#{key}": key for key in updates}
        expression_values = {f":{key}": value for key, value in updates.items()}
        update_expression = "SET " + ", ".join(f"#{key} = :{key}" for key in updates)

        try:
            self.table.update_item(
                Key={"session_id": session_id},
                UpdateExpression=update_expression,
                ExpressionAttributeNames=expression_names,
                ExpressionAttributeValues=expression_values,
                ConditionExpression="attribute_exists(session_id)",
            )
        except Exception as exc:
            raise SessionRepositoryError("failed to update session") from exc

    def get_session(self, session_id: str) -> SessionRecord | None:
        try:
            response = self.table.get_item(Key={"session_id": session_id})
        except Exception as exc:
            raise SessionRepositoryError("failed to get session") from exc

        item = response.get("Item")
        if item is None:
            return None
        return session_from_item(item)

    def list_sessions(self) -> list[SessionRecord]:
        items: list[dict[str, Any]] = []
        scan_kwargs: dict[str, Any] = {}
        try:
            while True:
                response = self.table.scan(**scan_kwargs)
                items.extend(response.get("Items", []))
                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_evaluated_key
        except Exception as exc:
            raise SessionRepositoryError("failed to list sessions") from exc

        return [session_from_item(item) for item in items]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_record(*, session_id: str, call_sid: str, persona_id: str) -> SessionRecord:
    now = utc_now_iso()
    return SessionRecord(
        session_id=session_id,
        call_sid=call_sid,
        persona_id=persona_id,
        status=SessionState.STARTING,
        started_at=now,
        ended_at=None,
        last_event_at=now,
        outcome_description=None,
        error_kind=None,
    )


def session_to_item(record: SessionRecord) -> dict[str, Any]:
    item: dict[str, Any] = {
        "session_id": record.session_id,
        "call_sid": record.call_sid,
        "persona_id": record.persona_id,
        "status": record.status.value,
        "started_at": record.started_at,
        "last_event_at": record.last_event_at,
        "schema_version": record.schema_version,
    }
    if record.ended_at is not None:
        item["ended_at"] = record.ended_at
    if record.outcome_description is not None:
        item["outcome_description"] = record.outcome_description
    if record.error_kind is not None:
        item["error_kind"] = record.error_kind
    return item


def session_from_item(item: dict[str, Any]) -> SessionRecord:
    return SessionRecord(
        session_id=_required_string(item, "session_id"),
        call_sid=_required_string(item, "call_sid"),
        persona_id=_required_string(item, "persona_id"),
        status=SessionState(_required_string(item, "status")),
        started_at=_required_string(item, "started_at"),
        ended_at=_optional_string(item, "ended_at"),
        last_event_at=_required_string(item, "last_event_at"),
        outcome_description=_optional_string(item, "outcome_description"),
        error_kind=_optional_string(item, "error_kind"),
        schema_version=int(item.get("schema_version", SCHEMA_VERSION)),
    )


async def create_session_with_retry(
    *,
    repository: SessionRepository,
    record: SessionRecord,
    settings: Settings,
) -> None:
    await _run_with_retry(
        operation="create",
        attempts=settings.session_create_max_attempts,
        delay_seconds=settings.session_write_retry_delay_seconds,
        timeout_seconds=settings.session_write_timeout_seconds,
        call=lambda: repository.create_session(record),
    )


async def update_session_with_retry(
    *,
    repository: SessionRepository,
    session_id: str,
    settings: Settings,
    status: SessionState | None = None,
    call_sid: str | None = None,
    outcome_description: str | None = None,
    error_kind: str | None = None,
) -> None:
    await _run_with_retry(
        operation="update",
        attempts=settings.session_update_max_attempts,
        delay_seconds=settings.session_write_retry_delay_seconds,
        timeout_seconds=settings.session_write_timeout_seconds,
        call=lambda: repository.update_session(
            session_id,
            status=status,
            call_sid=call_sid,
            last_event_at=utc_now_iso(),
            outcome_description=outcome_description,
            error_kind=error_kind,
        ),
    )


async def finalize_session_with_retry(
    *,
    repository: SessionRepository,
    session_id: str,
    status: SessionState,
    settings: Settings,
    call_sid: str | None = None,
    outcome_description: str | None = None,
    error_kind: str | None = None,
) -> None:
    now = utc_now_iso()
    await _run_with_retry(
        operation="finalize",
        attempts=settings.session_finalize_max_attempts,
        delay_seconds=settings.session_write_retry_delay_seconds,
        timeout_seconds=settings.session_write_timeout_seconds,
        call=lambda: repository.update_session(
            session_id,
            status=status,
            call_sid=call_sid,
            ended_at=now,
            last_event_at=now,
            outcome_description=outcome_description,
            error_kind=error_kind,
        ),
    )


async def _run_with_retry(
    *,
    operation: str,
    attempts: int,
    delay_seconds: float,
    timeout_seconds: float,
    call: Any,
) -> None:
    last_error_kind = "unknown"
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout_seconds)
            return
        except TimeoutError:
            last_error_kind = "session_write_timeout"
        except Exception as exc:
            last_error_kind = _error_kind(exc)

        if attempt < attempts and delay_seconds:
            await asyncio.sleep(delay_seconds)

    raise SessionPersistenceError(operation=operation, error_kind=last_error_kind)


def _required_string(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SessionRepositoryError(f"session item is missing {key}")
    return value.strip()


def _optional_string(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _error_kind(exc: BaseException) -> str:
    current: BaseException | None = exc
    while current is not None:
        response = getattr(current, "response", None)
        if isinstance(response, dict):
            error = response.get("Error")
            if isinstance(error, dict):
                code = error.get("Code")
                if isinstance(code, str) and code.strip():
                    return code.strip()
        current = current.__cause__
    return type(exc).__name__
