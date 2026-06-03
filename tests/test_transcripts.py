import asyncio
from decimal import Decimal

import pytest

from app.config import Settings
from app.nova import NovaParsedEvent
from app.transcripts import (
    DynamoTranscriptRepository,
    TranscriptPersistenceError,
    TranscriptRepositoryError,
    TranscriptTurn,
    TranscriptTurnBuffer,
    format_transcript,
    put_transcript_turn_with_retry,
    transcript_turn_from_item,
    transcript_turn_to_item,
)
from scripts.get_transcript import get_transcript_text


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, int], dict] = {}

    def put_item(self, *, Item: dict, ConditionExpression: str | None = None) -> None:
        key = (Item["session_id"], int(Item["turn_index"]))
        if ConditionExpression and key in self.items:
            raise RuntimeError("conditional write failed")
        self.items[key] = dict(Item)

    def query(
        self,
        *,
        KeyConditionExpression: str,
        ExpressionAttributeValues: dict,
        ScanIndexForward: bool,
    ) -> dict:
        session_id = ExpressionAttributeValues[":session_id"]
        items = [item for (stored_session_id, _), item in self.items.items() if stored_session_id == session_id]
        return {"Items": sorted(items, key=lambda item: item["turn_index"], reverse=not ScanIndexForward)}


class FakeDynamoResource:
    def __init__(self, table: FakeTable) -> None:
        self.table = table

    def Table(self, table_name: str) -> FakeTable:
        return self.table


class FakeTranscriptRepository:
    def __init__(self, fail_writes: int = 0) -> None:
        self.turns: list[TranscriptTurn] = []
        self.fail_writes = fail_writes
        self.write_attempts = 0

    def put_turn(self, turn: TranscriptTurn) -> None:
        self.write_attempts += 1
        if self.fail_writes:
            self.fail_writes -= 1
            raise RuntimeError("put failed")
        self.turns.append(turn)

    def list_turns(self, session_id: str) -> list[TranscriptTurn]:
        return [turn for turn in self.turns if turn.session_id == session_id]


def make_turn(turn_index: int, *, speaker: str = "caller", text: str = "hello") -> TranscriptTurn:
    return TranscriptTurn(
        session_id="session-123",
        turn_index=turn_index,
        speaker=speaker,
        text=text,
        transcript_item_id=f"content-{turn_index}",
        confidence=0.98,
        created_at=f"2026-06-02T00:00:0{turn_index}+00:00",
    )


def test_transcript_turn_item_round_trip() -> None:
    turn = make_turn(0)

    item = transcript_turn_to_item(turn)
    parsed = transcript_turn_from_item(item)

    assert parsed == turn
    assert item["confidence"] == Decimal("0.98")


def test_transcript_turn_item_rejects_invalid_speaker() -> None:
    with pytest.raises(TranscriptRepositoryError):
        transcript_turn_from_item(
            {
                "session_id": "session-123",
                "turn_index": 0,
                "speaker": "system",
                "text": "hello",
                "transcript_item_id": "content-1",
                "created_at": "2026-06-02T00:00:00+00:00",
            }
        )


def test_dynamo_transcript_repository_lists_turns_in_order() -> None:
    table = FakeTable()
    repository = DynamoTranscriptRepository(
        table_name="transcript_turns",
        dynamodb_resource=FakeDynamoResource(table),
    )

    repository.put_turn(make_turn(1, speaker="assistant", text="second"))
    repository.put_turn(make_turn(0, speaker="caller", text="first"))

    turns = repository.list_turns("session-123")

    assert [turn.turn_index for turn in turns] == [0, 1]
    assert [turn.text for turn in turns] == ["first", "second"]


