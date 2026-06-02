import asyncio

from app.sessions.actor import SessionActor


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionActor] = {}
        self._lock = asyncio.Lock()

    async def create(self, actor: SessionActor) -> SessionActor:
        async with self._lock:
            if actor.session_id in self._sessions:
                raise ValueError(f"session already exists: {actor.session_id}")
            self._sessions[actor.session_id] = actor
            return actor

    async def get(self, session_id: str) -> SessionActor | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> SessionActor | None:
        async with self._lock:
            return self._sessions.pop(session_id, None)

    async def count(self) -> int:
        async with self._lock:
            return len(self._sessions)

    async def clear(self) -> None:
        async with self._lock:
            self._sessions.clear()


active_sessions = SessionRegistry()
