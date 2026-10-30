"""Local CLI for testing transcription without the web app.

    python backend/cli.py samples/audio.mp3
    python backend/cli.py samples/audio.wav --engine google --language en
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from transcriber import TranscriptionError, transcribe  # noqa: E402


def format_timestamp(seconds: float) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"{int(minutes):02d}:{secs:05.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe an audio file.")
    parser.add_argument("file", type=Path, help="Path to a wav/mp3/m4a/ogg/flac file")
    parser.add_argument(
        "--engine", default="google", choices=["whisper", "google"], help="ASR engine"
    )
    parser.add_argument(
        "--language", default="auto", help="Language code, e.g. en, hi (auto = detect/en-US)"
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON result")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"File not found: {args.file}", file=sys.stderr)
        return 2

    started = time.perf_counter()
    try:
        result = transcribe(args.file.read_bytes(), args.engine, args.language)
    except TranscriptionError as exc:
        print(f"Error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1

    elapsed = time.perf_counter() - started

    if args.json:
        print(
            json.dumps(
                {
                    "text": result.text,
                    "engine": result.engine,
                    "language": result.language,
                    "duration": round(result.duration, 2),
                    "segments": result.segments,
                    "warnings": result.warnings,
                },
                indent=2,
            )
        )
        return 0

    print(f"-- {args.file.name} | {result.engine} | {result.language} "
          f"| {result.duration:.1f}s audio | {elapsed:.1f}s wall --")
    if result.segments:
        for segment in result.segments:
            print(f"[{format_timestamp(segment['start'])} -> "
                  f"{format_timestamp(segment['end'])}] {segment['text']}")
        print()
    print(result.text or "(no text)")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
