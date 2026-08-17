"""SMTC — Windows System Media Transport Controls (MPRIS equivalent).

Uses the Windows Runtime (``winrt-sdk`` package, Windows only) to surface
now-playing info and media-key control in the Windows media flyout / lock
screen. Two transport layers are attempted in order:

1. Modern ``SystemMediaTransportControls`` on a dedicated DispatcherQueue
   thread — full metadata + timeline (position/duration) support.
2. Legacy ``Windows.Media.MediaControl`` (windowless) — basic play / pause /
   next / previous + title / artist / album.

Everything is best-effort: any missing import or runtime failure degrades to
the next layer and finally to a no-op, so the app keeps running on machines
without ``winrt-sdk`` or a usable media UI.

.. note:: ``pip install winrt-sdk`` is required on Windows. Only the legacy
   ``MediaControl`` layer can be tested without a windowing context.
"""
from __future__ import annotations

import datetime
import logging
import threading

from .media_control import MediaControl

log = logging.getLogger(__name__)

try:
    import winrt.windows.media as _wm
    import winrt.windows.system as _ws

    _WINRT = True
except Exception:  # pragma: no cover - not importable on non-Windows
    _WINRT = False


class SmtcControl(MediaControl):
    def __init__(self, store):
        super().__init__(store)
        self._controls = None
        self._updater = None
        self._dispatcher = None
        self._controller = None
        self._legacy = False
        self._thread = None

    # -- lifecycle ---------------------------------------------------------

    def _open(self):
        if not _WINRT:
            log.warning("SMTC: winrt-sdk not available; disabled")
            return
        self._thread = threading.Thread(target=self._bootstrap, daemon=True)
        self._thread.start()

    def _bootstrap(self):
        try:
            controller = _ws.DispatcherQueueController.create_on_dedicated_thread()
            self._controller = controller
            queue = controller.dispatcher_queue
            queue.try_enqueue(lambda: self._setup_modern(queue))
        except Exception as e:
            log.warning("SMTC: modern path unavailable (%s); trying MediaControl", e)
            self._setup_legacy()

    def _setup_modern(self, queue):
        try:
            controls = _wm.SystemMediaTransportControls.get_for_current_view()
            if controls is None:
                raise RuntimeError("no current view for SMTC")
            controls.is_enabled = True
            controls.is_play_enabled = True
            controls.is_pause_enabled = True
            controls.is_next_enabled = True
            controls.is_previous_enabled = True
            controls.is_stop_enabled = True
            controls.is_seek_enabled = True
            controls.playback_status = _wm.MediaPlaybackStatus.CLOSED
            controls.button_pressed += self._on_button
            controls.playback_position_change_requested += self._on_seek_requested
            controls.auto_repeat_mode_change_requested += self._on_repeat_requested
            controls.shuffle_enabled_change_requested += self._on_shuffle_requested
            self._controls = controls
            self._updater = controls.display_updater
            self._dispatcher = queue
            log.info("SMTC: SystemMediaTransportControls active")
            self._push_all()
        except Exception as e:
            log.warning("SMTC: modern path failed (%s); trying MediaControl", e)
            self._setup_legacy()

    def _setup_legacy(self):
        try:
            mc = _wm.MediaControl
            mc.play_pressed += self._on_legacy_play
            mc.pause_pressed += self._on_legacy_pause
            mc.play_pause_toggle_pressed += self._on_legacy_toggle
            mc.next_track_pressed += self._on_legacy_next
            mc.previous_track_pressed += self._on_legacy_prev
            self._legacy = True
            log.info("SMTC: MediaControl active")
            self._push_all()
        except Exception as e:
            log.warning("SMTC: MediaControl unavailable: %s", e)

    def _shutdown(self):
        self._dispatcher = None
        self._controls = None
        self._updater = None
        self._controller = None
        self._thread = None

    # -- incoming: system UI events (dispatcher thread) -------------------

    def _on_button(self, _sender, args):
        btn = getattr(args, "button", None)
        if btn is None:
            return
        if btn == _wm.SystemMediaTransportControlsButton.PLAY:
            self.cmd_play()
        elif btn == _wm.SystemMediaTransportControlsButton.PAUSE:
            self.cmd_pause()
        elif btn == _wm.SystemMediaTransportControlsButton.PLAY_PAUSE:
            self.cmd_toggle()
        elif btn == _wm.SystemMediaTransportControlsButton.NEXT:
            self.cmd_next()
        elif btn == _wm.SystemMediaTransportControlsButton.PREVIOUS:
            self.cmd_prev()
        elif btn == _wm.SystemMediaTransportControlsButton.STOP:
            self.cmd_stop()

    def _on_seek_requested(self, _sender, args):
        pos = getattr(args, "requested_playback_position", None)
        if pos is None:
            return
        ms = int(getattr(pos, "total_seconds", lambda: 0)() * 1000)
        self.cmd_seek(ms)

    def _on_repeat_requested(self, _sender, args):
        mode = getattr(args, "requested_auto_repeat_mode", None)
        mapping = {
            _wm.MediaPlaybackAutoRepeatMode.NONE: "off",
            _wm.MediaPlaybackAutoRepeatMode.TRACK: "one",
            _wm.MediaPlaybackAutoRepeatMode.LIST: "all",
        }
        if mode in mapping:
            self.cmd_repeat(mapping[mode])

    def _on_shuffle_requested(self, _sender, args):
        on = getattr(args, "requested_shuffle_enabled", None)
        if on is not None:
            self.cmd_shuffle(on)

    def _on_legacy_play(self, _s, _e):
        self.cmd_play()

    def _on_legacy_pause(self, _s, _e):
        self.cmd_pause()

    def _on_legacy_toggle(self, _s, _e):
        self.cmd_toggle()

    def _on_legacy_next(self, _s, _e):
        self.cmd_next()

    def _on_legacy_prev(self, _s, _e):
        self.cmd_prev()

    # -- outbound: push player state to SMTC ------------------------------

    def _push_all(self):
        self._emit_state(self.player.state.get())
        self._emit_metadata(self.player.playing.get())
        self._emit_position(int(self.player.position_ms.get() or 0))
        self._emit_shuffle(bool(self.player.shuffle.get()))
        self._emit_repeat(str(self.player.repeat.get() or "off"))

    def _enqueue(self, fn, *args):
        """Run *fn* on the SMTC dispatcher thread (or inline for legacy)."""
        queue = self._dispatcher
        if queue is not None:
            try:
                if queue.try_enqueue(lambda: _safe(fn, *args)):
                    return
            except Exception:
                pass
        _safe(fn, *args)

    def _emit_state(self, state):
        self._enqueue(self._apply_status, state)

    def _apply_status(self, state):
        if self._controls is not None:
            self._controls.playback_status = self._status_enum(state)
        if self._legacy:
            _wm.MediaControl.is_playing = state in ("playing", "paused")

    def _emit_metadata(self, song):
        self._enqueue(self._apply_metadata, song)

    def _apply_metadata(self, song):
        title = str(getattr(song, "title", None) or "")
        artist = str(getattr(song, "artist", None)
                     or getattr(song, "display_artist", None) or "")
        album = str(getattr(song, "album", None) or "")
        if self._updater is not None:
            updater = self._updater
            updater.type = _wm.MediaPlaybackType.MUSIC
            updater.music_properties.title = title
            updater.music_properties.artist = artist
            updater.music_properties.album_title = album
            updater.update()
        if self._legacy:
            mc = _wm.MediaControl
            mc.track_name = title
            mc.artist_name = artist
            mc.album_name = album

    def _emit_position(self, ms, seeked=False):
        self._enqueue(self._apply_timeline, ms, seeked)

    def _apply_timeline(self, ms, seeked=False):
        if self._controls is None:
            return
        dur = int(self.player.duration_ms.get() or 0)
        props = _wm.SystemMediaTransportControlsTimelineProperties()
        props.start_time = datetime.timedelta(0)
        props.end_time = datetime.timedelta(milliseconds=dur)
        props.position = datetime.timedelta(milliseconds=max(0, int(ms)))
        props.min_seek_time = datetime.timedelta(0)
        props.max_seek_time = datetime.timedelta(milliseconds=dur)
        self._controls.update_timeline_properties(props)

    def _emit_volume(self, _v):
        pass  # SMTC exposes no volume surface

    def _emit_shuffle(self, on):
        self._enqueue(self._apply_shuffle, on)

    def _apply_shuffle(self, on):
        if self._controls is not None:
            self._controls.shuffle_enabled = bool(on)

    def _emit_repeat(self, mode):
        self._enqueue(self._apply_repeat, mode)

    def _apply_repeat(self, mode):
        if self._controls is not None:
            self._controls.auto_repeat_mode = {
                "off": _wm.MediaPlaybackAutoRepeatMode.NONE,
                "one": _wm.MediaPlaybackAutoRepeatMode.TRACK,
                "all": _wm.MediaPlaybackAutoRepeatMode.LIST,
            }.get(str(mode or "off"), _wm.MediaPlaybackAutoRepeatMode.NONE)

    @staticmethod
    def _status_enum(state):
        return {
            "playing": _wm.MediaPlaybackStatus.PLAYING,
            "paused": _wm.MediaPlaybackStatus.PAUSED,
            "stopped": _wm.MediaPlaybackStatus.STOPPED,
            "completed": _wm.MediaPlaybackStatus.STOPPED,
        }.get(str(state or ""), _wm.MediaPlaybackStatus.STOPPED)


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception as e:
        log.debug("SMTC update failed: %s", e)
