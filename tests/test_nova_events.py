import base64

import pytest

from app.nova import (
    DEFAULT_NOVA_MODEL_ID,
    NOVA_INPUT_SAMPLE_RATE_HZ,
    NOVA_OUTPUT_SAMPLE_RATE_HZ,
    NovaEventParseError,
    audio_content_start_event,
    audio_input_event,
    content_end_event,
    event_to_bytes,
    parse_nova_event_bytes,
    prompt_end_event,
    prompt_start_event,
    session_end_event,
    session_start_event,
    system_prompt_events,
)


def test_nova_model_and_audio_defaults_match_phase_6_contract() -> None:
    assert DEFAULT_NOVA_MODEL_ID == "amazon.nova-2-sonic-v1:0"
    assert NOVA_INPUT_SAMPLE_RATE_HZ == 16_000
    assert NOVA_OUTPUT_SAMPLE_RATE_HZ == 24_000


def test_session_and_prompt_start_events() -> None:
    session_start = session_start_event()
    prompt_start = prompt_start_event("prompt-1", voice_id="matthew")

    assert session_start["event"]["sessionStart"]["inferenceConfiguration"]["maxTokens"] == 1024
    assert session_start["event"]["sessionStart"]["turnDetectionConfiguration"]["endpointingSensitivity"] == "HIGH"
    assert prompt_start["event"]["promptStart"]["promptName"] == "prompt-1"
    assert prompt_start["event"]["promptStart"]["audioOutputConfiguration"]["sampleRateHertz"] == 24_000


def test_system_prompt_events_are_ordered() -> None:
    events = system_prompt_events("prompt-1", "content-1", "system prompt")

    assert [next(iter(event["event"])) for event in events] == ["contentStart", "textInput", "contentEnd"]
    assert events[0]["event"]["contentStart"]["role"] == "SYSTEM"
    assert events[1]["event"]["textInput"]["content"] == "system prompt"


def test_audio_input_events_encode_pcm16_as_base64() -> None:
    start = audio_content_start_event("prompt-1", "audio-1")
    audio = audio_input_event("prompt-1", "audio-1", b"\x01\x02")
    end = content_end_event("prompt-1", "audio-1")

    assert start["event"]["contentStart"]["audioInputConfiguration"]["sampleRateHertz"] == 16_000
    assert audio["event"]["audioInput"]["content"] == "AQI="
    assert end["event"]["contentEnd"]["contentName"] == "audio-1"


def test_end_events() -> None:
    assert prompt_end_event("prompt-1") == {"event": {"promptEnd": {"promptName": "prompt-1"}}}
    assert session_end_event() == {"event": {"sessionEnd": {}}}


def test_event_to_bytes_outputs_compact_json() -> None:
    assert event_to_bytes({"event": {"sessionEnd": {}}}) == b'{"event":{"sessionEnd":{}}}'


def test_parse_content_start_event() -> None:
    parsed = parse_nova_event_bytes(
        b'{"event":{"contentStart":{"role":"ASSISTANT","contentId":"content-1","type":"TEXT","additionalModelFields":"{\\"generationStage\\":\\"FINAL\\"}"}}}'
    )

    assert parsed.event_type == "content_start"
    assert parsed.role == "ASSISTANT"
    assert parsed.content_name == "content-1"
    assert parsed.content_type == "TEXT"
    assert parsed.generation_stage == "FINAL"


def test_parse_text_output_event() -> None:
    parsed = parse_nova_event_bytes(
        b'{"event":{"textOutput":{"contentId":"content-1","content":"hello","confidence":0.91}}}'
    )

    assert parsed.event_type == "text_output"
    assert parsed.content == "hello"
    assert parsed.confidence == 0.91
    assert parsed.content_name == "content-1"


def test_parse_audio_output_event() -> None:
    content = base64.b64encode(b"\x00\x01").decode("ascii")
    parsed = parse_nova_event_bytes(
        f'{{"event":{{"audioOutput":{{"contentId":"audio-1","content":"{content}"}}}}}}'.encode("utf-8")
    )

    assert parsed.event_type == "audio_output"
    assert parsed.audio_bytes == b"\x00\x01"
    assert parsed.content_name == "audio-1"


def test_parse_events_accept_legacy_content_name_identifier() -> None:
    parsed = parse_nova_event_bytes(
        b'{"event":{"textOutput":{"contentName":"legacy-content","content":"hello"}}}'
    )

    assert parsed.content_name == "legacy-content"


def test_parse_content_end_event_reads_stop_reason() -> None:
    parsed = parse_nova_event_bytes(
        b'{"event":{"contentEnd":{"contentId":"content-1","type":"TEXT","stopReason":"END_TURN"}}}'
    )

    assert parsed.event_type == "content_end"
    assert parsed.content_name == "content-1"
    assert parsed.content_type == "TEXT"
    assert parsed.stop_reason == "END_TURN"


def test_parse_usage_event() -> None:
    parsed = parse_nova_event_bytes(b'{"event":{"usageEvent":{"details":{}}}}')

    assert parsed.event_type == "usage"


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"not_event":{}}',
        b'{"event":{"audioOutput":{"content":"not base64!"}}}',
    ],
)
def test_parse_nova_event_rejects_malformed_payloads(payload: bytes) -> None:
    with pytest.raises(NovaEventParseError):
        parse_nova_event_bytes(payload)
