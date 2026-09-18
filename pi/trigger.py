"""Pi Mumble trigger: wakes the tower and keeps it alive while people are in
the target Mumble channel.

State machine:
  IDLE -> (presence or text) -> TRIGGERING (WOL + wait for SSH)
  TRIGGERING -> (SSH up) -> ACTIVE (long-lived SSH keepalive)
  ACTIVE -> (no presence) -> GRACE
  GRACE -> (presence returns) -> ACTIVE
  GRACE -> (grace expires) -> IDLE (keepalive closed)

Runs as a systemd service on the Pi, connected to the local murmurd.
"""

import logging
import os
import socket
import subprocess
import threading
import time

from dotenv import load_dotenv

import pymumble_py3 as pymumble
from pymumble_py3.constants import (
    PYMUMBLE_CLBK_CONNECTED,
    PYMUMBLE_CLBK_DISCONNECTED,
    PYMUMBLE_CLBK_TEXTMESSAGERECEIVED,
    PYMUMBLE_CLBK_USERCREATED,
    PYMUMBLE_CLBK_USERREMOVED,
    PYMUMBLE_CLBK_USERUPDATED,
)
from pymumble_py3.errors import UnknownChannelError

load_dotenv()

# --- Mumble (local murmurd) ---
MUMBLE_HOST = os.getenv("MUMBLE_HOST", "127.0.0.1")
MUMBLE_PORT = int(os.getenv("MUMBLE_PORT", "64738"))
MUMBLE_USER = os.getenv("MUMBLE_USER", "pi-trigger")
MUMBLE_PASSWORD = os.getenv("MUMBLE_PASSWORD", "")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "Home")

# --- Tower ---
# No real defaults: these must be set in pi/.env (see pi/.env.example).
TOWER_HOST = os.getenv("TOWER_HOST", "")
WOL_MAC_ADDRESS = os.getenv("WOL_MAC_ADDRESS", "")
TOWER_SSH_USER = os.getenv("TOWER_SSH_USER", "")
TOWER_SSH_KEY = os.getenv("TOWER_SSH_KEY", "~/.ssh/id_ed25519")

# --- Behavior ---
WOL_WAIT_SECONDS = int(os.getenv("WOL_WAIT_SECONDS", "300"))
GRACE_SECONDS = int(os.getenv("GRACE_SECONDS", "300"))
PRESENCE_POLL_SECONDS = float(os.getenv("PRESENCE_POLL_SECONDS", "5"))
POST_STATUS = os.getenv("POST_STATUS", "1") == "1"
IGNORE_USER = {u.strip() for u in os.getenv("IGNORE_USER", "tower-bot").split(",") if u.strip()}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("trigger")


def send_wol(mac):
    """Broadcast a standard 102-byte Wake-on-LAN magic packet."""
    b = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + b * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.sendto(packet, ("255.255.255.255", 9))
    finally:
        sock.close()
    log.info("WOL packet sent to %s", mac)


