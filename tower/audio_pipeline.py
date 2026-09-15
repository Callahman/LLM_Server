"""Per-user audio buffering and utterance detection.

Input: 16-bit 48 kHz mono PCM frames (10 ms each) from Mumble's
SOUNDRECEIVED callback. A simple RMS energy threshold detects the start and
end of an utterance; finished utterances are emitted to the job queue as
WAV bytes + speaker name + duration.
"""

import array
import logging
import struct
import time
from collections import deque

log = logging.getLogger("audio_pipeline")

SAMPLE_RATE = 48000
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH  # 96000


def rms_int16(pcm):
    """RMS level of a 16-bit little-endian PCM buffer."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    n = len(samples)
    if n == 0:
        return 0
    total = 0
    for s in samples:
        total += s * s
    return int((total / n) ** 0.5)


def wav_bytes(pcm):
    """Package raw 16-bit 48 kHz mono PCM into a WAV (RIFF) file."""
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, 1, 1, SAMPLE_RATE, BYTES_PER_SECOND, SAMPLE_WIDTH, 16,
        b"data", len(pcm),
    )
    return header + pcm


class _UserState:
    __slots__ = ("name", "buffer", "recording", "last_voice", "started")

    def __init__(self, name):
        self.name = name
        self.buffer = deque()
        self.recording = False
        self.last_voice = 0.0
        self.started = 0.0


class AudioPipeline:
    def __init__(self, job_queue, rms_threshold=400, silence_ms=700,
                 max_utterance_ms=30000, min_utterance_ms=300):
        self.job_queue = job_queue
        self.rms_threshold = rms_threshold
        self.silence_s = silence_ms / 1000.0
        self.max_utterance_s = max_utterance_ms / 1000.0
        self.min_utterance_s = min_utterance_ms / 1000.0
        self.users = {}

    def feed(self, session, name, pcm):
        """Called from the Mumble thread for every audio frame. Keep it fast."""
        state = self.users.get(session)
        if state is None:
            state = self.users[session] = _UserState(name)
        elif name and name != state.name:
            state.name = name

        energy = rms_int16(pcm)
        now = time.monotonic()

        if not state.recording:
            if energy >= self.rms_threshold:
                state.recording = True
                state.buffer.clear()
                state.buffer.append(pcm)
                state.last_voice = now
                state.started = now
        else:
            state.buffer.append(pcm)
            if energy >= self.rms_threshold:
                state.last_voice = now
            silence = now - state.last_voice
            duration = now - state.started
            if silence >= self.silence_s or duration >= self.max_utterance_s:
                reason = "silence" if silence >= self.silence_s else "max-length"
                self._finish(state, session, reason)

    def _finish(self, state, session, reason):
        state.recording = False
        size = sum(len(p) for p in state.buffer)
        duration = size / BYTES_PER_SECOND
        if duration < self.min_utterance_s:
            state.buffer.clear()
            log.debug("dropped %.3fs blip from %s", duration, state.name)
            return
        pcm = b"".join(state.buffer)
        state.buffer.clear()
        self.job_queue.put(("utterance", wav_bytes(pcm), state.name, round(duration, 2)))
        log.info("utterance from %s: %.2fs (%s)", state.name, duration, reason)

    def drop_user(self, session):
        self.users.pop(session, None)
