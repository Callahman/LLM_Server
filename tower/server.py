"""Tower Mumble pipeline: voice + text in, Whisper + Ollama, Mumble text out.

Runs as a systemd service on the tower. Threads:
  - pymumble Mumble thread (library; receives audio/text, sends commands)
  - worker thread: transcribe -> LLM -> push (one job at a time)
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
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:latest")
LLM_SYSTEM_PROMPT = os.getenv(
    "LLM_SYSTEM_PROMPT",
    "You are a helpful assistant. Answer the user's question or comment.",
)
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

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
whisper_model = None
bot = None
pipeline = None


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


def transcribe(wav_b):
    pcm = wav_b[44:]  # skip the RIFF header
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    result = whisper_model.transcribe(audio, fp16=False)
    return (result.get("text") or "").strip()


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
    # Mumble's TextMessage has no "type" field — derive it from the
    # addressing fields: empty session = server message, channel_id set
    # = channel message, otherwise private message.
    if not message.session:
        return  # server message
    sender = message.session[0]
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


# --- worker ---

def worker():
    while True:
        kind, payload, speaker, extra = job_queue.get()
        try:
            if kind == "utterance":
                if not wait_for_whisper():
                    log.error("Whisper model failed to load; dropping utterance from %s",
                              speaker)
                    continue
                wav_b, duration = payload, extra
                spoken = transcribe(wav_b)
                if not spoken:
                    activity("transcription_empty", speaker=speaker, duration=duration)
                    continue
                activity("transcription", speaker=speaker, text=spoken, duration=duration)
                try:
                    path = archive_wav(wav_b, speaker)
                    activity("audio_archived", path=str(path))
                    enforce_retention()
                except Exception as e:
                    log.warning("archive failed: %s", e)
                reply = ask_llm(speaker, spoken)
                activity("llm_reply", speaker=speaker, reply=reply, via="voice")
                if reply:
                    bot.push(reply)
            elif kind == "text":
                reply = ask_llm(speaker, payload)
                activity("llm_reply", speaker=speaker, reply=reply, via="text")
                if reply:
                    bot.push(reply)
        except Exception as e:
            log.exception("job failed")
            activity("job_failed", speaker=speaker, error=str(e))


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
    global bot, pipeline
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
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=keep_awake, daemon=True).start()
    log.info("connecting to Mumble %s:%s as %r (channel %r)...",
             MUMBLE_HOST, MUMBLE_PORT, MUMBLE_USER, TARGET_CHANNEL)
    bot.start()  # blocks until the first connection completes
    log.info("Mumble connected; pipeline running")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
