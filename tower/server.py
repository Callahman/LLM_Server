"""Tower Mumble pipeline: voice + text in, Whisper + Ollama, Mumble text out.

Runs as a systemd service on the tower. Threads:
  - pymumble Mumble thread (library; receives audio/text, sends commands)
  - worker thread: utterance detection -> transcribe -> archive (one job at a time)
  - llm worker thread: ask LLM -> push reply (decoupled from transcription)
  - keep-awake thread: lockfile for the tower's idle-shutdown integration
"""

import json
import logging
import logging.handlers
import os
import queue
import threading
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

import whisper

import audio_pipeline
import harness
import mumble_client

load_dotenv()

# --- Mumble ---
# No real default: MUMBLE_HOST must be set in tower/.env (see tower/.env.example).
MUMBLE_HOST = os.getenv("MUMBLE_HOST", "")
MUMBLE_PORT = int(os.getenv("MUMBLE_PORT", "64738"))
MUMBLE_USER = os.getenv("MUMBLE_USER", "tower-bot")
MUMBLE_PASSWORD = os.getenv("MUMBLE_PASSWORD", "")
MUMBLE_CERT = os.getenv("MUMBLE_CERT") or None
MUMBLE_KEY = os.getenv("MUMBLE_KEY") or None
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "Home")

# --- Whisper / LLM ---
# Accuracy/latency tradeoff: base is fastest, small is the recommended
# default for a casual pipeline, medium is the most accurate (slowest on CPU).
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
# Language of the speakers (ISO 639-1). Setting it skips per-utterance
# language detection, which is unreliable on short, noisy clips.
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "en")
# Utterances whose no_speech_prob exceeds this are treated as silence.
NO_SPEECH_PROB_MAX = float(os.getenv("NO_SPEECH_PROB_MAX", "0.6"))
# Utterances whose avg_logprob is below this are treated as too unreliable
# to send to the LLM.
AVG_LOGPROB_MIN = float(os.getenv("AVG_LOGPROB_MIN", "-1.0"))
# Silero VAD trims leading/trailing silence before transcribing
# (downloads the VAD model on first run).
WHISPER_VAD_FILTER = os.getenv("WHISPER_VAD_FILTER", "1") == "1"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:latest")
LLM_SYSTEM_PROMPT = os.getenv(
    "LLM_SYSTEM_PROMPT",
    "You are a helpful assistant. Answer the user's question or comment.",
)
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# --- Code-execution harness (optional) ---
# Wrap the LLM with smolagents CodeAgent + a Docker-sandboxed Python executor
# confined to SANDBOX_DIR. Requires Docker on the tower (see SETUP.md §6.1).
# If the harness is unavailable at runtime, messages fall back to the plain
# single-shot LLM call.
LLM_HARNESS = os.getenv("LLM_HARNESS", "0") == "1"
SANDBOX_DIR = os.path.expanduser(os.getenv("SANDBOX_DIR", "~/llm_server/sandbox"))
HARNESS_MAX_STEPS = int(os.getenv("HARNESS_MAX_STEPS", "6"))
HARNESS_TIMEOUT_SECONDS = int(os.getenv("HARNESS_TIMEOUT_SECONDS", "300"))

# --- Utterance detection ---
AUDIO_RMS_THRESHOLD = int(os.getenv("AUDIO_RMS_THRESHOLD", "400"))
SILENCE_MS = int(os.getenv("SILENCE_MS", "700"))
MAX_UTTERANCE_MS = int(os.getenv("MAX_UTTERANCE_MS", "30000"))
MIN_UTTERANCE_MS = int(os.getenv("MIN_UTTERANCE_MS", "300"))

# --- Archive / logs ---
AUDIO_ARCHIVE_DIR = os.path.expanduser(os.getenv("AUDIO_ARCHIVE_DIR", "~/llm_server/archive"))
AUDIO_RETENTION_GB = float(os.getenv("AUDIO_RETENTION_GB", "5"))
LOG_DIR = os.path.expanduser(os.getenv("LOG_DIR", "~/llm_server/logs"))

# --- Keep-awake ---
KEEP_AWAKE_DIR = os.getenv("KEEP_AWAKE_DIR", "/var/run/keep-awake.d")
KEEP_AWAKE_GRACE_SECONDS = int(os.getenv("KEEP_AWAKE_GRACE_SECONDS", "300"))
KEEP_AWAKE_LOCKFILE = "mumble_tower_bot"
KEEP_AWAKE_POLL_SECONDS = 30


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    log_path = Path(LOG_DIR)
    log_path.mkdir(parents=True, exist_ok=True)
    fileh = logging.handlers.RotatingFileHandler(
        log_path / "activity.jsonl", maxBytes=5 * 1024 * 1024, backupCount=5
    )
    fileh.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(fileh)


setup_logging()
log = logging.getLogger("server")


