# Project Phases — Speech-to-Text

Living plan. Update status as work lands. Source of truth for decisions.

## Snapshot

| Item | Value |
| --- | --- |
| Repo | https://github.com/Darkstrike03/SPEECH |
| Backend | https://speech-to-text-api-9ovp.onrender.com (Render free, 512 MB / 0.1 CPU, sleeps after 15 min) |
| Frontend | Vercel — not deployed yet (pending Phase 3) |
| Local API | http://127.0.0.1:8000 |
| Local web | http://127.0.0.1:5500 (`python -m http.server 5500 --directory frontend`) |
| Python | 3.12.7 inside `.venv` — always use `.venv\Scripts\python.exe` (Inkscape ships its own Python) |
| Engines | `SpeechRecognition` → Google Web Speech (default) + local `faster-whisper` tiny fallback |

## Locked decisions

- Live mode = **server-side streaming** over WebSocket. No browser Web Speech fallback.
- Interim words **off** — finals only, emitted when the VAD closes an utterance.
- Default engine = **Google Web Speech via `SpeechRecognition`** (the library named in the brief):
  network-bound, keyless, fast on Render's free CPU.
- Local **faster-whisper tiny** is the fallback and the second UI engine (works offline; slow on Render).
- No hosted API key anywhere; Groq was evaluated and removed (see Phase 0 finding).
- UI = **light rice-paper ink-painting** theme, **English text only** (calligraphy is visual, not textual).
- Any-format uploads decoded with PyAV; no system ffmpeg.
- Limits: 25 MB upload, 5 min audio, 5 min live session, 2 concurrent live streams per IP.

## Phases

| # | Phase | Status |
| --- | --- | --- |
| 0 | Render latency spike + engine decision | **done** — local Whisper too slow on Render; Google chosen |
| 1 | Streaming backend: VAD endpointer + `/ws/stream` | **done** — local replay + Render verified |
| 2 | Ink-painting UI rewrite + live mic client | **done** — browser E2E passes (Google + Whisper) |
| 3 | Vercel deploy, README, final smoke test | **in progress** |

### Phase 0 — checklist
- [x] `render.yaml` + `backend/.python-version` committed (`cee58a7`)
- [x] Repo initialized and pushed to `main`
- [x] Render service created by owner
- [x] Measure real RTF against Render via `benchmark.py`
- [x] Measure streaming latency via `ws_test_client.py --realtime`
- [x] Record numbers below; keep local Whisper as fallback only

### Phase 1 — checklist
- [x] `backend/streaming.py` — energy VAD, pre-roll/hangover/force-cut, absolute timestamps
- [x] `backend/transcriber.py` — `transcribe_pcm(..., engine)` reuses warm model
- [x] `backend/app.py` — `/ws/stream`, worker queue, concurrency cap, session cap, keepalive
- [x] `backend/ws_test_client.py` — replay tool (`--engine`, `--realtime`)
- [x] Local replay passes (Google and Whisper)
- [x] Commit + push, Render redeploys
- [x] Render replay passes

### Phase 2 — checklist
- [x] `frontend/pcm-worklet.js` — 48k→16k int16 frames, low-pass + linear resample
- [x] `index.html` + `styles.css` rewritten: rice paper, turbulence grain, ink mountains, ensō mic, STT seal, brush tabs
- [x] `app.js`: Upload / Live tabs, engine selectors, WS client w/ reconnect + cold-start state, level meter, timer, status, finals transcript
- [x] Copy/download/timestamps preserved; a11y + reduced-motion pass
- [x] Playwright E2E: upload, live Google, live Whisper, tab-switch persistence, mobile
- [x] Fix: keep trailing silence for Google (300 ms pad) and stop trimming speech on `stop`

### Phase 3 — checklist
- [ ] Vercel deploy of `frontend/` (static, no build)
- [x] `config.js` `API_BASE` → Render URL
- [ ] CORS check from deployed origin
- [x] README with architecture, protocol, measured latency, limits
- [ ] Public-URL smoke test (upload + live)

## Architecture

