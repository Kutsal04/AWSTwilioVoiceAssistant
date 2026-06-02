"""Amazon Nova Sonic integration boundary."""

from app.nova.events import (
    DEFAULT_NOVA_MODEL_ID,
    NOVA_INPUT_SAMPLE_RATE_HZ,
    NOVA_OUTPUT_SAMPLE_RATE_HZ,
    NovaEventParseError,
    NovaParsedEvent,
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
    text_content_start_event,
    text_input_event,
)

try:
    from app.nova.client import NovaClient, NovaClientError
except ModuleNotFoundError as exc:
    if exc.name != "aws_sdk_bedrock_runtime":
        raise

    class NovaClientError(RuntimeError):
        pass

    class NovaClient:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise NovaClientError(
                "aws-sdk-bedrock-runtime is required for NovaClient. "
                "Install dependencies with `python -m pip install -r requirements.txt`."
            )

__all__ = [
    "DEFAULT_NOVA_MODEL_ID",
    "NOVA_INPUT_SAMPLE_RATE_HZ",
    "NOVA_OUTPUT_SAMPLE_RATE_HZ",
    "NovaClient",
    "NovaClientError",
    "NovaEventParseError",
    "NovaParsedEvent",
    "audio_content_start_event",
    "audio_input_event",
    "content_end_event",
    "event_to_bytes",
    "parse_nova_event_bytes",
    "prompt_end_event",
    "prompt_start_event",
    "session_end_event",
    "session_start_event",
    "system_prompt_events",
    "text_content_start_event",
    "text_input_event",
]
