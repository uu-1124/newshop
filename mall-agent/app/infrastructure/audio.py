"""Bounded PCM WAV validation; client MIME and claimed duration are never trusted."""

import io
import math
import struct
import wave

from app.domain.identity import DomainError

MAX_AUDIO_BYTES = 5 * 1024 * 1024


def wav_info(raw):
    if not raw or len(raw) > MAX_AUDIO_BYTES:
        raise DomainError("ATTACHMENT_TOO_LARGE", "录音大小不能超过5MB", 413)
    try:
        with wave.open(io.BytesIO(raw), "rb") as audio:
            channels, width, rate, frames = (
                audio.getnchannels(),
                audio.getsampwidth(),
                audio.getframerate(),
                audio.getnframes(),
            )
            if (
                channels not in (1, 2)
                or width != 2
                or rate not in (8000, 16000, 22050, 24000, 44100, 48000)
                or not 0 < frames <= rate * 60
            ):
                raise ValueError()
            if len(audio.readframes(frames)) != frames * channels * width:
                raise ValueError()
            return max(1, math.ceil(frames * 1000 / rate))
    except (ValueError, wave.Error, EOFError):
        raise DomainError("INVALID_AUDIO", "请使用60秒以内的PCM WAV录音", 422) from None


def normalize_supplier_wav(raw):
    """DashScope can return a streaming WAV header whose lengths are placeholders.

    Only this trusted provider path accepts that header. Uploaded recordings keep
    strict lengths; actual PCM bytes, format and duration are still validated.
    """
    if len(raw) >= 44 and raw[:4] == b"RIFF" and raw[8:16] == b"WAVEfmt " and raw[36:40] == b"data":
        riff, size = struct.unpack_from("<I", raw, 4)[0], struct.unpack_from("<I", raw, 40)[0]
        if size > 0x7FFF0000 and riff == size + 36 and len(raw) <= MAX_AUDIO_BYTES:
            actual = len(raw) - 44
            if actual % 2:
                raise DomainError("INVALID_AUDIO", "合成音频帧不完整", 503)
            corrected = bytearray(raw)
            struct.pack_into("<I", corrected, 4, len(raw) - 8)
            struct.pack_into("<I", corrected, 40, actual)
            raw = bytes(corrected)
    wav_info(raw)
    return raw
