import asyncio

import app.main as main_module
from app.config import Settings
from app.sessions import SessionActor, SessionState, active_sessions


class FakeSessionRepository:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict]] = []

    def create_session(self, record: object) -> None:
        return None

    def update_session(self, session_id: str, **updates: object) -> None:
        self.updates.append((session_id, updates))

    def get_session(self, session_id: str) -> None:
        return None

    def list_sessions(self) -> list:
        return []


def make_actor() -> SessionActor:
    return SessionActor(
        session_id="shutdown-session",
        call_sid="CA-shutdown",
        persona_id="warm_clinical_followup",
        audio_queue_maxsize=2,
    )


def test_shutdown_abandons_and_finalizes_active_sessions(monkeypatch) -> None:
    async def run() -> None:
        await active_sessions.clear()
        actor = make_actor()
        await actor.activate()
        await active_sessions.create(actor)
        repository = FakeSessionRepository()
        settings = Settings(
            _env_file=None,
            graceful_shutdown_drain_seconds=0.5,
            session_write_retry_delay_seconds=0,
        )

        monkeypatch.setattr(main_module, "get_settings", lambda: settings)
        monkeypatch.setattr(main_module, "get_session_repository", lambda _settings: repository)

        await main_module.shutdown_active_sessions()

        assert actor.state == SessionState.ABANDONED
        assert await active_sessions.count() == 0
        assert repository.updates[-1][0] == "shutdown-session"
        assert repository.updates[-1][1]["status"] == SessionState.ABANDONED
        assert repository.updates[-1][1]["error_kind"] == "service_shutdown"

    asyncio.run(run())
