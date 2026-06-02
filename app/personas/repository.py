import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import boto3

from app.config import Settings


@dataclass(frozen=True)
class Persona:
    persona_id: str
    display_name: str
    system_prompt: str
    active: bool = True
    version: int = 1
    schema_version: int = 1


class PersonaRepositoryError(RuntimeError):
    pass


class PersonaRepository(Protocol):
    def get_persona(self, persona_id: str) -> Persona | None:
        ...

    def put_persona(self, persona: Persona) -> None:
        ...


class DynamoPersonaRepository:
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

    def get_persona(self, persona_id: str) -> Persona | None:
        try:
            response = self.table.get_item(Key={"persona_id": persona_id})
        except Exception as exc:
            raise PersonaRepositoryError("failed to get persona") from exc

        item = response.get("Item")
        if item is None:
            return None
        return persona_from_item(item)

    def put_persona(self, persona: Persona) -> None:
        try:
            self.table.put_item(Item=persona_to_item(persona))
        except Exception as exc:
            raise PersonaRepositoryError("failed to put persona") from exc


class PersonaSelectionError(ValueError):
    def __init__(self, *, requested_persona_id: str, error_kind: str) -> None:
        super().__init__(error_kind)
        self.requested_persona_id = requested_persona_id
        self.error_kind = error_kind


def persona_from_item(item: dict[str, Any]) -> Persona:
    persona_id = _required_string(item, "persona_id")
    display_name = _required_string(item, "display_name")
    system_prompt = _required_string(item, "system_prompt")
    active = bool(item.get("active", True))
    version = int(item.get("version", 1))
    schema_version = int(item.get("schema_version", 1))
    return Persona(
        persona_id=persona_id,
        display_name=display_name,
        system_prompt=system_prompt,
        active=active,
        version=version,
        schema_version=schema_version,
    )


def persona_to_item(persona: Persona) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "persona_id": persona.persona_id,
        "display_name": persona.display_name,
        "system_prompt": persona.system_prompt,
        "active": persona.active,
        "version": persona.version,
        "schema_version": persona.schema_version,
        "updated_at": now,
    }


async def resolve_persona(
    *,
    requested_persona_id: str,
    settings: Settings,
    repository: PersonaRepository,
) -> Persona:
    persona = await _lookup_persona(requested_persona_id, settings=settings, repository=repository)
    if persona is not None and persona.active:
        return persona

    if settings.persona_lookup_fallback_enabled and requested_persona_id != settings.default_persona_id:
        fallback = await _lookup_persona(settings.default_persona_id, settings=settings, repository=repository)
        if fallback is not None and fallback.active:
            return fallback

    error_kind = "inactive_persona" if persona is not None else "missing_persona"
    raise PersonaSelectionError(requested_persona_id=requested_persona_id, error_kind=error_kind)


async def _lookup_persona(
    persona_id: str,
    *,
    settings: Settings,
    repository: PersonaRepository,
) -> Persona | None:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(repository.get_persona, persona_id),
            timeout=settings.persona_lookup_timeout_seconds,
        )
    except TimeoutError as exc:
        raise PersonaSelectionError(requested_persona_id=persona_id, error_kind="persona_lookup_timeout") from exc
    except PersonaSelectionError:
        raise
    except Exception as exc:
        raise PersonaSelectionError(requested_persona_id=persona_id, error_kind=type(exc).__name__) from exc


def _required_string(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PersonaRepositoryError(f"persona item is missing {key}")
    return value.strip()