def ssh_reachable(key, timeout=10):
    """Check whether the tower answers a non-interactive SSH login."""
    cmd = ["ssh", "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=%d" % timeout,
           "-o", "StrictHostKeyChecking=accept-new"]
    if key and os.path.exists(key):
        cmd += ["-i", key]
    cmd.append("%s@%s" % (TOWER_SSH_USER, TOWER_HOST))
    cmd.append("true")
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def wait_for_ssh(key, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ssh_reachable(key):
            return True
        time.sleep(5)
    return False


class Trigger:
    def __init__(self):
        self.state = "IDLE"
        self.channel = None
        self.presence = 0
        self.last_presence = 0.0
        self.ssh_proc = None
        self.lock = threading.Lock()
        self.key = os.path.expanduser(TOWER_SSH_KEY)

        self.mumble = pymumble.Mumble(MUMBLE_HOST, MUMBLE_USER, port=MUMBLE_PORT,
                                      password=MUMBLE_PASSWORD, reconnect=True)
        # No set_receive_sound: the trigger does not need audio.
        self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, self._cb_connected)
        self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_DISCONNECTED, self._cb_disconnected)
        self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_TEXTMESSAGERECEIVED, self._cb_text)
        for cb in (PYMUMBLE_CLBK_USERCREATED, PYMUMBLE_CLBK_USERREMOVED, PYMUMBLE_CLBK_USERUPDATED):
            self.mumble.callbacks.add_callback(cb, lambda *a: self._request_presence_check())

    # --- lifecycle ---

    def start(self):
        self.mumble.start()
        self.mumble.is_ready()
        threading.Thread(target=self._presence_loop, daemon=True).start()
        threading.Thread(target=self._grace_loop, daemon=True).start()

    # --- mumble callbacks ---

    def _cb_connected(self):
        log.info("connected to murmurd")
        try:
            self.channel = self.mumble.channels.find_by_name(TARGET_CHANNEL)
        except UnknownChannelError:
            log.warning("channel %r not found yet", TARGET_CHANNEL)
            self.channel = None
        # Re-evaluate presence after (re)connect: WOL is idempotent.
        self._request_presence_check()

    def _cb_disconnected(self):
        log.warning("disconnected from murmurd (auto-reconnect active)")
        self.channel = None

    def _cb_text(self, message):
        # The library runs text callbacks in their own thread.
        # Mumble's TextMessage has no "type" field — derive it from the
        # addressing fields: empty session = server message, channel_id
        # set = channel message, otherwise private message.
        if not message.session:
            return  # server message
        if message.session[0] == self.mumble.users.myself_session:
            return
        if message.channel_id and self.channel is not None \
                and message.channel_id[0] != self.channel["channel_id"]:
            return
        user = self.mumble.users.get(message.session[0])
        name = user.get("name") if user else None
        if name in IGNORE_USER:
            return
        if self.state == "IDLE":
            self._trigger("text message")

    def _request_presence_check(self):
        # USER* callbacks run on the mumble thread: do not block it.
        threading.Thread(target=self._check_presence, daemon=True).start()

    # --- presence ---

    def _presence_loop(self):
        while True:
            time.sleep(PRESENCE_POLL_SECONDS)
            try:
                self._check_presence()
            except Exception as e:
                log.warning("presence check failed: %s", e)

    def _check_presence(self):
        if self.channel is None:
            return
        try:
            me = self.mumble.users.myself_session
            n = len([u for u in self.channel.get_users() if u["session"] != me])
        except Exception:
            return
        changed = False
        with self.lock:
            if n != self.presence:
                self.presence = n
                self.last_presence = time.monotonic()
                changed = True
                log.info("presence: %d user(s) in %r", n, TARGET_CHANNEL)
        if not changed:
            return
        if n > 0:
            if self.state == "IDLE":
                self._trigger("presence")
            elif self.state == "GRACE":
                with self.lock:
                    self.state = "ACTIVE"
                log.info("presence returned; back to ACTIVE")
        else:
            with self.lock:
                if self.state == "ACTIVE":
                    self.state = "GRACE"
                    log.info("grace period started (%ds)", GRACE_SECONDS)

    # --- state transitions ---

    def _trigger(self, reason):
        with self.lock:
            if self.state != "IDLE":
                return
            self.state = "TRIGGERING"
        log.info("triggering tower (%s)", reason)
        if POST_STATUS:
            self._post("Server is waking up...")
        threading.Thread(target=self._do_wake, daemon=True).start()

    def _post(self, text):
        if self.channel is not None:
            try:
                self.channel.send_text_message(text)
            except Exception as e:
                log.warning("post failed: %s", e)

    def _do_wake(self):
        send_wol(WOL_MAC_ADDRESS)
        ok = wait_for_ssh(self.key, WOL_WAIT_SECONDS)
        with self.lock:
            if self.state != "TRIGGERING":
                return
            if ok:
                self.state = "ACTIVE"
            else:
                self.state = "IDLE"
                self.presence = 0
        if ok:
            self._start_keepalive()
            log.info("tower is up; SSH keepalive started")
        else:
            log.error("tower did not come up within %ds", WOL_WAIT_SECONDS)
            if POST_STATUS:
                self._post("Server failed to wake. Check the tower.")

    def _start_keepalive(self):
        cmd = ["ssh", "-N",
               "-o", "ServerAliveInterval=10",
               "-o", "ServerAliveCountMax=3",
               "-o", "ConnectTimeout=5",
               "-o", "StrictHostKeyChecking=accept-new"]
        if self.key and os.path.exists(self.key):
            cmd += ["-i", self.key]
        cmd.append("%s@%s" % (TOWER_SSH_USER, TOWER_HOST))
        self.ssh_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)

    def _stop_keepalive(self):
        if self.ssh_proc is not None:
            self.ssh_proc.terminate()
            try:
                self.ssh_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.ssh_proc.kill()
            self.ssh_proc = None

    def _grace_loop(self):
        while True:
            time.sleep(10)
            with self.lock:
                state = self.state
                presence = self.presence
                last = self.last_presence
            if state == "GRACE":
                if presence > 0:
                    with self.lock:
                        self.state = "ACTIVE"
                    log.info("presence returned; back to ACTIVE")
                elif time.monotonic() - last > GRACE_SECONDS:
                    with self.lock:
                        self.state = "IDLE"
                    self._stop_keepalive()
                    log.info("grace expired; keepalive closed")
                    if POST_STATUS:
                        self._post("Server going idle.")
            elif state == "ACTIVE":
                if self.ssh_proc is None or self.ssh_proc.poll() is not None:
                    log.warning("SSH keepalive died; restarting")
                    self._start_keepalive()


def main():
    missing = [name for name, val in (
        ("TOWER_HOST", TOWER_HOST),
        ("WOL_MAC_ADDRESS", WOL_MAC_ADDRESS),
        ("TOWER_SSH_USER", TOWER_SSH_USER),
    ) if not val]
    if missing:
        raise SystemExit(
            "Missing required config: %s — set them in pi/.env "
            "(see pi/.env.example for the template)." % ", ".join(missing)
        )
    t = Trigger()
    t.start()
    log.info("trigger running (channel=%r, tower=%s@%s)",
             TARGET_CHANNEL, TOWER_SSH_USER, TOWER_HOST)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
