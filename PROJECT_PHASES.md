# Project Phases — Speech-to-Text

Living plan. Update status as work lands. Source of truth for decisions.

## Snapshot

| Item | Value |
| --- | --- |
| Repo | https://github.com/Darkstrike03/SPEECH |
| Backend | https://speech-to-text-api-9ovp.onrender.com (Render free, 512 MB / 0.1 CPU, sleeps after 15 min) |
| Frontend | Vercel — not deployed yet (pending Phase 2/3) |
| Local API | http://127.0.0.1:8000 |
| Local web | http://127.0.0.1:5500 (`python -m http.server 5500 --directory frontend`) |
| Python | 3.12.7 inside `.venv` — always use `.venv\Scripts\python.exe` (Inkscape ships its own Python) |
| Model | faster-whisper `tiny`, int8, cached in `backend/models` at build time |

## Locked decisions

- Live mode = **server-side streaming Whisper** over WebSocket. No browser Web Speech fallback.
- Interim words **off** — finals only, emitted when the VAD closes an utterance.
- UI = **light rice-paper ink-painting** theme, **English text only** (calligraphy is visual, not textual).
- Any-format uploads decoded with PyAV; no system ffmpeg.
- Google Web Speech stays as an optional **upload** engine (rate-limits from datacenter IPs).
- Limits: 25 MB upload, 5 min audio, 5 min live session, 2 concurrent live streams per IP.

## Phases

| # | Phase | Status |
| --- | --- | --- |
| 0 | Render latency spike: deploy upload API, measure RTF | **in progress** |
| 1 | Streaming backend: VAD endpointer + `/ws/stream` | code written, local test pending |
| 2 | Ink-painting UI rewrite + live mic client | not started |
| 3 | Deploy frontend to Vercel, README, final smoke test | not started |

### Phase 0 — checklist
- [x] `render.yaml` + `backend/.python-version` committed (`cee58a7`)
- [x] Repo initialized and pushed to `main`
- [x] Render service created by owner
- [ ] Measure real RTF against Render via `benchmark.py`
- [ ] Measure streaming latency via `ws_test_client.py --realtime`
- [ ] Record numbers below and tune `MAX_UTTERANCE_MS` if needed

### Phase 1 — checklist
- [x] `backend/streaming.py` — energy VAD, pre-roll/hangover/force-cut, absolute timestamps
- [x] `backend/transcriber.py` — `transcribe_pcm()` reuses warm model
- [x] `backend/app.py` — `/ws/stream`, worker queue, concurrency cap, session cap, keepalive
- [x] `backend/ws_test_client.py` — replay tool
- [ ] Local replay passes
- [ ] Commit + push, Render redeploys
- [ ] Render replay passes

### Phase 2 — checklist
- [ ] `frontend/pcm-worklet.js` — 48k→16k int16 frames, low-pass + linear resample
- [ ] `index.html` + `styles.css` rewritten: rice paper, turbulence grain, ink mountains, ensō mic, STT seal, brush tabs
- [ ] `app.js`: Upload / Live tabs, WS client w/ reconnect + cold-start state, level meter, timer, status, finals transcript
- [ ] Copy/download/timestamps preserved; a11y + reduced-motion pass

### Phase 3 — checklist
- [ ] Vercel deploy of `frontend/` (static, no build)
- [ ] `config.js` `API_BASE` → Render URL
- [ ] CORS check from deployed origin
- [ ] README with architecture, protocol, measured latency, limits
- [ ] Public-URL smoke test (upload + live)

## Architecture

```
upload:  browser --multipart--> FastAPI /api/transcribe -> PyAV decode -> whisper/google
live:    mic -> AudioWorklet (16k int16, 100ms) -> WS /ws/stream -> VAD endpointer
              -> utterance -> asyncio.to_thread(whisper, warm model) -> JSON final
```

## WebSocket protocol (`/ws/stream`)

Client → server
- first message (JSON, within 15 s): `{"type":"start","language":"auto|en|hi|..."}`
- binary: raw little-endian int16 PCM, mono, 16 kHz, any chunk size (100 ms recommended)
- `{"type":"ping"}` keepalive (send every 20 s)
- `{"type":"stop"}` — flushes the open utterance, drains the queue

Server → client
- `ready`: sample_rate, frame_ms, max_seconds, model
- `vad`: `{speaking: bool}`
- `processing`: `{seq, start}` — utterance accepted
- `final`: `{seq, text, start, end, language, audio_seconds, elapsed}`
- `empty`: `{seq, start}` — VAD segment had no speech
- `warning`: backlog / skipped segment
- `error`: `{code, message}` → `too_many_streams`, `too_long`, `expected_start`, engine failures
- `pong`, `done`: `{dropped}`

## Measured performance

| Where | Audio | Wall | RTF | Notes |
| --- | --- | --- | --- | --- |
| Local (desktop) | 8.0 s | 0.84 s | 0.11 | warm model, HTTP upload |
| Local (desktop) | 8.0 s | 1.23 s | 0.16 | first warm call |
| Render free | — | — | — | **pending** |

Interpretation: RTF < 0.5 smooth, 0.5–1 demoable, > 1 tune/reduce utterance size.

## Env vars (backend)

`WHISPER_MODEL_SIZE=tiny`, `WHISPER_MODEL_DIR=models`, `WHISPER_VAD=1` (uploads only),
`MAX_UPLOAD_MB=25`, `MAX_AUDIO_SECONDS=300`, `MAX_STREAM_SECONDS=300`,
`MAX_CONCURRENT_STREAMS=2`, `RATE_LIMIT_REQUESTS=15`, `RATE_LIMIT_WINDOW_SECONDS=600`,
`ALLOWED_ORIGINS=...`

## Open risks

- Render 0.1 CPU may make streaming feel laggy; decide after Phase 0 numbers.
- Free instance sleeps; first live connect can take ~60 s (UI must show "waking server").
- 512 MB RAM is fine for tiny int8, but only one model instance must ever exist (module singleton + lock).
- OneDrive-backed working copy can confuse git with CRLF warnings; committed as LF.
