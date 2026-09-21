# LLM Server

A self-hosted Mumble voice + text pipeline that replaces the old Discord
setup. No gateway, no API tokens, no third-party ToS — just a murmurd on the
Pi and two small Python services.

## How it works

```
+-------------------------------------------------------------------+
| Pi (always on)                                                    |
|                                                                   |
|  murmurd (port 64738)  <----  phone Mumble app / desktop client   |
|                                                                   |
|  pi/trigger.py:                                                   |
|    channel occupancy + text messages                              |
|      -> Wake-on-LAN the tower                                     |
|      -> long-lived SSH keepalive while people are in the channel  |
+-----------------------------|-------------------------------------+
                              | WOL / SSH (LAN)
                              v
+-------------------------------------------------------------------+
| Tower (sleeps when idle)                                          |
|                                                                   |
|  tower/server.py:                                                 |
|    pymumble client (auto-reconnect)                               |
|      voice: per-user PCM buffer -> utterance detection            |
|              -> Whisper (transcribe) -> Ollama (LLM)              |
|              -> Mumble text message                               |
|      text:  Mumble text message -> Ollama -> Mumble text message  |
|    keep-awake lockfile for the tower's idle-shutdown integration  |
+-------------------------------------------------------------------+
```

- **Phone / desktop**: any Mumble client, joined to the `Home` channel.
- **Pi**: runs murmurd (a plain OS service) and the trigger service. The Pi
  never runs a demanding real-time pipeline — that was the original failure
  mode of the Discord setup.
- **Tower**: the only machine that runs Whisper + Ollama. It wakes on demand
  and is kept alive while the channel has people in it.

## Repository layout

```
LLM_Server/
├── README.md                  # this file
├── SETUP.md                   # full deployment guide (read this)
├── .gitignore                 # keeps .env, .venv/, and bytecode out of git
├── murmurd/
│   └── mumble-server.ini      # reference murmurd config for the Pi
├── pi/
│   ├── trigger.py             # Mumble trigger: WOL + SSH keepalive
│   ├── requirements.txt
│   ├── .env.example           # template with stand-in placeholders
│   └── pi-trigger.service     # systemd unit
├── vendor/
│   ├── README.md              # why pymumble is vendored (upstream source 404s)
│   └── pymumble-1.7*.whl      # prebuilt vendored wheel(s), per platform tag
└── tower/
    ├── server.py              # entry point: wires everything together
    ├── harness.py             # optional: smolagents CodeAgent + Docker-sandboxed code execution
    ├── mumble_client.py       # pymumble connection wrapper + push
    ├── audio_pipeline.py      # per-user buffer + utterance detection
    ├── requirements.txt
    ├── .env.example           # template with stand-in placeholders
    └── tower-server.service   # systemd unit
```

## Quick start

1. Read `SETUP.md` and deploy (murmurd on the Pi, Tailscale, both services).
2. Join the `Home` channel from the phone — the tower wakes up.
3. Speak or type — the bot answers in the channel.

## Notes

- `pymumble` (1.7) is the Mumble client library, installed from a prebuilt
  vendored wheel under `vendor/` (the original 1.7 source now 404s upstream
  and the other releases still use `ssl.wrap_socket`, removed in Python 3.12 —
  see `vendor/README.md`). Its media path is TCP-tunneled only (no UDP), which
  is fine for a self-hosted LAN/tailnet. Audio arrives already decoded as
  16-bit 48 kHz mono PCM — no Opus decoding in our code.
- Callbacks run in the pymumble thread; our services keep that thread fast —
  audio frames are processed there (RMS check + per-user buffering, numpy
  vectorized) and only finished utterances are handed to a worker queue; the
  library itself runs text callbacks in their own thread.
- The tower connection values (host, WOL MAC, SSH user/key) are **not**
  committed — `pi/.env.example` ships with stand-in placeholders, and the real
  `.env` is gitignored. Set your own values in `pi/.env` on deploy (see
  `SETUP.md` §7).
- **Code-execution harness (optional)**: with `LLM_HARNESS=1`, the LLM is
  wrapped in smolagents' `CodeAgent` — the model can write and run Python in
  a Docker container confined to `SANDBOX_DIR` (no network, capped RAM/CPU).
  If Docker is unavailable, messages fall back to the plain LLM (see
  `SETUP.md` §6.1).
- **Shared conversation**: the LLM keeps one conversation for the whole
  channel — each new prompt is sent with the recent prompts and replies of
  all speakers (capped at `HISTORY_MAX_TURNS` turns). The context window
  lives in `CONVERSATION_DIR` (survives restarts; wiped by `/clear` or a
  spoken "clear chat history"), while every prompt and reply is also
  archived under `TEXT_ARCHIVE_DIR` / `REPLY_ARCHIVE_DIR` (and each utterance
  WAV under `AUDIO_ARCHIVE_DIR`). Archives are never touched by `/clear`, but
  they are size-capped at `AUDIO_RETENTION_GB` (default 5 GB) — the oldest
  files are deleted first. The activity log keeps only short snippets
  (debugging); full content lives in the archives.
