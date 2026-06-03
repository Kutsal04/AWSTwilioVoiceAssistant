from app.sessions import SessionRecord, SessionState
from scripts.report import build_report_summary, format_report, get_report_text


class FakeSessionRepository:
    def __init__(self, sessions: list[SessionRecord]) -> None:
        self.sessions = sessions

    def create_session(self, record: SessionRecord) -> None:
        self.sessions.append(record)

    def update_session(self, session_id: str, **updates: object) -> None:
        return None

    def get_session(self, session_id: str) -> SessionRecord | None:
        return next((session for session in self.sessions if session.session_id == session_id), None)

    def list_sessions(self) -> list[SessionRecord]:
        return self.sessions


def make_session(
    session_id: str,
    *,
    persona_id: str = "warm_clinical_followup",
    status: SessionState = SessionState.COMPLETED,
    started_at: str = "2026-06-03T00:00:00+00:00",
    ended_at: str | None = "2026-06-03T00:01:00+00:00",
    error_kind: str | None = None,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        call_sid=f"CA-{session_id}",
        persona_id=persona_id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        last_event_at=ended_at or started_at,
        outcome_description="test",
        error_kind=error_kind,
    )


def test_build_report_summary_aggregates_sessions() -> None:
    summary = build_report_summary(
        [
            make_session("1", persona_id="warm_clinical_followup"),
            make_session(
                "2",
                persona_id="appointment_reminder",
                started_at="2026-06-03T00:00:00+00:00",
                ended_at="2026-06-03T00:03:00+00:00",
            ),
            make_session("3", persona_id="appointment_reminder", status=SessionState.FAILED, error_kind="NovaError"),
            make_session("4", persona_id="warm_clinical_followup", status=SessionState.ACTIVE, ended_at=None),
        ]
    )

    assert summary.total_sessions == 4
    assert summary.sessions_per_persona == {
        "appointment_reminder": 2,
        "warm_clinical_followup": 2,
    }
    assert summary.average_call_length_seconds == 100.0
    assert summary.error_count == 1
    assert summary.error_rate == 0.25


def test_format_report_is_operator_readable() -> None:
    summary = build_report_summary([make_session("1")])

    assert format_report(summary) == (
        "Voice Agent Session Report\n"
        "total_sessions: 1\n"
        "sessions_per_persona:\n"
        "  warm_clinical_followup: 1\n"
        "average_call_length_seconds: 60.0\n"
        "error_count: 0\n"
        "error_rate: 0.0%"
    )


def test_get_report_text_uses_repository_records() -> None:
    repository = FakeSessionRepository([make_session("1"), make_session("2", status=SessionState.ABANDONED)])

    report = get_report_text(repository=repository)

    assert "total_sessions: 2" in report
    assert "error_count: 1" in report
    assert "error_rate: 50.0%" in report


def test_empty_report_handles_no_sessions() -> None:
    summary = build_report_summary([])

    assert summary.total_sessions == 0
    assert summary.average_call_length_seconds is None
    assert summary.error_rate == 0.0
    assert "average_call_length_seconds: n/a" in format_report(summary)
