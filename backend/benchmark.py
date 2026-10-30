"""Measure transcription speed against a running API.

    python backend/benchmark.py samples/speech-test.wav
    python backend/benchmark.py samples/speech-test.wav --url https://x.onrender.com
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--engine", default="google")
    parser.add_argument("--language", default="auto")
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()

    data = args.file.read_bytes()
    print(f"file={args.file.name} size={len(data) / 1024:.1f} KB url={args.url}")

    for run in range(1, args.runs + 1):
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{args.file.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + data + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="engine"\r\n\r\n{args.engine}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="language"\r\n\r\n{args.language}\r\n'
            f"--{boundary}--\r\n"
        ).encode()

        started = time.perf_counter()
        response = requests.post(
            f"{args.url.rstrip('/')}/api/transcribe",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=300,
        )
        wall = time.perf_counter() - started

        if response.status_code != 200:
            print(f"run {run}: HTTP {response.status_code} {response.text[:300]}")
            continue

        payload = response.json()
        audio = payload["duration"]
        rtf = wall / audio if audio else float("inf")
        print(
            f"run {run}: audio={audio:.1f}s wall={wall:.1f}s "
            f"server_elapsed={payload.get('elapsed')}s RTF={rtf:.2f} "
            f"chars={len(payload.get('text', ''))}"
        )
        if run == 1:
            print(json.dumps({"text": payload.get("text", "")[:200]}, indent=2))


if __name__ == "__main__":
    main()
