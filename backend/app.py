"""FastAPI service that turns uploaded audio into text."""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from transcriber import (
    MAX_AUDIO_SECONDS,
    WHISPER_MODEL_SIZE,
    TranscriptionError,
    transcribe,
)

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "15"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "600"))

DEFAULT_ORIGINS = "http://localhost:5500,http://127.0.0.1:5500,http://localhost:8000"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
    if origin.strip()
]

STATUS_BY_CODE = {
    "empty_file": 400,
    "no_audio_track": 415,
    "no_audio_frames": 415,
    "decode_failed": 415,
    "too_short": 400,
    "too_long": 413,
    "bad_engine": 400,
    "no_speech": 422,
    "engine_unavailable": 502,
    "engine_failed": 502,
    "model_unavailable": 503,
    "rate_limited": 429,
    "file_too_large": 413,
}

app = FastAPI(
    title="Speech-to-Text API",
    description="Upload audio in any common format and get a transcript back.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://([a-z0-9-]+\.)*vercel\.app",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=86400,
)

_hits: dict[str, deque[float]] = defaultdict(deque)
_hits_lock = Lock()


def _rate_limited(client_ip: str) -> bool:
    now = time.monotonic()
    with _hits_lock:
        window = _hits[client_ip]
        while window and now - window[0] > RATE_LIMIT_WINDOW:
            window.popleft()
        if len(window) >= RATE_LIMIT_REQUESTS:
            return True
        window.append(now)
        return False


def _error(message: str, code: str, status_code: int | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code or STATUS_BY_CODE.get(code, 500),
        content={"error": {"code": code, "message": message}},
    )


@app.get("/")
def root() -> dict:
    return {
        "name": "Speech-to-Text API",
        "docs": "/docs",
        "health": "/health",
        "endpoint": "POST /api/transcribe",
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "whisper_model": WHISPER_MODEL_SIZE,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_audio_seconds": MAX_AUDIO_SECONDS,
    }


@app.post("/api/transcribe")
def api_transcribe(
    request: Request,
    file: UploadFile = File(...),
    engine: str = Form("whisper"),
    language: str = Form("auto"),
):
    client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip):
        return _error(
            "Too many requests. Please wait a few minutes and try again.",
            "rate_limited",
        )

    if engine not in ("whisper", "google"):
        return _error("engine must be 'whisper' or 'google'", "bad_engine")

    declared = request.headers.get("content-length")
    if declared and int(declared) > MAX_UPLOAD_BYTES + 64 * 1024:
        return _error(
            f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            "file_too_large",
        )

    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return _error(
            f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            "file_too_large",
        )
    if not data:
        return _error("The uploaded file is empty.", "empty_file")

    started = time.perf_counter()
    try:
        result = transcribe(data, engine=engine, language=language)
    except TranscriptionError as exc:
        return _error(exc.message, exc.code)
    except Exception:
        return _error(
            "Something went wrong while transcribing this file.",
            "internal_error",
            500,
        )

    return {
        "text": result.text,
        "engine": result.engine,
        "language": result.language,
        "duration": round(result.duration, 2),
        "segments": result.segments,
        "warnings": result.warnings,
        "elapsed": round(time.perf_counter() - started, 2),
        "filename": Path(file.filename or "audio").name,
    }
