import asyncio

import pytest

from app.config import Settings
from app.personas import (
    DynamoPersonaRepository,
    Persona,
    PersonaRepositoryError,
    PersonaSelectionError,
    resolve_persona,
)
from app.personas.repository import persona_from_item, persona_to_item


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def get_item(self, *, Key: dict) -> dict:
        item = self.items.get(Key["persona_id"])
        return {"Item": item} if item else {}

    def put_item(self, *, Item: dict) -> None:
        self.items[Item["persona_id"]] = Item


class FakeDynamoResource:
    def __init__(self, table: FakeTable) -> None:
        self.table = table

    def Table(self, table_name: str) -> FakeTable:
        return self.table


class FakePersonaRepository:
    def __init__(self, personas: list[Persona]) -> None:
        self.personas = {persona.persona_id: persona for persona in personas}

    def get_persona(self, persona_id: str) -> Persona | None:
        return self.personas.get(persona_id)

    def put_persona(self, persona: Persona) -> None:
        self.personas[persona.persona_id] = persona


def test_persona_item_round_trip() -> None:
    persona = Persona(
        persona_id="appointment_reminder",
        display_name="Appointment Reminder",
        system_prompt="prompt",
        active=True,
        version=2,
    )

    item = persona_to_item(persona)
    parsed = persona_from_item(item)

    assert parsed == persona
    assert item["updated_at"]


def test_persona_item_rejects_missing_required_fields() -> None:
    with pytest.raises(PersonaRepositoryError):
        persona_from_item({"persona_id": "missing-prompt", "display_name": "Missing Prompt"})


def test_dynamo_persona_repository_gets_and_puts_items() -> None:
    table = FakeTable()
    repository = DynamoPersonaRepository(table_name="personas", dynamodb_resource=FakeDynamoResource(table))
    persona = Persona(
        persona_id="warm_clinical_followup",
        display_name="Warm Clinical Follow-up",
        system_prompt="prompt",
    )

    repository.put_persona(persona)

    assert repository.get_persona("warm_clinical_followup") == persona
    assert repository.get_persona("missing") is None


def test_resolve_persona_selects_requested_active_persona() -> None:
    async def run() -> None:
        settings = Settings(_env_file=None)
        repository = FakePersonaRepository(
            [
                Persona("warm_clinical_followup", "Warm", "default prompt"),
                Persona("appointment_reminder", "Reminder", "reminder prompt"),
            ]
        )

        persona = await resolve_persona(
            requested_persona_id="appointment_reminder",
            settings=settings,
            repository=repository,
        )

        assert persona.persona_id == "appointment_reminder"
        assert persona.system_prompt == "reminder prompt"

    asyncio.run(run())


def test_resolve_persona_falls_back_to_default_when_requested_missing() -> None:
    async def run() -> None:
        settings = Settings(_env_file=None, default_persona_id="warm_clinical_followup")
        repository = FakePersonaRepository([Persona("warm_clinical_followup", "Warm", "default prompt")])

        persona = await resolve_persona(
            requested_persona_id="unknown",
            settings=settings,
            repository=repository,
        )

        assert persona.persona_id == "warm_clinical_followup"

    asyncio.run(run())


def test_resolve_persona_fails_when_fallback_disabled() -> None:
    async def run() -> None:
        settings = Settings(
            _env_file=None,
            default_persona_id="warm_clinical_followup",
            persona_lookup_fallback_enabled=False,
        )
        repository = FakePersonaRepository([Persona("warm_clinical_followup", "Warm", "default prompt")])

        with pytest.raises(PersonaSelectionError) as exc_info:
            await resolve_persona(
                requested_persona_id="unknown",
                settings=settings,
                repository=repository,
            )

        assert exc_info.value.error_kind == "missing_persona"

    asyncio.run(run())


def test_resolve_persona_fails_when_default_inactive() -> None:
    async def run() -> None:
        settings = Settings(_env_file=None, default_persona_id="warm_clinical_followup")
        repository = FakePersonaRepository(
            [Persona("warm_clinical_followup", "Warm", "default prompt", active=False)]
        )

        with pytest.raises(PersonaSelectionError) as exc_info:
            await resolve_persona(
                requested_persona_id="warm_clinical_followup",
                settings=settings,
                repository=repository,
            )

        assert exc_info.value.error_kind == "inactive_persona"

    asyncio.run(run())
