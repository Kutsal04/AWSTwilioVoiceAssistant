import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Literal


NOVA_INPUT_SAMPLE_RATE_HZ = 16_000
NOVA_OUTPUT_SAMPLE_RATE_HZ = 24_000
NOVA_PCM_SAMPLE_SIZE_BITS = 16
NOVA_CHANNEL_COUNT = 1
DEFAULT_NOVA_MODEL_ID = "amazon.nova-2-sonic-v1:0"

NovaParsedEventType = Literal[
    "content_start",
    "text_output",
    "audio_output",
    "content_end",
    "prompt_end",
    "session_end",
    "tool_use",
    "usage",
    "unknown",
]


class NovaEventParseError(ValueError):
    pass


@dataclass(frozen=True)
class NovaParsedEvent:
    event_type: NovaParsedEventType
    raw_event: dict[str, Any]
    role: str | None = None
    content: str | None = None
    audio_bytes: bytes | None = None
    content_name: str | None = None


def event_to_bytes(event: dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":")).encode("utf-8")


def session_start_event(max_tokens: int = 1024, top_p: float = 0.9, temperature: float = 0.7) -> dict[str, Any]:
    return {
        "event": {
            "sessionStart": {
                "inferenceConfiguration": {
                    "maxTokens": max_tokens,
                    "topP": top_p,
                    "temperature": temperature,
                },
                "turnDetectionConfiguration": {
                    "endpointingSensitivity": "HIGH",
                },
            }
        }
    }


def prompt_start_event(prompt_name: str, voice_id: str = "matthew") -> dict[str, Any]:
    return {
        "event": {
            "promptStart": {
                "promptName": prompt_name,
                "textOutputConfiguration": {"mediaType": "text/plain"},
                "audioOutputConfiguration": {
                    "mediaType": "audio/lpcm",
                    "sampleRateHertz": NOVA_OUTPUT_SAMPLE_RATE_HZ,
                    "sampleSizeBits": NOVA_PCM_SAMPLE_SIZE_BITS,
                    "channelCount": NOVA_CHANNEL_COUNT,
                    "voiceId": voice_id,
                    "encoding": "base64",
                    "audioType": "SPEECH",
                },
            }
        }
    }


def text_content_start_event(prompt_name: str, content_name: str, role: str = "SYSTEM") -> dict[str, Any]:
    return {
        "event": {
            "contentStart": {
                "promptName": prompt_name,
                "contentName": content_name,
                "type": "TEXT",
                "interactive": True,
                "role": role,
                "textInputConfiguration": {"mediaType": "text/plain"},
            }
        }
    }


def text_input_event(prompt_name: str, content_name: str, content: str) -> dict[str, Any]:
    return {
        "event": {
            "textInput": {
                "promptName": prompt_name,
                "contentName": content_name,
                "content": content,
            }
        }
    }


def audio_content_start_event(prompt_name: str, content_name: str) -> dict[str, Any]:
    return {
        "event": {
            "contentStart": {
                "promptName": prompt_name,
                "contentName": content_name,
                "type": "AUDIO",
                "interactive": True,
                "role": "USER",
                "audioInputConfiguration": {
                    "mediaType": "audio/lpcm",
                    "sampleRateHertz": NOVA_INPUT_SAMPLE_RATE_HZ,
                    "sampleSizeBits": NOVA_PCM_SAMPLE_SIZE_BITS,
                    "channelCount": NOVA_CHANNEL_COUNT,
                    "audioType": "SPEECH",
                    "encoding": "base64",
                },
            }
        }
    }


def audio_input_event(prompt_name: str, content_name: str, pcm16_audio: bytes) -> dict[str, Any]:
    return {
        "event": {
            "audioInput": {
                "promptName": prompt_name,
                "contentName": content_name,
                "content": base64.b64encode(pcm16_audio).decode("ascii"),
            }
        }
    }


def content_end_event(prompt_name: str, content_name: str) -> dict[str, Any]:
    return {
        "event": {
            "contentEnd": {
                "promptName": prompt_name,
                "contentName": content_name,
            }
        }
    }


def prompt_end_event(prompt_name: str) -> dict[str, Any]:
    return {"event": {"promptEnd": {"promptName": prompt_name}}}


def session_end_event() -> dict[str, Any]:
    return {"event": {"sessionEnd": {}}}


def system_prompt_events(prompt_name: str, content_name: str, system_prompt: str) -> list[dict[str, Any]]:
    return [
        text_content_start_event(prompt_name, content_name, role="SYSTEM"),
        text_input_event(prompt_name, content_name, system_prompt),
        content_end_event(prompt_name, content_name),
    ]


def parse_nova_event_bytes(payload: bytes) -> NovaParsedEvent:
    try:
        decoded = payload.decode("utf-8")
        raw = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NovaEventParseError("Nova output payload is not valid UTF-8 JSON") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("event"), dict):
        raise NovaEventParseError("Nova output payload is missing event object")

    event = raw["event"]
    if "contentStart" in event:
        content_start = event["contentStart"]
        return NovaParsedEvent(
            event_type="content_start",
            raw_event=raw,
            role=_string_or_none(content_start.get("role")),
            content_name=_string_or_none(content_start.get("contentName")),
        )
    if "textOutput" in event:
        text_output = event["textOutput"]
        return NovaParsedEvent(
            event_type="text_output",
            raw_event=raw,
            content=_string_or_none(text_output.get("content")),
            content_name=_string_or_none(text_output.get("contentName")),
        )
    if "audioOutput" in event:
        audio_output = event["audioOutput"]
        content = _string_or_none(audio_output.get("content"))
        if content is None:
            raise NovaEventParseError("Nova audioOutput event is missing content")
        try:
            audio_bytes = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise NovaEventParseError("Nova audioOutput content is not valid base64") from exc
        return NovaParsedEvent(
            event_type="audio_output",
            raw_event=raw,
            audio_bytes=audio_bytes,
            content_name=_string_or_none(audio_output.get("contentName")),
        )
    if "contentEnd" in event:
        content_end = event["contentEnd"]
        return NovaParsedEvent(
            event_type="content_end",
            raw_event=raw,
            content_name=_string_or_none(content_end.get("contentName")),
        )
    if "promptEnd" in event:
        return NovaParsedEvent(event_type="prompt_end", raw_event=raw)
    if "sessionEnd" in event:
        return NovaParsedEvent(event_type="session_end", raw_event=raw)
    if "toolUse" in event:
        return NovaParsedEvent(event_type="tool_use", raw_event=raw)
    if "usageEvent" in event:
        return NovaParsedEvent(event_type="usage", raw_event=raw)
    return NovaParsedEvent(event_type="unknown", raw_event=raw)


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None
