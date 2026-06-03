import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.sessions import DynamoSessionRepository, SessionRecord, SessionRepository, SessionState


@dataclass(frozen=True)
class ReportSummary:
    total_sessions: int
    sessions_per_persona: dict[str, int]
    average_call_length_seconds: float | None
    error_count: int
    error_rate: float


def build_report_summary(sessions: list[SessionRecord]) -> ReportSummary:
    total_sessions = len(sessions)
    sessions_per_persona = dict(sorted(Counter(session.persona_id for session in sessions).items()))
    durations = [_duration_seconds(session) for session in sessions]
    complete_durations = [duration for duration in durations if duration is not None]
    average_call_length_seconds = (
        sum(complete_durations) / len(complete_durations) if complete_durations else None
    )
    error_count = sum(1 for session in sessions if _is_error_session(session))
    error_rate = error_count / total_sessions if total_sessions else 0.0
    return ReportSummary(
        total_sessions=total_sessions,
        sessions_per_persona=sessions_per_persona,
        average_call_length_seconds=average_call_length_seconds,
        error_count=error_count,
        error_rate=error_rate,
    )


def format_report(summary: ReportSummary) -> str:
    lines = [
        "Voice Agent Session Report",
        f"total_sessions: {summary.total_sessions}",
        "sessions_per_persona:",
    ]
    if summary.sessions_per_persona:
        for persona_id, count in summary.sessions_per_persona.items():
            lines.append(f"  {persona_id}: {count}")
    else:
        lines.append("  none: 0")

    average = (
        f"{summary.average_call_length_seconds:.1f}"
        if summary.average_call_length_seconds is not None
        else "n/a"
    )
    lines.extend(
        [
            f"average_call_length_seconds: {average}",
            f"error_count: {summary.error_count}",
            f"error_rate: {summary.error_rate:.1%}",
        ]
    )
    return "\n".join(lines)


def get_report_text(*, repository: SessionRepository) -> str:
    return format_report(build_report_summary(repository.list_sessions()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report aggregate voice-agent session metrics from DynamoDB.")
    parser.add_argument("--table-name", default=None, help="DynamoDB sessions table name. Defaults to SESSIONS_TABLE_NAME.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    table_name = args.table_name or settings.sessions_table_name
    repository = DynamoSessionRepository(table_name=table_name)
    print(get_report_text(repository=repository))


def _duration_seconds(session: SessionRecord) -> float | None:
    if session.ended_at is None:
        return None
    started_at = _parse_iso_datetime(session.started_at)
    ended_at = _parse_iso_datetime(session.ended_at)
    return max((ended_at - started_at).total_seconds(), 0.0)


def _parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _is_error_session(session: SessionRecord) -> bool:
    if session.error_kind:
        return True
    return session.status in {SessionState.FAILED, SessionState.ABANDONED}


if __name__ == "__main__":
    main()
