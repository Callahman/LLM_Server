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
# Max size (GB) to retain in each archive (audio, text, reply); the oldest
# files are deleted first
AUDIO_RETENTION_GB = float(os.getenv("AUDIO_RETENTION_GB", "5"))
# User prompts (typed messages + transcripts) and LLM replies are archived
# as .txt files (same <unix-ts>_<speaker> naming as the audio) in these
# directories. These are the permanent record — never touched by "/clear".
TEXT_ARCHIVE_DIR = os.path.expanduser(os.getenv("TEXT_ARCHIVE_DIR", "~/llm_server/text"))
REPLY_ARCHIVE_DIR = os.path.expanduser(os.getenv("REPLY_ARCHIVE_DIR", "~/llm_server/reply"))
# Max conversation turns (prompt+reply pairs) sent to the LLM with each new
# prompt; 0 = no history (stateless, like before)
HISTORY_MAX_TURNS = int(os.getenv("HISTORY_MAX_TURNS", "10"))
# The clearable context window: holds only the most recent
# HISTORY_MAX_TURNS prompt+reply pairs. The LLM's conversation is built
# from here (it survives restarts), and "/clear" wipes only this directory.
CONVERSATION_DIR = os.path.expanduser(os.getenv("CONVERSATION_DIR", "~/llm_server/conversation"))
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


# The activity log is for debugging: full chat content lives in the
# TEXT/REPLY archives, so the log keeps only a short snippet.
LOG_CHAT_SNIPPET = 100


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