def test_transcript_buffer_finalizes_partial_text_on_content_end() -> None:
    buffer = TranscriptTurnBuffer(session_id="session-123")

    assert buffer.handle_nova_event(
        NovaParsedEvent(
            event_type="content_start",
            raw_event={},
            role="USER",
            generation_stage="FINAL",
            content_type="TEXT",
            content_name="content-user-1",
        )
    ) is None
    assert buffer.handle_nova_event(
        NovaParsedEvent(
            event_type="text_output",
            raw_event={},
            content="hello ",
            confidence=0.7,
            content_name="content-user-1",
        )
    ) is None
    assert buffer.handle_nova_event(
        NovaParsedEvent(
            event_type="text_output",
            raw_event={},
            content="there",
            confidence=0.9,
            content_name="content-user-1",
        )
    ) is None

    turn = buffer.handle_nova_event(
        NovaParsedEvent(
            event_type="content_end",
            raw_event={},
            content_name="content-user-1",
        )
    )

    assert turn is not None
    assert turn.turn_index == 0
    assert turn.speaker == "caller"
    assert turn.text == "hello there"
    assert turn.transcript_item_id == "content-user-1"
    assert turn.confidence == 0.9


def test_transcript_buffer_ignores_text_without_final_content_start() -> None:
    buffer = TranscriptTurnBuffer(session_id="session-123")

    buffer.handle_nova_event(
        NovaParsedEvent(event_type="text_output", raw_event={}, content="hi", content_name="assistant-content")
    )
    turn = buffer.handle_nova_event(
        NovaParsedEvent(event_type="content_end", raw_event={}, content_name="assistant-content")
    )

    assert turn is None


def test_transcript_buffer_ignores_speculative_assistant_text() -> None:
    buffer = TranscriptTurnBuffer(session_id="session-123")

    buffer.handle_nova_event(
        NovaParsedEvent(
            event_type="content_start",
            raw_event={},
            role="ASSISTANT",
            generation_stage="SPECULATIVE",
            content_type="TEXT",
            content_name="assistant-content",
        )
    )
    buffer.handle_nova_event(
        NovaParsedEvent(
            event_type="text_output",
            raw_event={},
            content="This is a preview.",
            content_name="assistant-content",
        )
    )
    turn = buffer.handle_nova_event(
        NovaParsedEvent(event_type="content_end", raw_event={}, content_name="assistant-content")
    )

    assert turn is None


def test_transcript_buffer_ignores_interruption_control_text() -> None:
    buffer = TranscriptTurnBuffer(session_id="session-123")

    buffer.handle_nova_event(
        NovaParsedEvent(
            event_type="content_start",
            raw_event={},
            role="ASSISTANT",
            generation_stage="FINAL",
            content_type="TEXT",
            content_name="assistant-content",
        )
    )
    buffer.handle_nova_event(
        NovaParsedEvent(
            event_type="text_output",
            raw_event={},
            content='{ "interrupted" : true }',
            content_name="assistant-content",
        )
    )
    turn = buffer.handle_nova_event(
        NovaParsedEvent(event_type="content_end", raw_event={}, content_name="assistant-content")
    )

    assert turn is None


def test_put_transcript_turn_retry_policy_retries_briefly() -> None:
    async def run() -> None:
        settings = Settings(
            _env_file=None,
            transcript_write_max_attempts=2,
            transcript_write_retry_delay_seconds=0,
        )
        repository = FakeTranscriptRepository(fail_writes=1)

        await put_transcript_turn_with_retry(repository=repository, turn=make_turn(0), settings=settings)

        assert repository.write_attempts == 2
        assert len(repository.turns) == 1

    asyncio.run(run())


def test_put_transcript_turn_retry_policy_raises_after_attempts() -> None:
    async def run() -> None:
        settings = Settings(
            _env_file=None,
            transcript_write_max_attempts=2,
            transcript_write_retry_delay_seconds=0,
        )
        repository = FakeTranscriptRepository(fail_writes=2)

        with pytest.raises(TranscriptPersistenceError):
            await put_transcript_turn_with_retry(repository=repository, turn=make_turn(0), settings=settings)

        assert repository.write_attempts == 2

    asyncio.run(run())


def test_transcript_cli_formatting_orders_turns() -> None:
    repository = FakeTranscriptRepository()
    repository.turns = [
        make_turn(1, speaker="assistant", text="I can help."),
        make_turn(0, speaker="caller", text="Hello."),
    ]

    assert get_transcript_text(repository=repository, session_id="session-123") == (
        "[0000] caller: Hello.\n[0001] assistant: I can help."
    )


def test_format_transcript_handles_empty_results() -> None:
    assert format_transcript([]) == "No transcript turns found."
