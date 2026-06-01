from collections.abc import Iterator

import pytest

from app.config import get_settings
from tests.fakes import FakeExternalClient, FakeRepository, make_fake_external_client, make_fake_repository


@pytest.fixture(autouse=True)
def reset_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_repository() -> FakeRepository:
    return make_fake_repository()


@pytest.fixture
def fake_external_client() -> FakeExternalClient:
    return make_fake_external_client()

