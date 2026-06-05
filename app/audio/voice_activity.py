import math
import struct

from app.audio.codec import validate_pcm16


def pcm16_rms_amplitude(pcm16_audio: bytes) -> float:
    validate_pcm16(pcm16_audio)
    sample_count = len(pcm16_audio) // 2
    if sample_count == 0:
        return 0.0

    total_squares = 0
    for (sample,) in struct.iter_unpack("<h", pcm16_audio):
        total_squares += sample * sample
    return math.sqrt(total_squares / sample_count)


def has_voice_activity(pcm16_audio: bytes, *, rms_threshold: float) -> bool:
    if rms_threshold <= 0:
        return bool(pcm16_audio)
    return pcm16_rms_amplitude(pcm16_audio) >= rms_threshold
