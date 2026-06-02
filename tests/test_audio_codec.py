import math
import struct

import pytest

from app.audio import (
    AudioConversionError,
    decode_twilio_payload,
    encode_twilio_payload,
    mu_law_to_pcm16,
    nova_pcm16_to_twilio_payload,
    pcm16_to_mu_law,
    resample_pcm16_mono,
    twilio_payload_to_nova_pcm16,
    validate_pcm16,
)


def pcm16_samples(pcm16_audio: bytes) -> tuple[int, ...]:
    return struct.unpack(f"<{len(pcm16_audio) // 2}h", pcm16_audio)


def pcm16_rms(pcm16_audio: bytes) -> float:
    samples = pcm16_samples(pcm16_audio)
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def sine_pcm16(sample_rate_hz: int, duration_seconds: float = 0.02, frequency_hz: float = 440.0) -> bytes:
    samples = []
    sample_count = int(sample_rate_hz * duration_seconds)
    for index in range(sample_count):
        value = int(12_000 * math.sin(2 * math.pi * frequency_hz * index / sample_rate_hz))
        samples.append(value)
    return struct.pack(f"<{len(samples)}h", *samples)


def test_twilio_base64_payload_decode_and_encode() -> None:
    mu_law_audio = bytes([0xFF, 0x7F, 0x00, 0x55])
    payload = encode_twilio_payload(mu_law_audio)

    assert payload == "/38AVQ=="
    assert decode_twilio_payload(payload) == mu_law_audio


def test_mu_law_decode_and_encode_known_vector() -> None:
    mu_law_audio = bytes([0xFF, 0xE7, 0xDB, 0x00, 0x7F])
    pcm16_audio = mu_law_to_pcm16(mu_law_audio)

    assert pcm16_audio == b"\x00\x00\x04\x01\x0c\x02\x84\x82\x00\x00"
    assert pcm16_to_mu_law(pcm16_audio) == bytes([0xFF, 0xE7, 0xDB, 0x00, 0xFF])


def test_resample_pcm16_8khz_to_16khz() -> None:
    pcm8k = sine_pcm16(8_000)
    pcm16k = resample_pcm16_mono(pcm8k, 8_000, 16_000)

    assert len(pcm16k) > len(pcm8k)
    assert abs((len(pcm16k) // 2) - (2 * (len(pcm8k) // 2))) <= 1
    assert pcm16_samples(pcm16k)[0] == pcm16_samples(pcm8k)[0]


def test_resample_pcm16_16khz_to_8khz() -> None:
    pcm16k = sine_pcm16(16_000)
    pcm8k = resample_pcm16_mono(pcm16k, 16_000, 8_000)

    assert len(pcm8k) < len(pcm16k)
    assert abs((len(pcm8k) // 2) - ((len(pcm16k) // 2) // 2)) <= 1
    assert pcm16_samples(pcm8k)[0] == pcm16_samples(pcm16k)[0]


def test_twilio_payload_to_nova_pcm16_converts_ulaw_8khz_to_pcm16_16khz() -> None:
    pcm8k = sine_pcm16(8_000)
    payload = encode_twilio_payload(pcm16_to_mu_law(pcm8k))

    pcm16k = twilio_payload_to_nova_pcm16(payload)

    assert len(pcm16k) > len(pcm8k)
    assert len(pcm16k) % 2 == 0


def test_nova_pcm16_to_twilio_payload_converts_pcm16_16khz_to_ulaw_8khz_payload() -> None:
    pcm16k = sine_pcm16(16_000)

    payload = nova_pcm16_to_twilio_payload(pcm16k, source_rate_hz=16_000)
    mu_law_audio = decode_twilio_payload(payload)

    assert len(mu_law_audio) < len(pcm16k)
    assert mu_law_to_pcm16(mu_law_audio)


def test_round_trip_sanity_for_known_sample_frame() -> None:
    original_pcm8k = sine_pcm16(8_000)
    twilio_payload = encode_twilio_payload(pcm16_to_mu_law(original_pcm8k))

    nova_pcm16 = twilio_payload_to_nova_pcm16(twilio_payload)
    output_payload = nova_pcm16_to_twilio_payload(nova_pcm16, source_rate_hz=16_000)
    output_pcm8k = mu_law_to_pcm16(decode_twilio_payload(output_payload))

    assert len(output_pcm8k) == len(original_pcm8k)
    assert pcm16_rms(output_pcm8k) > 0


def test_empty_frames_are_safe_noops() -> None:
    assert decode_twilio_payload("") == b""
    assert encode_twilio_payload(b"") == ""
    assert mu_law_to_pcm16(b"") == b""
    assert pcm16_to_mu_law(b"") == b""
    assert resample_pcm16_mono(b"", 8_000, 16_000) == b""
    assert twilio_payload_to_nova_pcm16("") == b""
    assert nova_pcm16_to_twilio_payload(b"") == ""


def test_malformed_base64_payload_fails_observably() -> None:
    with pytest.raises(AudioConversionError) as exc_info:
        twilio_payload_to_nova_pcm16("not valid base64!")

    assert exc_info.value.kind == "invalid_base64_payload"


def test_odd_length_pcm16_frame_fails_observably() -> None:
    with pytest.raises(AudioConversionError) as exc_info:
        validate_pcm16(b"\x00")

    assert exc_info.value.kind == "invalid_pcm16_frame"


def test_short_pcm16_frames_are_handled_when_complete_sample() -> None:
    assert resample_pcm16_mono(b"\x00\x00", 8_000, 16_000)
