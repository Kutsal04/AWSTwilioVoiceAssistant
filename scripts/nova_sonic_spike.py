import argparse
import asyncio
import logging
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.logging import configure_logging
from app.nova import (
    NovaClient,
    audio_content_start_event,
    audio_input_event,
    content_end_event,
    prompt_end_event,
    prompt_start_event,
    session_end_event,
    session_start_event,
    system_prompt_events,
)


DEFAULT_SYSTEM_PROMPT = (
    "You are a concise voice assistant. Keep responses brief and suitable for a real-time phone conversation."
)


async def run_spike(args: argparse.Namespace) -> None:
    settings = get_settings()
    client = NovaClient(model_id=args.model_id or settings.nova_model_id, region=args.region or settings.bedrock_region)
    prompt_name = str(uuid4())
    system_content_name = str(uuid4())
    audio_content_name = str(uuid4())

    print(f"opening Nova stream model={client.model_id} region={client.region}")
    await asyncio.wait_for(client.open(), timeout=args.open_timeout_seconds)
    print("stream opened")

    try:
        await client.send_event(session_start_event())
        await client.send_event(prompt_start_event(prompt_name, voice_id=args.voice_id))
        for event in system_prompt_events(prompt_name, system_content_name, args.system_prompt):
            await client.send_event(event)

        if args.pcm16_file:
            audio_bytes = Path(args.pcm16_file).read_bytes()
            await client.send_event(audio_content_start_event(prompt_name, audio_content_name))
            await client.send_event(audio_input_event(prompt_name, audio_content_name, audio_bytes))
            await client.send_event(content_end_event(prompt_name, audio_content_name))
            print(f"sent pcm16 audio bytes={len(audio_bytes)}")

            for _ in range(args.max_events):
                event = await asyncio.wait_for(client.receive_event(), timeout=args.response_timeout_seconds)
                print_event_summary(event.event_type, event.role, event.content_name, event.audio_bytes, event.raw_event)
        else:
            print("no pcm16 file supplied; opened stream and sent initialization events only")

        await client.send_event(prompt_end_event(prompt_name))
        await client.send_event(session_end_event())
    finally:
        await client.close()
        print("stream closed")


def print_event_summary(
    event_type: str,
    role: str | None,
    content_name: str | None,
    audio_bytes: bytes | None,
    raw_event: dict | None = None,
) -> None:
    details = [f"event={event_type}"]
    if role:
        details.append(f"role={role}")
    if content_name:
        details.append(f"content_name={content_name}")
    if audio_bytes is not None:
        details.append(f"audio_bytes={len(audio_bytes)}")
    if event_type == "unknown" and raw_event:
        event_payload = raw_event.get("event")
        if isinstance(event_payload, dict):
            details.append(f"raw_event_keys={','.join(event_payload.keys())}")
    print(" ".join(details))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open an isolated Amazon Nova 2 Sonic stream for Phase 6 validation.")
    parser.add_argument("--region", default=None, help="AWS region. Defaults to BEDROCK_REGION.")
    parser.add_argument("--model-id", default=None, help="Nova Sonic model id. Defaults to NOVA_MODEL_ID.")
    parser.add_argument("--voice-id", default="matthew", help="Nova output voice id.")
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument("--pcm16-file", default=None, help="Optional raw 16 kHz mono PCM16 audio file to send.")
    parser.add_argument("--open-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-events", type=int, default=5, help="Maximum response events to read when audio is supplied.")
    parser.add_argument("--response-timeout-seconds", type=float, default=20.0)
    return parser.parse_args()


def main() -> None:
    configure_logging(logging.INFO)
    asyncio.run(run_spike(parse_args()))


if __name__ == "__main__":
    main()
