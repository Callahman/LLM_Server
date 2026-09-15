# Deployment Guide

Devices involved:

| Device | Role | What runs |
|---|---|---|
| Pi (Raspberry Pi, always on) | Mumble server + trigger | `mumble-server` (murmurd), `pi/trigger.py` |
| Tower (Ubuntu server, sleeps when idle) | Pipeline | `tower/server.py`, Whisper, Ollama |
| Phone (Android) | User client | Mumble app (+ Tailscale app) |
| Desktop | User client (optional) | Mumble client (+ Tailscale) |

---

## 1. Pi — Mumble server (murmurd)

```bash
sudo apt update
sudo apt install -y mumble-server
sudo dpkg-reconfigure mumble-server     # set the SuperUser password
sudo systemctl enable --now mumble-server
```

- Port `64738` is Mumble's standard default — no change needed.
- See `murmurd/mumble-server.ini` for the reference config (log path,
  database, welcome text).
- **Create the channel**: connect with a desktop Mumble client as SuperUser,
  right-click the root channel → "Add Channel" → name it **Home** (or change
  `TARGET_CHANNEL` in the `.env` files to match whatever you pick).
- **Static IP**: reserve the Pi's LAN IP in the router so the tower has a
  stable address (the tower's `MUMBLE_HOST`).

## 2. Tailscale (off-network access)

Install on the **Pi, the tower, and the phone** (Tailscale app on Android):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

With MagicDNS enabled, the Pi is reachable as `rpi-<id>.ts.net` from anywhere
on the tailnet. LAN access works without Tailscale; Tailscale is only needed
when the phone/tower are off the home network.

## 3. Phone — Mumble app

1. Install **Mumble** from the Play Store.
2. Add a server:
   - Host: the Pi's LAN IP (at home) or its Tailscale hostname (away)
   - Port: `64738`
3. Accept the self-signed TLS certificate warning on first connect.
4. Join the **Home** channel.

## 4. Tower — desktop Mumble client (optional GUI)

```bash
sudo apt install -y mumble
```

Add the same server and join **Home** — this is the "GUI" half of the
original design (talk to the tower from a desktop while the phone talks from
the couch).

## 5. Pi — trigger service

```bash
# system deps (libopus is needed to build pymumble's opuslib dependency)
sudo apt install -y libopus-dev python3-venv

sudo mkdir -p /opt/mumble-listener
sudo cp -r <this-repo>/pi /opt/mumble-listener/pi
cd /opt/mumble-listener/pi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # review the values (see §7)

# install the systemd unit (edit User= first)
sudo cp pi-trigger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-trigger.service
```

The trigger connects to the **local** murmurd — no Tailscale needed for it.

## 6. Tower — pipeline service

```bash
# system deps (ffmpeg is used by whisper; libopus for opuslib)
sudo apt install -y libopus-dev ffmpeg python3-venv

# make sure Ollama is running with the model pulled
sudo systemctl enable --now ollama
ollama pull qwen3.5:latest

sudo mkdir -p /opt/mumble-listener
sudo cp -r <this-repo>/tower /opt/mumble-listener/tower
cd /opt/mumble-listener/tower
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # torch is a large download
cp .env.example .env          # set MUMBLE_HOST = Pi's IP or ts.net hostname

sudo cp tower-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tower-server.service
```

The service blocks on its first Mumble connect and auto-reconnects every ~10 s
if the Pi is unreachable (e.g., the tower booted before the Pi finished
starting).

## 7. Values carried over from the old Discord `.env` files

These transport-agnostic values were in the old Pi `.env` and are **pre-filled
in `pi/.env.example`**:

| Value | New location |
|---|---|
| `TOWER_HOST=10.0.0.12` | `pi/.env` |
| `WOL_MAC_ADDRESS=B4:2E:99:A1:E1:AC` | `pi/.env` |
| `TOWER_SSH_USER=mason_callahan` | `pi/.env` |
| `TOWER_SSH_KEY=~/.ssh/id_ed25519` | `pi/.env` |

Discord-only values (bot token, channel IDs) are gone for good.

## 8. First-run test

1. Power the tower **off**.
2. Join **Home** from the phone.
3. Within ~5 s the trigger posts *"Server is waking up..."* and sends WOL.
4. The tower boots; `tower-server` connects to Mumble and posts
   *"(tower-bot online)"*.
5. **Speak** — after a silence gap (~0.7 s) the bot answers in the channel.
6. **Type** a message — the bot answers.
7. **Leave** the channel — after the grace period (5 min) the trigger posts
   *"Server going idle."* and closes the SSH keepalive.

## 9. Tuning

| Setting (tower `.env`) | Default | Meaning |
|---|---|---|
| `AUDIO_RMS_THRESHOLD` | `400` | Raise if the bot triggers on background noise; lower if it misses quiet speech |
| `SILENCE_MS` | `700` | Silence gap that ends an utterance |
| `MAX_UTTERANCE_MS` | `30000` | Force-end very long utterances |
| `MIN_UTTERANCE_MS` | `300` | Drop blips shorter than this |
| `WHISPER_MODEL_SIZE` | `base` | `small` = better accuracy, slower |
| `GRACE_SECONDS` (pi) | `300` | Idle grace before the keepalive closes |
| `POST_STATUS` (pi) | `1` | Post waking/idle status in the channel |

## 10. Troubleshooting

```bash
journalctl -u pi-trigger -f        # Pi trigger logs
journalctl -u tower-server -f      # Tower pipeline logs
tail -f ~/mumble_listener/logs/activity.jsonl   # structured activity log
tail -f /var/log/mumble-server/mumble-server.log  # murmurd log
```

- **Tower can't reach the Pi**: `nc -vz <pi-host> 64738` from the tower (LAN
  or Tailscale).
- **WOL doesn't wake the tower**: WOL only works on the same L2 (same LAN,
  no router in between); check the tower's BIOS WOL setting; make sure the
  Pi and tower share a subnet.
- **`opuslib` build fails on `pip install`**: `sudo apt install libopus-dev`
  and retry.
- **Bot answers its own messages**: it shouldn't (filtered by session id). On
  the Pi, make sure `IGNORE_USER` includes the tower bot's username so the
  trigger doesn't re-wake the tower on bot replies.
- **Half-open TCP (mesh drops connections)**: both services auto-reconnect
  (pymumble `reconnect=True`, 10 s interval) and systemd restarts them on
  thread death.
