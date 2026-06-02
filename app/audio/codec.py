import base64
import binascii
import warnings
from dataclasses import dataclass
from typing import Literal

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning, message="'audioop' is deprecated.*")
    import audioop


TWILIO_SAMPLE_RATE_HZ = 8_000
NOVA_SAMPLE_RATE_HZ = 16_000
NOVA_OUTPUT_SAMPLE_RATE_HZ = 24_000
PCM16_SAMPLE_WIDTH_BYTES = 2

AudioErrorKind = Literal["invalid_base64_payload", "invalid_pcm16_frame"]


@dataclass(frozen=True)
class AudioConversionError(ValueError):
    kind: AudioErrorKind
    message: str

    def __str__(self) -> str:
        return self.message


def decode_twilio_payload(payload: str) -> bytes:
    if not payload:
        return b""
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AudioConversionError("invalid_base64_payload", "Twilio media payload is not valid base64") from exc


def encode_twilio_payload(mu_law_audio: bytes) -> str:
    if not mu_law_audio:
        return ""
    return base64.b64encode(mu_law_audio).decode("ascii")


def mu_law_to_pcm16(mu_law_audio: bytes) -> bytes:
    if not mu_law_audio:
        return b""
    return audioop.ulaw2lin(mu_law_audio, PCM16_SAMPLE_WIDTH_BYTES)


def pcm16_to_mu_law(pcm16_audio: bytes) -> bytes:
    validate_pcm16(pcm16_audio)
    if not pcm16_audio:
        return b""
    return audioop.lin2ulaw(pcm16_audio, PCM16_SAMPLE_WIDTH_BYTES)


def resample_pcm16_mono(pcm16_audio: bytes, source_rate_hz: int, target_rate_hz: int) -> bytes:
    validate_pcm16(pcm16_audio)
    if not pcm16_audio or source_rate_hz == target_rate_hz:
        return pcm16_audio
    converted, _state = audioop.ratecv(
        pcm16_audio,
        PCM16_SAMPLE_WIDTH_BYTES,
        1,
        source_rate_hz,
        target_rate_hz,
        None,
    )
    return converted


def twilio_payload_to_nova_pcm16(payload: str) -> bytes:
    mu_law_audio = decode_twilio_payload(payload)
    pcm8k = mu_law_to_pcm16(mu_law_audio)
    return resample_pcm16_mono(pcm8k, TWILIO_SAMPLE_RATE_HZ, NOVA_SAMPLE_RATE_HZ)


def nova_pcm16_to_twilio_payload(pcm16_audio: bytes, source_rate_hz: int = NOVA_OUTPUT_SAMPLE_RATE_HZ) -> str:
    pcm8k = resample_pcm16_mono(pcm16_audio, source_rate_hz, TWILIO_SAMPLE_RATE_HZ)
    mu_law_audio = pcm16_to_mu_law(pcm8k)
    return encode_twilio_payload(mu_law_audio)


def validate_pcm16(pcm16_audio: bytes) -> None:
    if len(pcm16_audio) % PCM16_SAMPLE_WIDTH_BYTES:
        raise AudioConversionError("invalid_pcm16_frame", "PCM16 audio must contain whole 16-bit samples")
