"""Streaming endpointing for live microphone audio.

Turns a stream of 16 kHz mono int16 samples into complete utterances using an
adaptive energy VAD, so Whisper only ever runs on speech.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16_000
FRAME_MS = 20
PRE_ROLL_MS = 300
HANGOVER_MS = 400
MIN_UTTERANCE_MS = 400
MAX_UTTERANCE_MS = 15_000

ABS_MIN_THRESHOLD = 220.0
NOISE_MULTIPLIER = 2.5
NOISE_MARGIN = 120.0
NOISE_EWMA = 0.05


@dataclass
class Utterance:
    pcm: np.ndarray
    start: float
    end: float


@dataclass
class VadEvent:
    speaking: bool


class StreamEndpointer:
    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        frame_ms: int = FRAME_MS,
        pre_roll_ms: int = PRE_ROLL_MS,
        hangover_ms: int = HANGOVER_MS,
        min_utterance_ms: int = MIN_UTTERANCE_MS,
        max_utterance_ms: int = MAX_UTTERANCE_MS,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame = int(sample_rate * frame_ms / 1000)
        self.pre_frames = max(1, int(pre_roll_ms / frame_ms))
        self.hang_frames = max(1, int(hangover_ms / frame_ms))
        self.min_frames = max(1, int(min_utterance_ms / frame_ms))
        self.max_frames = max(1, int(max_utterance_ms / frame_ms))

        self._carry = np.zeros(0, dtype=np.int16)
        self._pre: deque[np.ndarray] = deque(maxlen=self.pre_frames)
        self._frames: list[np.ndarray] = []
        self._in_speech = False
        self._silence_run = 0
        self._noise = 60.0
        self._offset = 0
        self._start_time = 0.0

    @property
    def speaking(self) -> bool:
        return self._in_speech

    def push(self, samples: np.ndarray) -> list[Utterance | VadEvent]:
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16, copy=False)
        if samples.size:
            self._carry = np.concatenate([self._carry, samples])

        events: list[Utterance | VadEvent] = []
        complete = len(self._carry) // self.frame
        for index in range(complete):
            frame = self._carry[index * self.frame : (index + 1) * self.frame]
            events.extend(self._process(frame))
        self._carry = self._carry[complete * self.frame :]
        return events

    def flush(self) -> list[Utterance | VadEvent]:
        events: list[Utterance | VadEvent] = []
        if self._in_speech and len(self._frames) >= self.min_frames:
            events.append(self._close(trim=False))
        self._reset()
        return events

    def _process(self, frame: np.ndarray) -> list[Utterance | VadEvent]:
        events: list[Utterance | VadEvent] = []
        energy = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        threshold = max(self._noise * NOISE_MULTIPLIER + NOISE_MARGIN, ABS_MIN_THRESHOLD)
        is_speech = energy > threshold

        if not self._in_speech:
            if not is_speech:
                self._noise = (1 - NOISE_EWMA) * self._noise + NOISE_EWMA * energy
            self._pre.append(frame.copy())
            if is_speech:
                self._frames = list(self._pre)
                self._in_speech = True
                self._silence_run = 0
                self._start_time = max(
                    0.0,
                    (self._offset + self.frame - len(self._frames) * self.frame) / self.sample_rate,
                )
                events.append(VadEvent(True))
        else:
            self._frames.append(frame.copy())
            self._silence_run = 0 if is_speech else self._silence_run + 1

            if self._silence_run >= self.hang_frames:
                events.append(self._close(trim=True))
                events.append(VadEvent(False))
            elif len(self._frames) >= self.max_frames:
                events.append(self._close(trim=False))
                events.append(VadEvent(False))

        self._offset += self.frame
        return events

    def _close(self, trim: bool) -> Utterance:
        frames = self._frames
        if trim and len(frames) > self.hang_frames:
            frames = frames[: -self.hang_frames + 1]

        pcm = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
        start = round(self._start_time, 2)
        utterance = Utterance(
            pcm=pcm,
            start=start,
            end=round(start + len(pcm) / self.sample_rate, 2),
        )

        self._in_speech = False
        self._frames = []
        self._silence_run = 0
        self._pre.clear()
        return utterance

    def _reset(self) -> None:
        self._in_speech = False
        self._frames = []
        self._silence_run = 0
        self._pre.clear()
