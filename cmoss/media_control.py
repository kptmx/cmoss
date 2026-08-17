"""Media-control integration (MPRIS on Linux, SMTC on Windows).

Bridges the player's observable `DataBox`es to the OS media UI so users can
control cmoss from desktop media keys / panels and see now-playing info. The
base `MediaControl` owns the subscription + command plumbing and is a safe
no-op; platform backends override the ``_open`` / ``_shutdown`` hooks and the
``_emit_*`` methods.

DataBox notifications are marshalled onto the Flet page loop, so every
``_emit_*`` callback runs on that thread. Commands issued by the OS transport
(which may come from any thread) are dispatched back onto the page loop with
``_schedule`` — the same marshalling the UI itself relies on.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class MediaControl:
    def __init__(self, store):
        self.store = store
        self.player = store.player
        self.loop = store._loop
        self._started = False
        self._closed = False
        self._disposers = []
        self._position_ms = int(self.player.position_ms.get() or 0)

    # -- lifecycle -------------------------------------------------------

    def start(self):
        if self._started:
            return
        self._started = True
        for box, fn in (
            (self.player.state, self._on_state),
            (self.player.playing, self._on_song),
            (self.player.queue_rev, self._on_queue),
            (self.player.position_ms, self._on_position),
            (self.player.duration_ms, self._on_duration),
            (self.player.volume, self._on_volume),
            (self.player.shuffle, self._on_shuffle),
            (self.player.repeat, self._on_repeat),
        ):
            try:
                self._disposers.append(box.subscribe(fn))
            except Exception as e:
                log.debug("media control subscribe failed: %s", e)
        try:
            self._open()
        except Exception as e:
            log.warning("media control init failed: %s", e)

    def close(self):
        if self._closed:
            return
        self._closed = True
        for d in self._disposers:
            try:
                d()
            except Exception:
                pass
        self._disposers = []
        try:
            self._shutdown()
        except Exception as e:
            log.debug("media control shutdown failed: %s", e)

    # -- DataBox listeners (page-loop thread) ---------------------------

    def _on_state(self, sender, _field):
        self._emit_state(sender.get())

    def _on_song(self, sender, _field):
        self._emit_metadata(sender.get())
        self._emit_can_go()

    def _on_queue(self, sender, _field):
        self._emit_can_go()

    def _on_position(self, sender, _field):
        ms = int(sender.get() or 0)
        prev = self._position_ms
        self._position_ms = ms
        self._emit_position(ms, seeked=abs(ms - prev) > 3000)

    def _on_duration(self, sender, _field):
        self._emit_duration(int(sender.get() or 0))

    def _on_volume(self, sender, _field):
        self._emit_volume(float(sender.get() or 0.0))

    def _on_shuffle(self, sender, _field):
        self._emit_shuffle(bool(sender.get()))

    def _on_repeat(self, sender, _field):
        self._emit_repeat(str(sender.get() or "off"))

    # -- transport hooks (no-op by default) ------------------------------

    def _open(self):
        pass

    def _shutdown(self):
        pass

    def _emit_state(self, _state):
        pass

    def _emit_metadata(self, _song):
        pass

    def _emit_can_go(self):
        pass

    def _emit_position(self, _ms, seeked=False):
        pass

    def _emit_duration(self, _ms):
        pass

    def _emit_volume(self, _v):
        pass

    def _emit_shuffle(self, _on):
        pass

    def _emit_repeat(self, _mode):
        pass

    # -- command dispatch (any thread) -----------------------------------

    def cmd_play(self):
        self._schedule(self.player.resume)

    def cmd_pause(self):
        self._schedule(self.player.pause)

    def cmd_toggle(self):
        self._schedule(self.player.toggle)

    def cmd_stop(self):
        self._schedule(self.player.stop)

    def cmd_next(self):
        self._schedule(self.player.next)

    def cmd_prev(self):
        self._schedule(self.player.prev)

    def cmd_seek(self, ms):
        self._schedule(self._do_seek, max(0, int(ms)))

    def _do_seek(self, ms):
        self.player.seek(ms)

    def cmd_volume(self, v):
        self._schedule(self.player.set_volume, max(0.0, min(1.0, float(v))))

    def cmd_shuffle(self, on):
        self._schedule(self._do_shuffle, bool(on))

    def _do_shuffle(self, on):
        if bool(self.player.shuffle.get()) != on:
            self.player.toggle_shuffle()

    def cmd_repeat(self, mode):
        self._schedule(self.player.set_repeat, mode)

    def _schedule(self, fn, *args):
        loop = self.loop
        if loop is None or loop.is_closed() or self._closed:
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except Exception as e:
            log.debug("media control dispatch failed: %s", e)


def create_media_control(store):
    """Return the platform-appropriate media-control backend.

    Falls back to the no-op base `MediaControl` when the platform backend can't
    be built (missing dependency / unsupported OS), so the app never breaks.
    """
    import sys

    if sys.platform == "win32":
        try:
            from .smtc import SmtcControl

            return SmtcControl(store)
        except Exception as e:
            log.warning("SMTC backend unavailable: %s", e)
    else:
        try:
            from .mpris import MprisControl

            return MprisControl(store)
        except Exception as e:
            log.warning("MPRIS backend unavailable: %s", e)
    return MediaControl(store)
