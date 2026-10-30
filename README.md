# Speech to Text — Ink & Voice

A small speech-to-text web app for the "Speech-to-Text Transcription" assignment: convert
audio recordings (and live microphone speech) into text using Python speech-recognition
libraries, wrapped in a light rice-paper and ink-painting interface.

- **API:** https://speech-to-text-api-9ovp.onrender.com (`/docs`, `/health`)
- **Frontend:** static site (`frontend/`), intended for Vercel
- **Repo:** https://github.com/Darkstrike03/SPEECH

## Features

- Upload `wav`, `mp3`, `m4a`, `ogg`, `flac`, or `webm` (up to 25 MB / 5 minutes).
- Live microphone mode: near-live finals over WebSocket, one phrase per pause.
- Engine choice in both modes:
  - **Google Web Speech** via Python's `SpeechRecognition` (default; no API key).
  - **Whisper** via `faster-whisper` tiny running on the server (offline fallback).
- Timestamps, copy, download `.txt`, language selection, level meter, session timer.
- Ink-painting UI: rice paper, grain, brush tabs, ensō mic, red seal, layered mountains.

## Architecture

```
frontend/ (HTML + CSS + vanilla JS, no build)
  pcm-worklet.js   48 kHz mic -> 16 kHz mono int16, 100 ms chunks
  app.js           tabs, upload flow, WebSocket client, results

backend/ (FastAPI, Python 3.12)
  app.py           POST /api/transcribe, WS /ws/stream, CORS, rate + concurrency limits
  transcriber.py   PyAV decode, Google Web Speech, faster-whisper, engine fallback
  streaming.py     energy VAD endpointer -> complete utterances with timestamps
  cli.py           local file transcription
  benchmark.py     HTTP latency/RTF measurement
  ws_test_client.py replay an audio file into the live endpoint
```

Upload path: multipart -> PyAV decode to 16 kHz mono -> Google (45 s chunks with 300 ms tail
padding) or Whisper -> JSON. If Google fails, the server automatically falls back to Whisper and
returns a warning.

Live path: AudioWorklet -> WebSocket -> VAD endpointer -> worker thread -> engine -> `final`
message. Interim words are not emitted; each phrase appears after the speaker pauses.

## Engines and why

The brief asks for basic Python speech libraries. `SpeechRecognition` (Google Web Speech) is the
default because it is keyless, network-bound, and fast even on free hosting. Whisper runs locally
with `faster-whisper` tiny (int8) as the fallback and as the second UI engine.

On Render's free 0.1 CPU, local Whisper measured RTF 5.6–6.3 (about 45 s for 8 s of audio), so
hosting Whisper live there is not practical. Google Web Speech measured RTF 0.28–0.33 for uploads
and 0.6–1.4 s per live phrase. A hosted Whisper provider (Groq) was prototyped and then removed:
unnecessary for the assignment and an extra key/deployment dependency.

Note: Google's free endpoint can merge words across a short pause ("tool. The" -> "to all the")
and can rate-limit cloud IPs. Switch to Whisper for exact words; the server falls back
automatically on Google failures.

## Run locally

Requires Python 3.12 (the bundled `.venv` is 3.12.7) and a Chromium-based browser for live mode.

```powershell
# API
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --app-dir backend

# Web (second terminal)
.\.venv\Scripts\python.exe -m http.server 5500 --directory frontend
```

Open http://127.0.0.1:5500. Local `config.js` automatically targets `http://127.0.0.1:8000`.

Optional checks:

```powershell
.\.venv\Scripts\python.exe backend\cli.py samples\speech-test.wav --engine google
.\.venv\Scripts\python.exe backend\benchmark.py samples\speech-test.mp3 --engine google
.\.venv\Scripts\python.exe backend\ws_test_client.py samples\speech-test.wav --realtime --engine google
```

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | status, default engine, model, limits |
| `GET` | `/docs` | interactive OpenAPI docs |
| `POST` | `/api/transcribe` | multipart `file`, `engine=google|whisper`, `language` |
| `WS` | `/ws/stream` | live PCM streaming |

WebSocket contract: send `{"type":"start","language":"en","engine":"google"}`, then raw
little-endian int16 PCM (16 kHz mono), optional `ping`, and `stop`. The server replies with
`ready`, `vad`, `processing`, `final`, `empty`, `warning`, `error`, `pong`, and `done`.

Limits: 25 MB upload, 5 minutes of audio, 5-minute live sessions, 2 concurrent streams per IP,
15 uploads per 10 minutes per IP.

## Deploy

Backend: Render blueprint (`render.yaml`) builds `backend/`, prefetches the Whisper tiny model,
and serves `uvicorn` on `$PORT` with `/health`. No secrets are required.

Frontend: deploy `frontend/` as a static site (Vercel, no build step). `config.js` already points
non-local hosts at the Render API; the backend allows `https://*.vercel.app` origins.

## Project layout

```
backend/    FastAPI service and ASR pipeline
frontend/   static UI (no bundler)
samples/    local test audio (gitignored)
PROJECT_PHASES.md   decisions, protocol, measurements, risks
render.yaml         Render blueprint
```
