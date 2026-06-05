import pytest

from app.audio import AudioConversionError, has_voice_activity, pcm16_rms_amplitude


def test_pcm16_rms_amplitude_returns_zero_for_empty_audio() -> None:
    assert pcm16_rms_amplitude(b"") == 0.0


def test_pcm16_rms_amplitude_measures_sample_energy() -> None:
    pcm16_audio = (1000).to_bytes(2, "little", signed=True) + (-1000).to_bytes(2, "little", signed=True)

    assert pcm16_rms_amplitude(pcm16_audio) == 1000.0


def test_has_voice_activity_uses_rms_threshold() -> None:
    quiet = (100).to_bytes(2, "little", signed=True) * 160
    speech_like = (2000).to_bytes(2, "little", signed=True) * 160

    assert has_voice_activity(quiet, rms_threshold=500.0) is False
    assert has_voice_activity(speech_like, rms_threshold=500.0) is True


def test_voice_activity_rejects_invalid_pcm16_frames() -> None:
    with pytest.raises(AudioConversionError):
        pcm16_rms_amplitude(b"\x00")
