"""FastAPI service that turns uploaded audio into text."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock

import numpy as np
from fastapi import (
    FastAPI,
    File,
    Form,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from streaming import StreamEndpointer, Utterance, VadEvent
from transcriber import (
    MAX_AUDIO_SECONDS,
    SAMPLE_RATE,
    WHISPER_MODEL_SIZE,
    TranscriptionError,
    transcribe,
    transcribe_pcm,
)

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "15"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "600"))
MAX_STREAM_SECONDS = int(os.getenv("MAX_STREAM_SECONDS", "300"))
MAX_CONCURRENT_STREAMS = int(os.getenv("MAX_CONCURRENT_STREAMS", "2"))

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

_stream_counts: dict[str, int] = {}
_stream_lock = asyncio.Lock()


async def _acquire_stream(client_ip: str) -> bool:
    async with _stream_lock:
        current = _stream_counts.get(client_ip, 0)
        if current >= MAX_CONCURRENT_STREAMS:
            return False
        _stream_counts[client_ip] = current + 1
        return True


async def _release_stream(client_ip: str) -> None:
    async with _stream_lock:
        current = _stream_counts.get(client_ip, 1) - 1
        if current <= 0:
            _stream_counts.pop(client_ip, None)
        else:
            _stream_counts[client_ip] = current


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
        "upload": "POST /api/transcribe",
        "stream": "WS /ws/stream",
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "whisper_model": WHISPER_MODEL_SIZE,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_audio_seconds": MAX_AUDIO_SECONDS,
        "max_stream_seconds": MAX_STREAM_SECONDS,
        "streaming": True,
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


MAX_STREAM_QUEUE = 8


@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    client_ip = websocket.client.host if websocket.client else "unknown"

    if not await _acquire_stream(client_ip):
        await websocket.send_json(
            {
                "type": "error",
                "code": "too_many_streams",
                "message": "Only two live sessions at a time, please close another tab.",
            }
        )
        await websocket.close(code=1013)
        return

    send_lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        try:
            async with send_lock:
                await websocket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            pass

    worker: asyncio.Task | None = None
    try:
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
            config = json.loads(raw)
        except (asyncio.TimeoutError, ValueError):
            await send({"type": "error", "code": "expected_start", "message": "Send a start message first."})
            await websocket.close(code=1002)
            return

        if not isinstance(config, dict) or config.get("type") != "start":
            await send({"type": "error", "code": "expected_start", "message": "Send a start message first."})
            await websocket.close(code=1002)
            return

        language = str(config.get("language") or "auto")
        endpointer = StreamEndpointer()
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_STREAM_QUEUE)
        counter = itertools.count(1)
        total_samples = 0
        dropped = 0

        async def transcribe_worker() -> None:
            while True:
                item = await queue.get()
                try:
                    await send({"type": "processing", "seq": item["seq"], "start": item["start"]})
                    started = time.perf_counter()
                    result = await asyncio.to_thread(transcribe_pcm, item["pcm"], language)
                    text = result.text.strip()
                    if text:
                        await send(
                            {
                                "type": "final",
                                "seq": item["seq"],
                                "text": text,
                                "start": item["start"],
                                "end": item["end"],
                                "language": result.language,
                                "audio_seconds": round(len(item["pcm"]) / SAMPLE_RATE, 2),
                                "elapsed": round(time.perf_counter() - started, 2),
                            }
                        )
                    else:
                        await send({"type": "empty", "seq": item["seq"], "start": item["start"]})
                except TranscriptionError as exc:
                    await send({"type": "error", "code": exc.code, "message": exc.message})
                except Exception:
                    await send(
                        {
                            "type": "error",
                            "code": "engine_failed",
                            "message": "Whisper failed on this audio segment.",
                        }
                    )
                finally:
                    queue.task_done()

        worker = asyncio.create_task(transcribe_worker())

        await send(
            {
                "type": "ready",
                "sample_rate": SAMPLE_RATE,
                "frame_ms": 20,
                "max_seconds": MAX_STREAM_SECONDS,
                "model": WHISPER_MODEL_SIZE,
            }
        )

        def enqueue(event: Utterance) -> None:
            nonlocal dropped
            item = {
                "seq": next(counter),
                "pcm": event.pcm,
                "start": event.start,
                "end": event.end,
            }
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                dropped += 1
                asyncio.create_task(
                    send(
                        {
                            "type": "warning",
                            "code": "backlog",
                            "message": "Server is behind; a segment was skipped.",
                        }
                    )
                )

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                samples = np.frombuffer(message["bytes"], dtype="<i2")
                total_samples += samples.size
                if total_samples > MAX_STREAM_SECONDS * SAMPLE_RATE:
                    await send(
                        {
                            "type": "error",
                            "code": "too_long",
                            "message": f"Live sessions are limited to {MAX_STREAM_SECONDS // 60} minutes.",
                        }
                    )
                    break
                for event in endpointer.push(samples):
                    if isinstance(event, VadEvent):
                        await send({"type": "vad", "speaking": event.speaking})
                    else:
                        enqueue(event)
                continue

            text = message.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue

            kind = payload.get("type")
            if kind == "ping":
                await send({"type": "pong"})
            elif kind == "stop":
                for event in endpointer.flush():
                    if isinstance(event, Utterance):
                        enqueue(event)
                await queue.join()
                await send({"type": "done", "dropped": dropped})
                break
    except WebSocketDisconnect:
        pass
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await _release_stream(client_ip)
        try:
            await websocket.close()
        except RuntimeError:
            pass