def ask_llm(speaker, text, history=()):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            messages = [{"role": "system", "content": LLM_SYSTEM_PROMPT}]
            messages += [{"role": role, "content": content} for role, content in history]
            messages.append({"role": "user",
                             "content": "The user '%s' said: %s" % (speaker, text)})
            completion = llm.chat.completions.create(model=OLLAMA_MODEL, messages=messages)
            return (completion.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            log.warning("LLM attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
            time.sleep(2 * attempt)
    raise last_err


def llm_reply(speaker, text, history=()):
    """Ask the LLM (with the shared conversation history), via the
    code-execution harness when enabled.

    Falls back to the plain single-shot call whenever the harness is
    unavailable (no smolagents, no Docker, timeout, build failure) so the
    bot keeps answering.
    """
    if code_harness is not None:
        try:
            return code_harness.ask(speaker, text, history=history)
        except harness.HarnessUnavailable as e:
            log.warning("harness unavailable (%s); falling back to plain LLM", e)
            activity("harness_fallback", reason=str(e))
    return ask_llm(speaker, text, history=history)


def archive_wav(wav_b, speaker):
    arch = Path(AUDIO_ARCHIVE_DIR)
    arch.mkdir(parents=True, exist_ok=True)
    safe = (speaker or "unknown").replace(" ", "_")
    path = arch / ("%d_%s.wav" % (int(time.time()), safe))
    path.write_bytes(wav_b)
    return path


def enforce_retention(arch_dir, pattern="*.wav", limit_gb=None):
    """Delete the oldest files in an archive once it exceeds limit_gb."""
    if limit_gb is None:
        limit_gb = AUDIO_RETENTION_GB
    arch = Path(arch_dir)
    if not arch.exists():
        return
    files = sorted(arch.glob(pattern), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    limit = int(limit_gb * 1024 ** 3)
    for p in files:
        if total <= limit:
            break
        total -= p.stat().st_size
        p.unlink()


def archive_text(text, speaker, arch_dir):
    """Save one prompt/reply as <unix-ts>_<speaker>.txt; return the path."""
    arch = Path(arch_dir)
    arch.mkdir(parents=True, exist_ok=True)
    safe = (speaker or "unknown").replace(" ", "_")
    path = arch / ("%d_%s.txt" % (int(time.time()), safe))
    path.write_text(text, encoding="utf-8")
    return path


# --- shared conversation history ---
# One conversation for the whole channel: the most recent
# HISTORY_MAX_TURNS prompt+reply pairs, in order. CONVERSATION_DIR is the
# source of truth for the window (it survives restarts and is what
# "/clear" wipes); the list below mirrors it. The TEXT/REPLY archives are
# the permanent record and never feed the context.
conversation = []           # list of (role, content); user content is pre-formatted
_conversation_loaded = False

# Commands that wipe the shared context window (archives are never touched).
CLEAR_COMMANDS = ("/clear", "clear chat history")          # exact, text
CLEAR_COMMANDS_VOICE = (                                   # fuzzy, spoken
    "clear chat history", "clear the chat history",
    "please clear the chat history", "clear chat history please",
)


def _window_dirs():
    """The (prompts, replies) subdirectories of the context window."""
    return (Path(CONVERSATION_DIR) / "prompts", Path(CONVERSATION_DIR) / "replies")


def _file_ts(path):
    try:
        return int(path.name.split("_", 1)[0])
    except (ValueError, IndexError):
        return 0


def load_conversation():
    """Rebuild the shared conversation from the context window directory."""
    prompts, replies = _window_dirs()
    events = []
    for arch_dir, role in ((prompts, "user"), (replies, "assistant")):
        if not arch_dir.exists():
            continue
        for path in arch_dir.glob("*.txt"):
            name = path.name
            try:
                ts = int(name.split("_", 1)[0])
            except (ValueError, IndexError):
                continue
            speaker = name.split("_", 1)[1][:-4] if "_" in name else "unknown"
            try:
                content = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content:
                events.append((ts, role, speaker, content))
    events.sort(key=lambda e: e[0])
    turns = []
    for ts, role, speaker, content in events:
        if role == "user":
            turns.append(("user", "The user '%s' said: %s" % (speaker, content)))
        else:
            turns.append(("assistant", content))
    return turns


def get_conversation():
    global _conversation_loaded
    if not _conversation_loaded:
        conversation.extend(load_conversation())
        _conversation_loaded = True
    return conversation


def append_turn(speaker, text, reply):
    """Add a completed prompt+reply to the context window (dir + memory)."""
    prompts, replies = _window_dirs()
    try:
        prompts.mkdir(parents=True, exist_ok=True)
        replies.mkdir(parents=True, exist_ok=True)
        archive_text(text, speaker, str(prompts))
        archive_text(reply, MUMBLE_USER, str(replies))
    except Exception as e:
        log.warning("conversation dir write failed: %s", e)
    conversation.extend(
        [("user", "The user '%s' said: %s" % (speaker, text)),
         ("assistant", reply)])
    trim_conversation()


def trim_conversation():
    """Keep only the most recent HISTORY_MAX_TURNS pairs (dir + memory)."""
    if not HISTORY_MAX_TURNS:
        return
    if len(conversation) > HISTORY_MAX_TURNS * 2:
        del conversation[:-HISTORY_MAX_TURNS * 2]
    prompts, replies = _window_dirs()
    if not prompts.exists():
        return
    excess = len(list(prompts.glob("*.txt"))) - HISTORY_MAX_TURNS
    if excess <= 0:
        return
    for path in sorted(prompts.glob("*.txt"), key=_file_ts)[:excess]:
        path.unlink(missing_ok=True)
    if replies.exists():
        for path in sorted(replies.glob("*.txt"), key=_file_ts)[:excess]:
            path.unlink(missing_ok=True)


def clear_conversation():
    """Wipe the context window (dir + memory). Archives are never touched."""
    global _conversation_loaded
    conversation.clear()
    _conversation_loaded = True  # don't reload the wiped dir
    prompts, replies = _window_dirs()
    for arch in (prompts, replies):
        if arch.exists():
            for path in arch.glob("*.txt"):
                path.unlink(missing_ok=True)
    log.info("conversation cleared")


def _is_clear_command(spoken):
    """Fuzzy match for a spoken clear command (transcripts are noisy)."""
    norm = " ".join(spoken.lower().split()).strip(" .,!?")
    return norm in CLEAR_COMMANDS_VOICE


def history_turns():
    """The recent shared turns to send with the next prompt (capped)."""
    if not HISTORY_MAX_TURNS:
        return []
    turns = get_conversation()[-HISTORY_MAX_TURNS * 2:]
    while turns and turns[0][0] == "assistant":
        turns = turns[1:]
    return turns


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
    if text.lower() in CLEAR_COMMANDS:
        threading.Thread(target=clear_conversation, daemon=True).start()
        activity("conversation_cleared", by=name, via="text")
        bot.push("(chat history cleared)")
        return
    activity("text_received", speaker=name, text=text[:LOG_CHAT_SNIPPET])
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
                activity("transcription", speaker=speaker,
                         text=spoken[:LOG_CHAT_SNIPPET], duration=duration)
                if _is_clear_command(spoken):
                    threading.Thread(target=clear_conversation, daemon=True).start()
                    activity("conversation_cleared", by=speaker, via="voice")
                    bot.push("(chat history cleared)")
                    continue
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
    """Archive each turn, ask the LLM with the shared history, push and
    archive the reply — off the transcription path.

    LLM calls take 10s+; running them here keeps new utterances being
    transcribed while the model is still answering the previous one.
    """
    while True:
        via, text, speaker = llm_queue.get()
        try:
            # Fetch the history BEFORE archiving the current prompt, so the
            # prompt isn't in its own history.
            history = history_turns()
            try:
                archive_text(text, speaker, TEXT_ARCHIVE_DIR)
                enforce_retention(TEXT_ARCHIVE_DIR, "*.txt")
            except Exception as e:
                log.warning("text archive failed: %s", e)
            reply = llm_reply(speaker, text, history=history)
            if reply:
                reply_path = None
                try:
                    reply_path = archive_text(reply, MUMBLE_USER, REPLY_ARCHIVE_DIR)
                    enforce_retention(REPLY_ARCHIVE_DIR, "*.txt")
                except Exception as e:
                    log.warning("reply archive failed: %s", e)
                append_turn(speaker, text, reply)
                activity("llm_reply", speaker=speaker, via=via,
                         reply=reply[:LOG_CHAT_SNIPPET],
                         path=str(reply_path) if reply_path else None)
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
