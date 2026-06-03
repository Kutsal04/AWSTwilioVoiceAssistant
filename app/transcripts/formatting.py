from app.transcripts.repository import TranscriptTurn


def format_transcript(turns: list[TranscriptTurn]) -> str:
    if not turns:
        return "No transcript turns found."
    return "\n".join(format_turn(turn) for turn in sorted(turns, key=lambda turn: turn.turn_index))


def format_turn(turn: TranscriptTurn) -> str:
    return f"[{turn.turn_index:04d}] {turn.speaker}: {turn.text}"
