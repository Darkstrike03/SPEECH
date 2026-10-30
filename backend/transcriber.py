"""Core speech-to-text logic shared by the CLI and the FastAPI app."""

from __future__ import annotations

import io
import os
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import av
import numpy as np
import speech_recognition as sr

SAMPLE_RATE = 16_000
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "300"))
GOOGLE_CHUNK_SECONDS = 45

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "tiny")
WHISPER_MODEL_DIR = Path(os.getenv("WHISPER_MODEL_DIR", "models"))
WHISPER_VAD = os.getenv("WHISPER_VAD", "1") == "1"

GOOGLE_LANGUAGES = {
    "en": "en-US",
    "hi": "hi-IN",
    "bn": "bn-IN",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "zh": "zh-CN",
    "ar": "ar-SA",
    "pt": "pt-BR",
    "ru": "ru-RU",
    "ta": "ta-IN",
    "te": "te-IN",
    "mr": "mr-IN",
    "gu": "gu-IN",
    "ur": "ur-PK",
}

_whisper_model = None
_whisper_lock = threading.Lock()


class TranscriptionError(Exception):
    def __init__(self, message: str, code: str = "transcription_failed"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class TranscriptionResult:
    text: str
    engine: str
    language: str
    duration: float
    segments: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def decode_audio(data: bytes) -> np.ndarray:
    """Decode any container/codec PyAV supports into 16 kHz mono int16 PCM."""
    if not data:
        raise TranscriptionError("The uploaded file is empty.", "empty_file")

    try:
        with av.open(io.BytesIO(data)) as container:
            if not container.streams.audio:
                raise TranscriptionError(
                    "No audio track found in this file.", "no_audio_track"
                )
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            chunks: list[np.ndarray] = []
            for frame in container.decode(stream):
                for resampled in resampler.resample(frame):
                    chunks.append(resampled.to_ndarray().reshape(-1))
            for resampled in resampler.resample(None):
                chunks.append(resampled.to_ndarray().reshape(-1))
    except TranscriptionError:
        raise
    except Exception as exc:  # PyAV raises a wide variety of errors
        raise TranscriptionError(
            "Could not decode this file. Supported: wav, mp3, m4a, ogg, flac, webm.",
            "decode_failed",
        ) from exc

    if not chunks:
        raise TranscriptionError(
            "The file contains no decodable audio.", "no_audio_frames"
        )

    pcm = np.concatenate(chunks).astype(np.int16, copy=False)
    duration = len(pcm) / SAMPLE_RATE
    if duration < 0.2:
        raise TranscriptionError("The audio is too short to transcribe.", "too_short")
    if duration > MAX_AUDIO_SECONDS:
        raise TranscriptionError(
            f"Audio is {duration / 60:.1f} minutes long; the limit is "
            f"{MAX_AUDIO_SECONDS // 60} minutes.",
            "too_long",
        )
    return pcm


def _pcm_to_wav_bytes(pcm: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _iter_chunks(pcm: np.ndarray, seconds: int) -> Iterable[np.ndarray]:
    step = seconds * SAMPLE_RATE
    for start in range(0, len(pcm), step):
        yield pcm[start : start + step]


def _normalise_google_language(language: str) -> str:
    if not language or language == "auto":
        return "en-US"
    return GOOGLE_LANGUAGES.get(language, language)


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel

                WHISPER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
                try:
                    _whisper_model = WhisperModel(
                        WHISPER_MODEL_SIZE,
                        device="cpu",
                        compute_type="int8",
                        download_root=str(WHISPER_MODEL_DIR),
                    )
                except Exception as exc:
                    raise TranscriptionError(
                        "Whisper model could not be loaded. If this is the first "
                        "run, check the network connection for the model download.",
                        "model_unavailable",
                    ) from exc
    return _whisper_model


def _transcribe_whisper(
    pcm: np.ndarray, language: str, vad_filter: bool | None = None
) -> TranscriptionResult:
    model = get_whisper_model()
    audio = pcm.astype(np.float32) / 32768.0
    lang = None if language in ("", "auto") else language
    use_vad = WHISPER_VAD if vad_filter is None else vad_filter

    try:
        with _whisper_lock:
            segments_iter, info = model.transcribe(
                audio,
                language=lang,
                vad_filter=use_vad,
                beam_size=1,
                condition_on_previous_text=False,
            )
            segments = list(segments_iter)
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError(
            "Whisper failed while transcribing this audio.", "engine_failed"
        ) from exc

    segments_out = [
        {
            "start": round(segment.start, 2),
            "end": round(segment.end, 2),
            "text": segment.text.strip(),
        }
        for segment in segments
        if segment.text.strip()
    ]
    text = " ".join(segment["text"] for segment in segments_out).strip()

    return TranscriptionResult(
        text=text,
        engine="whisper",
        language=info.language or (language or "unknown"),
        duration=len(pcm) / SAMPLE_RATE,
        segments=segments_out,
        warnings=[],
    )


def _transcribe_google(pcm: np.ndarray, language: str) -> TranscriptionResult:
    recognizer = sr.Recognizer()
    recognizer.operation_timeout = 60
    google_language = _normalise_google_language(language)

    parts: list[str] = []
    warnings: list[str] = []
    for index, chunk in enumerate(_iter_chunks(pcm, GOOGLE_CHUNK_SECONDS)):
        audio_data = sr.AudioData(chunk.tobytes(), SAMPLE_RATE, 2)
        try:
            parts.append(recognizer.recognize_google(audio_data, language=google_language))
        except sr.UnknownValueError:
            warnings.append(f"Chunk {index + 1} had no recognisable speech.")
        except sr.RequestError as exc:
            message = f"Google Web Speech request failed: {exc}"
            if not parts:
                raise TranscriptionError(
                    message + " The endpoint is rate-limited on many hosts; try Whisper.",
                    "engine_unavailable",
                ) from exc
            warnings.append(f"Chunk {index + 1} failed: {exc}")
        except Exception as exc:
            raise TranscriptionError(
                "Google Web Speech failed while transcribing this audio.",
                "engine_failed",
            ) from exc

    text = " ".join(part.strip() for part in parts if part.strip()).strip()
    if not text:
        raise TranscriptionError(
            "No speech could be recognised in this audio.", "no_speech"
        )

    return TranscriptionResult(
        text=text,
        engine="google",
        language=google_language,
        duration=len(pcm) / SAMPLE_RATE,
        segments=[],
        warnings=warnings,
    )


def transcribe(
    data: bytes, engine: str = "whisper", language: str = "auto"
) -> TranscriptionResult:
    engine = (engine or "whisper").lower()
    if engine not in ("whisper", "google"):
        raise TranscriptionError(
            f"Unknown engine '{engine}'. Choose 'whisper' or 'google'.", "bad_engine"
        )

    pcm = decode_audio(data)
    if engine == "google":
        return _transcribe_google(pcm, language)
    return _transcribe_whisper(pcm, language)


def transcribe_pcm(pcm: np.ndarray, language: str = "auto") -> TranscriptionResult:
    """Transcribe 16 kHz mono int16 PCM that the caller already segmented."""
    if pcm.size == 0:
        raise TranscriptionError("No audio to transcribe.", "no_audio_frames")
    return _transcribe_whisper(pcm, language, vad_filter=False)
