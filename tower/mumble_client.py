"""Mumble connection wrapper for the tower pipeline.

Wraps pymumble's Mumble thread and translates library callbacks into
app-level callbacks. Outbound messages go through pymumble's command queue,
so push() is safe to call from the worker thread.
"""

import logging

import pymumble_py3 as pymumble
from pymumble_py3.constants import (
    PYMUMBLE_CLBK_CONNECTED,
    PYMUMBLE_CLBK_DISCONNECTED,
    PYMUMBLE_CLBK_SOUNDRECEIVED,
    PYMUMBLE_CLBK_TEXTMESSAGERECEIVED,
)
from pymumble_py3.errors import UnknownChannelError

log = logging.getLogger("mumble_client")

PUSH_CHUNK = 2000  # max characters per pushed message


class MumbleBot:
    def __init__(self, host, port, user, target_channel,
                 on_connected, on_disconnected, on_sound, on_text,
                 password="", certfile=None, keyfile=None):
        self.target_channel = target_channel
        self.channel = None  # resolved Channel object (None until found)
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_sound = on_sound
        self.on_text = on_text

        self.mumble = pymumble.Mumble(
            host, user, port=port, password=password,
            certfile=certfile, keyfile=keyfile,
            reconnect=True,  # auto-reconnect every ~10s while the app is alive
            client_type=1,   # flag the connection as a bot
        )
        # Must be set before start(): enables audio handling and the
        # per-user sound queues.
        self.mumble.set_receive_sound(True)
        self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, self._cb_connected)
        self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_DISCONNECTED, self._cb_disconnected)
        self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self._cb_sound)
        self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_TEXTMESSAGERECEIVED, self._cb_text)

    def start(self):
        """Start the client thread and block until the connection completes."""
        self.mumble.start()
        self.mumble.is_ready()

    # --- library callbacks ---

    def _cb_connected(self):
        log.info("connected to Mumble server")
        try:
            self.channel = self.mumble.channels.find_by_name(self.target_channel)
            self.channel.move_in()  # move our own session into the channel
        except UnknownChannelError:
            log.warning("target channel %r not found yet; will retry on next connect",
                        self.target_channel)
            self.channel = None
        self.on_connected()

    def _cb_disconnected(self):
        log.warning("disconnected from Mumble server (auto-reconnect active)")
        self.channel = None
        self.on_disconnected()

    def _cb_sound(self, user, chunk):
        # Runs on the pymumble loop thread: only hand off the decoded PCM.
        self.on_sound(user["session"], user.get("name"), chunk.pcm)

    def _cb_text(self, message):
        # The library already runs text callbacks in a new thread.
        self.on_text(message)

    # --- outbound (thread-safe: queued to the pymumble thread) ---

    def push(self, text):
        """Send a (possibly long) reply to the channel, chunked."""
        if self.channel is None:
            log.warning("no channel resolved; cannot push")
            return
        for i in range(0, len(text), PUSH_CHUNK):
            self.channel.send_text_message(text[i:i + PUSH_CHUNK])

    def post_status(self, text):
        if self.channel is not None:
            self.channel.send_text_message(text)

    def occupancy(self):
        """List of (session, name) in the target channel, excluding ourselves."""
        if self.channel is None:
            return []
        me = self.mumble.users.myself_session
        return [(u["session"], u.get("name")) for u in self.channel.get_users()
                if u["session"] != me]

    def my_session(self):
        return self.mumble.users.myself_session

    def stop(self):
        self.mumble.stop()
