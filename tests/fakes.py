from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeRepository:
    records: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    storage: dict[str, Any] = field(default_factory=dict)

    def put(self, key: str, value: Any) -> Any:
        self.records.append(("put", (key, value), {}))
        self.storage[key] = value
        return value

    def get(self, key: str) -> Any:
        self.records.append(("get", (key,), {}))
        return self.storage.get(key)

    def list(self) -> list[Any]:
        self.records.append(("list", tuple(), {}))
        return list(self.storage.values())


@dataclass
class FakeExternalClient:
    sent: list[Any] = field(default_factory=list)
    responses: list[Any] = field(default_factory=list)

    def send(self, payload: Any) -> Any:
        self.sent.append(payload)
        if self.responses:
            return self.responses.pop(0)
        return None

    def queue_response(self, payload: Any) -> None:
        self.responses.append(payload)


def make_fake_repository() -> FakeRepository:
    return FakeRepository()


def make_fake_external_client() -> FakeExternalClient:
    return FakeExternalClient()
