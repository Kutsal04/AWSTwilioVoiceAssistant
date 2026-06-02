"""Audio codec and resampling helpers."""

from app.audio.codec import (
    NOVA_SAMPLE_RATE_HZ,
    NOVA_OUTPUT_SAMPLE_RATE_HZ,
    PCM16_SAMPLE_WIDTH_BYTES,
    TWILIO_SAMPLE_RATE_HZ,
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

__all__ = [
    "NOVA_SAMPLE_RATE_HZ",
    "NOVA_OUTPUT_SAMPLE_RATE_HZ",
    "PCM16_SAMPLE_WIDTH_BYTES",
    "TWILIO_SAMPLE_RATE_HZ",
    "AudioConversionError",
    "decode_twilio_payload",
    "encode_twilio_payload",
    "mu_law_to_pcm16",
    "nova_pcm16_to_twilio_payload",
    "pcm16_to_mu_law",
    "resample_pcm16_mono",
    "twilio_payload_to_nova_pcm16",
    "validate_pcm16",
]
