import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol

import boto3

from app.config import Settings


Speaker = Literal["caller", "assistant"]
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TranscriptTurn:
    session_id: str
    turn_index: int
    speaker: Speaker
    text: str
    transcript_item_id: str
    confidence: float | None
    created_at: str
    schema_version: int = SCHEMA_VERSION


class TranscriptRepositoryError(RuntimeError):
    pass


class TranscriptPersistenceError(RuntimeError):
    def __init__(self, *, operation: str, error_kind: str) -> None:
        super().__init__(f"transcript persistence failed during {operation}: {error_kind}")
        self.operation = operation
        self.error_kind = error_kind


class TranscriptRepository(Protocol):
    def put_turn(self, turn: TranscriptTurn) -> None:
        ...

    def list_turns(self, session_id: str) -> list[TranscriptTurn]:
        ...


class DynamoTranscriptRepository:
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

    def put_turn(self, turn: TranscriptTurn) -> None:
        try:
            self.table.put_item(
                Item=transcript_turn_to_item(turn),
                ConditionExpression="attribute_not_exists(session_id) AND attribute_not_exists(turn_index)",
            )
        except Exception as exc:
            raise TranscriptRepositoryError("failed to put transcript turn") from exc

    def list_turns(self, session_id: str) -> list[TranscriptTurn]:
        try:
            response = self.table.query(
                KeyConditionExpression="session_id = :session_id",
                ExpressionAttributeValues={":session_id": session_id},
                ScanIndexForward=True,
            )
        except Exception as exc:
            raise TranscriptRepositoryError("failed to list transcript turns") from exc

        return [transcript_turn_from_item(item) for item in response.get("Items", [])]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def transcript_turn_to_item(turn: TranscriptTurn) -> dict[str, Any]:
    item: dict[str, Any] = {
        "session_id": turn.session_id,
        "turn_index": turn.turn_index,
        "speaker": turn.speaker,
        "text": turn.text,
        "transcript_item_id": turn.transcript_item_id,
        "created_at": turn.created_at,
        "schema_version": turn.schema_version,
    }
    if turn.confidence is not None:
        item["confidence"] = Decimal(str(turn.confidence))
    return item


def transcript_turn_from_item(item: dict[str, Any]) -> TranscriptTurn:
    return TranscriptTurn(
        session_id=_required_string(item, "session_id"),
        turn_index=int(item.get("turn_index")),
        speaker=_speaker(item.get("speaker")),
        text=_required_string(item, "text"),
        transcript_item_id=_required_string(item, "transcript_item_id"),
        confidence=_optional_float(item.get("confidence")),
        created_at=_required_string(item, "created_at"),
        schema_version=int(item.get("schema_version", SCHEMA_VERSION)),
    )


async def put_transcript_turn_with_retry(
    *,
    repository: TranscriptRepository,
    turn: TranscriptTurn,
    settings: Settings,
) -> None:
    await _run_with_retry(
        operation="put_turn",
        attempts=settings.transcript_write_max_attempts,
        delay_seconds=settings.transcript_write_retry_delay_seconds,
        timeout_seconds=settings.transcript_write_timeout_seconds,
        call=lambda: repository.put_turn(turn),
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
            last_error_kind = "transcript_write_timeout"
        except Exception as exc:
            last_error_kind = _error_kind(exc)

        if attempt < attempts and delay_seconds:
            await asyncio.sleep(delay_seconds)

    raise TranscriptPersistenceError(operation=operation, error_kind=last_error_kind)


def _required_string(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TranscriptRepositoryError(f"transcript turn item is missing {key}")
    return value.strip()


def _speaker(value: Any) -> Speaker:
    if value in {"caller", "assistant"}:
        return value
    raise TranscriptRepositoryError("transcript turn item has invalid speaker")


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise TranscriptRepositoryError("transcript turn item has invalid confidence")


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