def activity(event, **fields):
    log.info(json.dumps({"ts": round(time.time(), 3), "event": event, **fields}))


# --- shared state ---
job_queue = queue.Queue()
llm_queue = queue.Queue()
whisper_model = None
bot = None
pipeline = None
code_harness = None


def load_whisper_model():
    global whisper_model
    if whisper_model is not None:
        return
    log.info("loading Whisper model %r (first load may take a while)...", WHISPER_MODEL_SIZE)
    # device="cpu": openai-whisper auto-selects CUDA when a GPU is present.
    # On machines whose PyTorch build has no kernels for the installed GPU
    # (CUDA error: no kernel image is available for execution on the device),
    # CPU is the safe choice — and fast enough for Whisper base on short
    # voice messages. If you have a matching torch/GPU build, pass
    # device="cuda" here instead.
    whisper_model = whisper.load_model(WHISPER_MODEL_SIZE, device="cpu")
    log.info("Whisper model ready")


def wait_for_whisper(timeout=600):
    deadline = time.time() + timeout
    while whisper_model is None:
        if time.time() > deadline:
            return False
        time.sleep(2)
    return True


def to_16khz(pcm_b):
    """48 kHz int16 PCM -> 16 kHz float32 in [-1, 1].

    Whisper expects 16 kHz input and does NOT resample; feeding it the
    pipeline's 48 kHz audio makes the model hear speech 3x faster and 3x
    higher-pitched (garbage transcripts). 48/16 == 3 exactly, and Mumble's
    Opus voice stream is already band-limited to ~8 kHz, so a plain 3:1
    decimation is exact and needs no anti-aliasing filter.
    """
    samples = np.frombuffer(pcm_b, dtype=np.int16).astype(np.float32) / 32768.0
    return samples[::3]


def transcribe(wav_b):
    """Transcribe one utterance WAV (48 kHz).

    Returns (text, gate): gate is None for a usable transcript, or a short
    reason ("no_speech" / "low_confidence") when the utterance is judged to
    be silence or too unreliable to send to the LLM.
    """
    audio = to_16khz(wav_b[44:])  # skip the RIFF header, resample 48k -> 16k
    result = whisper_model.transcribe(
        audio,
        fp16=False,
        language=WHISPER_LANGUAGE,
        condition_on_previous_text=False,  # utterances are independent
        vad_filter=WHISPER_VAD_FILTER,
    )
    text = (result.get("text") or "").strip()
    if result.get("no_speech_prob", 0.0) > NO_SPEECH_PROB_MAX:
        return "", "no_speech"
    if result.get("avg_logprob", 0.0) < AVG_LOGPROB_MIN:
        return "", "low_confidence"
    return text, None


llm = OpenAI(api_key="ollama", base_url=OLLAMA_BASE_URL + "/v1")