```
upload:  browser --multipart--> FastAPI /api/transcribe -> PyAV decode (16k mono)
              -> engine=google: SpeechRecognition -> Google Web Speech (45 s chunks, 300 ms tail pad)
              -> engine=whisper: faster-whisper tiny (warm model, int8)
              -> on Google failure: automatic Whisper fallback + warning

live:    mic -> AudioWorklet (16k int16, 100 ms) -> WS /ws/stream -> VAD endpointer
              -> utterance -> asyncio.to_thread(transcribe_pcm, engine) -> JSON final
```

## WebSocket protocol (`/ws/stream`)

Client → server
- first message (JSON, within 15 s): `{"type":"start","language":"auto|en|hi|...","engine":"google|whisper"}`
- binary: raw little-endian int16 PCM, mono, 16 kHz, any chunk size (100 ms recommended)
- `{"type":"ping"}` keepalive (send every 20 s)
- `{"type":"stop"}` — flushes the open utterance, drains the queue

Server → client
- `ready`: `{sample_rate, frame_ms, max_seconds, engine, whisper_model}`
- `vad`: `{speaking: bool}`
- `processing`: `{seq, start}` — utterance accepted
- `final`: `{seq, text, start, end, language, engine, warnings, audio_seconds, elapsed, provider}`
- `empty`: `{seq, start}` — VAD segment had no speech
- `warning`: backlog / skipped segment
- `error`: `{code, message}` → `too_many_streams`, `too_long`, `expected_start`, engine failures
- `pong`, `done`: `{dropped}`

## Measured performance

| Where | Engine | Audio | Wall | RTF | Notes |
| --- | --- | --- | --- | --- | --- |
| Local (desktop) | Whisper tiny | 8.0 s | 0.84–0.90 s | 0.11 | warm model, HTTP upload |
| Local (desktop) | Google | 8.0 s | 2.2–2.6 s | 0.28–0.33 | network-bound, any CPU |
| Render free | Whisper tiny | 8.0 s | 44.9–50.5 s | **5.6–6.3** | fallback only, too slow for live |
| Local live | Google | 2.7–2.9 s phrase | 0.62–1.41 s | — | 1–2 finals per ~3 s phrase |
| Local live | Whisper tiny | 2.7–2.9 s phrase | 0.74–0.81 s | — | same endpointer |

Interpretation: RTF < 0.5 smooth, 0.5–1 demoable, > 1 tune/reduce utterance size.

**Finding (Phase 0):** Render's 0.1 CPU runs local Whisper at RTF 5.6–6.3, so live Whisper there
is unusable. HF Docker Spaces now require a paid plan. Resolution: use `SpeechRecognition` →
Google Web Speech (network-bound, keyless) as the default engine and keep local Whisper tiny as
the fallback/second engine. Groq hosted Whisper was implemented, mock-tested, then removed as
unnecessary for the brief and extra deployment risk.

**Google quirks (solved):** Google drops the last word when an utterance ends with almost no
silence. Google requests now append 300 ms of silence; `stop` no longer trims speech off the
final utterance. Google also occasionally merges "tool. The" into "to all the" — an accuracy
limitation, not a pipeline bug; Whisper returns the exact words.

## Env vars (backend)

`DEFAULT_ENGINE=google`, `WHISPER_MODEL_SIZE=tiny`, `WHISPER_MODEL_DIR=models`,
`WHISPER_VAD=1` (uploads only), `MAX_UPLOAD_MB=25`, `MAX_AUDIO_SECONDS=300`,
`MAX_STREAM_SECONDS=300`, `MAX_CONCURRENT_STREAMS=2`, `RATE_LIMIT_REQUESTS=15`,
`RATE_LIMIT_WINDOW_SECONDS=600`, `ALLOWED_ORIGINS=...`

No API keys required.

## Open risks

- Google's free endpoint can rate-limit datacenter IPs; the UI warns and falls back to Whisper.
- Whisper fallback on Render is ~6× slower than real time, so it is a safety net, not the live path.
- Free instance sleeps; first request can take ~60 s (UI shows "waking server").
- 512 MB RAM is fine for tiny int8; only one model instance may exist (module singleton + lock).
- OneDrive-backed working copy can confuse git with CRLF warnings; committed as LF.
