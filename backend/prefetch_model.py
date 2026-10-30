"""Pre-download the Whisper model during deployment so the first request is fast."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from transcriber import WHISPER_MODEL_DIR, WHISPER_MODEL_SIZE, get_whisper_model  # noqa: E402


def main() -> int:
    print(f"Pre-downloading faster-whisper '{WHISPER_MODEL_SIZE}' into {WHISPER_MODEL_DIR}")
    get_whisper_model()
    print("Model ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
