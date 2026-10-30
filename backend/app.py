"""FastAPI service that turns uploaded audio into text."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
from collections import deque
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
    DEFAULT_ENGINE,
    ENGINES,
    MAX_AUDIO_SECONDS,
    SAMPLE_RATE,
    WHISPER_MODEL_SIZE,
    TranscriptionError,
    transcribe,
    transcribe_pcm,
)

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "30"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "600"))
MAX_STREAM_SECONDS = int(os.getenv("MAX_STREAM_SECONDS", "300"))
MAX_LIVE_QUEUE = int(os.getenv("MAX_LIVE_QUEUE", "5"))
LIVE_QUEUE_TIMEOUT = int(os.getenv("LIVE_QUEUE_TIMEOUT_SECONDS", "120"))
STREAM_IDLE_TIMEOUT = int(os.getenv("STREAM_IDLE_TIMEOUT_SECONDS", "90"))

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
    description="Upload audio or stream a microphone into Google Web Speech or on-server Whisper.",
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

_hits: deque[float] = deque()
_hits_lock = Lock()

_stream_cond = asyncio.Condition()
_stream_active = False
_stream_waiting = 0


async def _acquire_stream(on_queued=None) -> tuple[str, int]:
    """One live session at a time; wait in a small FIFO queue for a free slot."""
    global _stream_active, _stream_waiting

    async with _stream_cond:
        if not _stream_active:
            _stream_active = True
            return "active", 0

        if _stream_waiting >= MAX_LIVE_QUEUE:
            return "full", 0

        _stream_waiting += 1
        position = _stream_waiting
        if on_queued is not None:
            await on_queued(position)
        try:
            await asyncio.wait_for(
                _stream_cond.wait_for(lambda: not _stream_active),
                timeout=LIVE_QUEUE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return "timeout", position
        finally:
            _stream_waiting -= 1

        _stream_active = True
        return "active", position


async def _release_stream() -> None:
    global _stream_active

    async with _stream_cond:
        _stream_active = False
        _stream_cond.notify_all()


def _rate_limited() -> bool:
    now = time.monotonic()
    with _hits_lock:
        while _hits and now - _hits[0] > RATE_LIMIT_WINDOW:
            _hits.popleft()
        if len(_hits) >= RATE_LIMIT_REQUESTS:
            return True
        _hits.append(now)
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
        "engine": DEFAULT_ENGINE,
        "engines": list(ENGINES),
        "whisper_model": WHISPER_MODEL_SIZE,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_audio_seconds": MAX_AUDIO_SECONDS,
        "max_stream_seconds": MAX_STREAM_SECONDS,
        "live_queue": MAX_LIVE_QUEUE,
        "streaming": True,
    }


@app.post("/api/transcribe")
def api_transcribe(
    request: Request,
    file: UploadFile = File(...),
    engine: str = Form(DEFAULT_ENGINE),
    language: str = Form("auto"),
):
    if _rate_limited():
        return _error(
            "Too many requests. Please wait a few minutes and try again.",
            "rate_limited",
        )

    if engine not in ENGINES:
        return _error("engine must be 'google' or 'whisper'", "bad_engine")

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
        "provider": result.provider,
    }


MAX_STREAM_QUEUE = 8


@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        try:
            async with send_lock:
                await websocket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            pass

    async def announce_queue(position: int) -> None:
        await send(
            {
                "type": "queued",
                "position": position,
                "max_wait_seconds": LIVE_QUEUE_TIMEOUT,
            }
        )

    slot, position = await _acquire_stream(announce_queue)

    if slot == "full":
        await send(
            {
                "type": "error",
                "code": "stream_full",
                "message": f"Live queue is full ({MAX_LIVE_QUEUE} waiting). Try again in a minute.",
            }
        )
        await websocket.close(code=1013)
        return

    if slot == "timeout":
        await send(
            {
                "type": "error",
                "code": "live_queue_timeout",
                "message": "No live slot opened up in time. Tap the mic to try again.",
            }
        )
        await websocket.close(code=1013)
        return

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
        engine = str(config.get("engine") or DEFAULT_ENGINE).lower()
        if engine not in ENGINES:
            engine = DEFAULT_ENGINE
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
                    result = await asyncio.to_thread(
                        transcribe_pcm, item["pcm"], language, engine
                    )
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
                                "engine": result.engine,
                                "warnings": result.warnings,
                                "audio_seconds": round(len(item["pcm"]) / SAMPLE_RATE, 2),
                                "elapsed": round(time.perf_counter() - started, 2),
                                "provider": result.provider,
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
                            "message": "Speech recognition failed on this audio segment.",
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
                "engine": engine,
                "whisper_model": WHISPER_MODEL_SIZE,
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
            message = await asyncio.wait_for(
                websocket.receive(), timeout=STREAM_IDLE_TIMEOUT
            )
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
    except asyncio.TimeoutError:
        await send(
            {
                "type": "error",
                "code": "idle_timeout",
                "message": f"Live session closed after {STREAM_IDLE_TIMEOUT} seconds without audio.",
            }
        )
    except WebSocketDisconnect:
        pass
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await _release_stream()
        try:
            await websocket.close()
        except RuntimeError:
            pass
