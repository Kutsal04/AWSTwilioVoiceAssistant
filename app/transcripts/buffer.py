import json
from dataclasses import dataclass, field

from app.nova import NovaParsedEvent
from app.transcripts.repository import Speaker, TranscriptTurn, utc_now_iso


@dataclass
class PartialTranscript:
    content_name: str
    speaker: Speaker
    generation_stage: str | None
    parts: list[str] = field(default_factory=list)
    confidence: float | None = None


class TranscriptTurnBuffer:
    def __init__(self, *, session_id: str, first_turn_index: int = 0) -> None:
        self.session_id = session_id
        self.next_turn_index = first_turn_index
        self._speakers_by_content_name: dict[str, Speaker] = {}
        self._partials_by_content_name: dict[str, PartialTranscript] = {}

    def handle_nova_event(self, event: NovaParsedEvent) -> TranscriptTurn | None:
        if event.event_type == "content_start":
            self._remember_content_speaker(event)
            return None

        if event.event_type == "text_output":
            self._buffer_text_output(event)
            return None

        if event.event_type == "content_end":
            return self._finalize_content(event.content_name)

        return None

    def _remember_content_speaker(self, event: NovaParsedEvent) -> None:
        if event.content_name is None:
            return
        if event.content_type is not None and event.content_type != "TEXT":
            return
        speaker = speaker_from_nova_role(event.role)
        if speaker is not None:
            self._speakers_by_content_name[event.content_name] = speaker
        if event.generation_stage is not None:
            partial = self._partials_by_content_name.get(event.content_name)
            if partial is None and speaker is not None:
                partial = PartialTranscript(
                    content_name=event.content_name,
                    speaker=speaker,
                    generation_stage=event.generation_stage,
                )
                self._partials_by_content_name[event.content_name] = partial
            elif partial is not None:
                partial.generation_stage = event.generation_stage

    def _buffer_text_output(self, event: NovaParsedEvent) -> None:
        if event.content is None or not event.content or event.content_name is None:
            return
        if is_control_text(event.content):
            return

        speaker = self._speakers_by_content_name.get(event.content_name, "assistant")
        partial = self._partials_by_content_name.get(event.content_name)
        if partial is None:
            partial = PartialTranscript(content_name=event.content_name, speaker=speaker, generation_stage=None)
            self._partials_by_content_name[event.content_name] = partial

        partial.parts.append(event.content)
        if event.confidence is not None:
            partial.confidence = event.confidence

    def _finalize_content(self, content_name: str | None) -> TranscriptTurn | None:
        if content_name is None:
            return None
        partial = self._partials_by_content_name.pop(content_name, None)
        if partial is None:
            return None
        if partial.generation_stage != "FINAL":
            return None

        text = "".join(partial.parts).strip()
        if not text or is_control_text(text):
            return None

        turn = TranscriptTurn(
            session_id=self.session_id,
            turn_index=self.next_turn_index,
            speaker=partial.speaker,
            text=text,
            transcript_item_id=partial.content_name,
            confidence=partial.confidence,
            created_at=utc_now_iso(),
        )
        self.next_turn_index += 1
        return turn


def speaker_from_nova_role(role: str | None) -> Speaker | None:
    if role == "USER":
        return "caller"
    if role == "ASSISTANT":
        return "assistant"
    return None


def is_control_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return False
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(decoded, dict) and decoded.get("interrupted") is True