def ask_llm(speaker, text):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            completion = llm.chat.completions.create(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": "The user '%s' said: %s" % (speaker, text)},
                ],
            )
            return (completion.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            log.warning("LLM attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
            time.sleep(2 * attempt)
    raise last_err


def llm_reply(speaker, text):
    """Ask the LLM, via the code-execution harness when enabled.

    Falls back to the plain single-shot call whenever the harness is
    unavailable (no smolagents, no Docker, timeout, build failure) so the
    bot keeps answering.
    """
    if code_harness is not None:
        try:
            return code_harness.ask(speaker, text)
        except harness.HarnessUnavailable as e:
            log.warning("harness unavailable (%s); falling back to plain LLM", e)
            activity("harness_fallback", reason=str(e))
    return ask_llm(speaker, text)


def archive_wav(wav_b, speaker):
    arch = Path(AUDIO_ARCHIVE_DIR)
    arch.mkdir(parents=True, exist_ok=True)
    safe = (speaker or "unknown").replace(" ", "_")
    path = arch / ("%d_%s.wav" % (int(time.time()), safe))
    path.write_bytes(wav_b)
    return path


def enforce_retention():
    arch = Path(AUDIO_ARCHIVE_DIR)
    if not arch.exists():
        return
    files = sorted(arch.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    limit = int(AUDIO_RETENTION_GB * 1024 ** 3)
    for p in files:
        if total <= limit:
            break
        total -= p.stat().st_size
        p.unlink()


# --- Mumble callbacks (called by the Mumble client) ---

def on_connected():
    activity("mumble_connected")
    bot.post_status("(%s online)" % MUMBLE_USER)
    threading.Thread(target=load_whisper_model, daemon=True).start()


def on_disconnected():
    activity("mumble_disconnected")


def on_sound(session, name, pcm):
    pipeline.feed(session, name, pcm)


def on_text(message):
    # message: mumble_pb2.TextMessage (already running in its own thread)
    # Mumble's TextMessage has no "type" field. The SENDER is in actor
    # (0/absent for server messages). session = target session(s) (private
    # messages); channel_id = target channel(s) (channel messages).
    sender = message.actor
    if sender == 0:
        return  # server message (no actor)
    if sender == bot.my_session():
        return
    if message.channel_id and bot.channel is not None \
            and message.channel_id[0] != bot.channel["channel_id"]:
        return
    user = bot.mumble.users.get(sender)
    name = user.get("name") if user else "user-%d" % sender
    text = (message.message or "").strip()
    if not text:
        return
    activity("text_received", speaker=name, text=text)
    job_queue.put(("text", text, name, None))


# --- workers ---

def worker():
    """Transcribe utterances and hand finished text to the LLM stage."""
    while True:
        kind, payload, speaker, extra = job_queue.get()
        try:
            if kind == "utterance":
                if not wait_for_whisper():
                    log.error("Whisper model failed to load; dropping utterance from %s",
                              speaker)
                    continue
                wav_b, duration = payload, extra
                spoken, gate = transcribe(wav_b)
                if not spoken:
                    activity("transcription_empty", speaker=speaker,
                             duration=duration, gate=gate)
                    continue
                activity("transcription", speaker=speaker, text=spoken, duration=duration)
                try:
                    path = archive_wav(wav_b, speaker)
                    activity("audio_archived", path=str(path))
                    enforce_retention()
                except Exception as e:
                    log.warning("archive failed: %s", e)
                llm_queue.put(("voice", spoken, speaker))
            elif kind == "text":
                llm_queue.put(("text", payload, speaker))
        except Exception as e:
            log.exception("job failed")
            activity("job_failed", speaker=speaker, error=str(e))


def llm_worker():
    """Ask the LLM and push replies, off the transcription path.

    LLM calls take 10s+; running them here keeps new utterances being
    transcribed while the model is still answering the previous one.
    """
    while True:
        via, text, speaker = llm_queue.get()
        try:
            reply = llm_reply(speaker, text)
            activity("llm_reply", speaker=speaker, reply=reply, via=via)
            if reply:
                bot.push(reply)
        except Exception as e:
            log.exception("LLM job failed")
            activity("llm_failed", speaker=speaker, error=str(e))


# --- keep-awake ---

def keep_awake():
    lock_path = Path(KEEP_AWAKE_DIR) / KEEP_AWAKE_LOCKFILE
    last_active = 0.0
    try:
        if lock_path.exists() and not bot.occupancy():
            lock_path.unlink()
            activity("keep_awake_stale_cleared")
    except Exception:
        pass
    while True:
        time.sleep(KEEP_AWAKE_POLL_SECONDS)
        try:
            if bot.occupancy():
                last_active = time.monotonic()
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_path.touch()
            elif time.monotonic() - last_active > KEEP_AWAKE_GRACE_SECONDS:
                if lock_path.exists():
                    lock_path.unlink()
                    activity("keep_awake_released")
        except Exception as e:
            log.warning("keep-awake tick failed: %s", e)


def main():
    if not MUMBLE_HOST:
        raise SystemExit(
            "Missing required config: MUMBLE_HOST — set it in tower/.env "
            "(see tower/.env.example for the template)."
        )
    global bot, pipeline, code_harness
    pipeline = audio_pipeline.AudioPipeline(
        job_queue,
        rms_threshold=AUDIO_RMS_THRESHOLD,
        silence_ms=SILENCE_MS,
        max_utterance_ms=MAX_UTTERANCE_MS,
        min_utterance_ms=MIN_UTTERANCE_MS,
    )
    bot = mumble_client.MumbleBot(
        host=MUMBLE_HOST, port=MUMBLE_PORT, user=MUMBLE_USER,
        target_channel=TARGET_CHANNEL,
        on_connected=on_connected, on_disconnected=on_disconnected,
        on_sound=on_sound, on_text=on_text,
        password=MUMBLE_PASSWORD, certfile=MUMBLE_CERT, keyfile=MUMBLE_KEY,
    )
    if LLM_HARNESS:
        code_harness = harness.CodeHarness(
            model_id=OLLAMA_MODEL,
            api_base=OLLAMA_BASE_URL + "/v1",
            sandbox_dir=SANDBOX_DIR,
            max_steps=HARNESS_MAX_STEPS,
            timeout_seconds=HARNESS_TIMEOUT_SECONDS,
            system_prompt=LLM_SYSTEM_PROMPT,
        )
        log.info("code-execution harness enabled (sandbox %s)", SANDBOX_DIR)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=llm_worker, daemon=True).start()
    threading.Thread(target=keep_awake, daemon=True).start()
    log.info("connecting to Mumble %s:%s as %r (channel %r)...",
             MUMBLE_HOST, MUMBLE_PORT, MUMBLE_USER, TARGET_CHANNEL)
    bot.start()  # blocks until the first connection completes
    log.info("Mumble connected; pipeline running")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
