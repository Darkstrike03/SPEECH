"""Replay an audio file into the live streaming endpoint.

    python backend/ws_test_client.py samples/speech-test.wav
    python backend/ws_test_client.py samples/speech-test.wav --url https://x.onrender.com --realtime
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))

from transcriber import decode_audio  # noqa: E402

CHUNK = 1600  # 100 ms at 16 kHz


async def run(args: argparse.Namespace) -> int:
    pcm = decode_audio(args.file.read_bytes())
    if args.pad_silence:
        silence = np.zeros(int(16000 * args.pad_silence), dtype=np.int16)
        pcm = np.concatenate([silence, pcm, silence])

    ws_url = args.url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    ws_url += "/ws/stream"
    print(f"connecting to {ws_url} with {len(pcm) / 16000:.1f}s of audio")

    finals = 0
    wall_started = time.perf_counter()

    async with websockets.connect(ws_url, max_size=None, open_timeout=300) as ws:
        await ws.send(
            json.dumps({"type": "start", "language": args.language, "engine": args.engine})
        )
        ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=300))
        print("server:", ready)

        async def reader() -> None:
            nonlocal finals
            async for message in ws:
                payload = json.loads(message)
                if payload.get("type") == "final":
                    finals += 1
                    print(
                        f"  [final {payload['seq']}] {payload['start']:.2f}-{payload['end']:.2f}s "
                        f"({payload.get('elapsed')}s to transcribe): {payload['text']}"
                    )
                elif payload.get("type") in ("error", "warning"):
                    print("  !", payload)
                elif payload.get("type") == "done":
                    return

        reader_task = asyncio.create_task(reader())
        step = 0.1 if args.realtime else args.speed
        for offset in range(0, len(pcm), CHUNK):
            await ws.send(pcm[offset : offset + CHUNK].tobytes())
            await asyncio.sleep(step)

        await ws.send(json.dumps({"type": "stop"}))
        await asyncio.wait_for(reader_task, timeout=300)

    wall = time.perf_counter() - wall_started
    print(f"sent {len(pcm) / 16000:.1f}s audio in {wall:.1f}s, {finals} final segment(s)")
    return 0 if finals else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--language", default="auto")
    parser.add_argument("--engine", default="google", choices=("google", "whisper"))
    parser.add_argument("--realtime", action="store_true", help="send at real-time pace")
    parser.add_argument("--speed", type=float, default=0.01, help="seconds between 100ms chunks")
    parser.add_argument("--pad-silence", type=float, default=0.5, help="seconds of silence to add")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
