"""PlayerModel — queue + playback control driven by declarative `DataBox`es.

Uses python-mpv (libmpv) directly instead of the previous fawe wrapper. mpv
property observers fire on mpv's own thread; they only call ``DataBox.set()``,
which is safe because the Flet session scheduler is thread-safe.
"""
from __future__ import annotations

import functools
import logging
import random
import sys
import time

from .reactive import DataBox

# python-mpv finds libmpv via ctypes.util.find_library() (system PATH) or
# next to mpv.py itself.  Neither covers the PyInstaller extraction dir or
# a dev-layout with winmpv/.  Prepend the likely DLL location to PATH so
# ctypes.CDLL can resolve it at import time.
def _ensure_mpv_path() -> None:
    import os
    candidates: list[str] = []
    if getattr(sys, "frozen", False):
        candidates.append(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "winmpv"))
    for d in candidates:
        dll = os.path.join(d, "libmpv-2.dll")
        if not os.path.isfile(dll):
            continue
        # os.add_dll_directory (3.8+) is the reliable way on Windows;
        # PATH is a fallback for older interpreters / Wine edge cases.
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(d)
        existing = os.environ.get("PATH", "")
        if d not in existing:
            os.environ["PATH"] = d + os.pathsep + existing
        break

_ensure_mpv_path()

try:
    import mpv as pythonmpv
    _MPV_AVAILABLE = True
except Exception:
    pythonmpv = None
    _MPV_AVAILABLE = False

log = logging.getLogger(__name__)

# mpv_end_file_reason (mpv/client.h)
_END_FILE_EOF = 0
_END_FILE_ERROR = 4


def fmt_ms(ms: int | None) -> str:
    if not ms or ms < 0:
        return "0:00"
    s = int(ms / 1000)
    return f"{s // 60}:{s % 60:02d}"


class MpvPlayer:
    """Thin adapter over python-mpv exposing mpv.mpy's MediaPlayer interface."""

    def __init__(self):
        # Audio resilience under CPU load. Default audio-buffer is 0.2 s — a
        # scheduler hiccup of that size underruns the AO and stutters. Bumping
        # it makes mpv keep more audio queued ahead of playback, so brief CPU
        # starvation produces no gap. video=no drops the video-decode path
        # entirely (this is an audio-only app).
        opts = {"audio_buffer": 0.7, "video": "no"}
        log.info("mpv init: opts=%s", opts)
        self.player = pythonmpv.MPV(**opts)
        self._state = "stopped"
        self._duration_ms = 0
        self._url = None
        self._title = None
        self._pending_position = 0
        self._shutdown = False
        self._active = False
        self._pending = False
        self.on_state_change = None
        self.on_position_change = None
        self.on_duration_change = None
        self.on_loaded = None

        self._observers = {
            "time-pos": self._on_pos,
            "duration": self._on_dur,
            "pause": self._on_reconcile,
            "core-idle": self._on_reconcile,
        }
        for name, callback in self._observers.items():
            self.player.observe_property(name, callback)
        # Real end-of-file signal. mpv's core-idle goes True both at EOF and
        # while a stream is simply stalled (rebuffering, slow download), so
        # EOF must come from the end-file event instead of the idle heuristic.
        self.player.event_callback("end_file")(self._on_end_file)

    # -- mpv end-file events (mpv thread) -----------------------------------

    def _on_end_file(self, event):
        if self._shutdown:
            return
        try:
            reason = event.data.reason if event.data is not None else None
        except Exception:
            reason = None
        self._pending = False
        self._active = False
        if reason == _END_FILE_EOF:
            self._set_state("completed")
        elif reason == _END_FILE_ERROR:
            log.warning("mpv: playback error (end-file reason=error)")
            self._set_state("stopped")
            try:
                self.store.show_toast("Playback error", seconds=4.0)
            except Exception:
                pass

    # -- python-mpy property observers (mpv thread) -------------------------

    def _on_pos(self, _name, value):
        if self._shutdown:
            return
        if value is None:
            return
        try:
            ms = int(float(value) * 1000)
        except (TypeError, ValueError):
            return
        if self.on_position_change:
            self.on_position_change(ms)
        self._reconcile()

    def _on_dur(self, _name, value):
        if self._shutdown:
            return
        if value is None:
            return
        try:
            ms = int(float(value) * 1000)
        except (TypeError, ValueError):
            return
        self._duration_ms = ms
        if self._pending_position and ms > 0:
            self._apply_seek(self._pending_position)
            self._pending_position = 0
        if self.on_duration_change:
            self.on_duration_change(ms)

    def _on_reconcile(self, _name, _value):
        self._reconcile()

    def _apply_seek(self, ms):
        try:
            self.player.seek(ms / 1000.0, reference="absolute")
        except Exception as e:
            log.debug("seek during load failed: %s", e)

    def _reconcile(self):
        if self._shutdown:
            return
        try:
            idle = bool(self.player.core_idle)
            pause = bool(self.player.pause)
        except Exception:
            return
        if pause:
            # mpv keeps core-idle=True while paused, so pause must take
            # precedence over the idle branches: otherwise a paused file is
            # misread as stopped/completed, and toggle() would restart it later.
            st = "paused"
        elif not idle:
            # A file is actively decoding — either a pending load finished or
            # playback resumed.
            self._pending = False
            self._active = True
            st = "playing"
        elif self._pending:
            # A new file was requested but hasn't started decoding yet. mpv
            # keeps core-idle=True during that window, so it must not be read
            # as EOF — otherwise auto-advance double-skips.
            st = "stopped"
        else:
            # Idle while a file is loaded but not decoding. This can mean real
            # EOF, but it can equally mean the stream is stalled waiting for
            # data (slow network, seek beyond the buffered bytes). EOF is
            # reported by the end-file event, so never auto-advance here.
            st = "stopped"
        self._set_state(st)

    def _set_state(self, st):
        if self._shutdown:
            return
        if st != self._state:
            self._state = st
            if self.on_state_change:
                self.on_state_change(st)

    # -- MediaPlayer-like interface ---------------------------------------

    @property
    def src(self):
        return self._url

    @src.setter
    def src(self, url):
        self._url = url

    @property
    def metadata(self):
        return {"title": self._title}

    @metadata.setter
    def metadata(self, m):
        self._title = (m or {}).get("title")

    @property
    def state(self):
        return self._state

    @property
    def duration(self):
        return self._duration_ms

    @property
    def position(self):
        try:
            t = self.player.time_pos
            return int((t or 0) * 1000)
        except Exception:
            return 0

    @property
    def volume(self):
        try:
            return self.player.volume / 100.0
        except Exception:
            return 0.7

    @volume.setter
    def volume(self, v):
        try:
            self.player.volume = int(max(0.0, min(100.0, v * 100.0)))
        except Exception:
            pass

    def play(self, position_ms: int = 0):
        self._state = "stopped"
        self._pending = True
        if self._title:
            try:
                self.player.title = self._title
            except Exception:
                pass
        log.info("mpv play: url=%s", self._url)
        try:
            self.player.play(self._url)
            self.player.pause = False
        except Exception as e:
            log.warning("mpv play failed: %s", e)
            self._pending = False
            self._set_state("stopped")
            self.store.show_toast(f"Play failed: {e}", seconds=4.0)
            return
        self._pending_position = max(0, int(position_ms or 0))
        if self._pending_position and self._duration_ms > 0:
            self._apply_seek(self._pending_position)
            self._pending_position = 0
        if self.on_loaded:
            self.on_loaded()

    def pause(self):
        try:
            self.player.pause = True
        except Exception:
            pass

    def resume(self):
        try:
            self.player.pause = False
        except Exception:
            pass

    def stop(self):
        self._pending = False
        self._active = False
        try:
            self.player.stop()
        except Exception:
            pass
        self._set_state("stopped")

    def seek(self, position_ms: int):
        try:
            self.player.seek(position_ms / 1000.0, reference="absolute")
        except Exception as e:
            log.warning("mpv seek failed: %s", e)

    def release(self):
        """Safely tear down libmpv.

        python-mpv's `terminate()` nulls `self.handle` before destroying the
        context, so any property-observer callback that is still in flight will
        read a NULL handle and SIGSEGV inside libmpv. To close the window:
        flag shutdown, unregister all observers, stop playback and drain the
        event thread for a beat — only then destroy the handle."""
        try:
            self._shutdown = True
            for name, callback in self._observers.items():
                try:
                    self.player.unobserve_property(name, callback)
                except Exception:
                    pass
            try:
                self.player.stop()
            except Exception:
                pass
            time.sleep(0.05)
            self.player.terminate()
        except Exception as e:
            log.warning("mpv release failed: %s", e)


class NullPlayer:
    """Fallback when libmpv is unavailable — lets the UI run unchanged."""

    on_state_change = None
    on_position_change = None
    on_duration_change = None
    on_loaded = None

    def __init__(self):
        self._state = "stopped"
        self._volume = 0.7
        self._url = None

    @property
    def src(self):
        return self._url

    @src.setter
    def src(self, v):
        self._url = v

    @property
    def metadata(self):
        return {}

    @metadata.setter
    def metadata(self, v):
        pass

    @property
    def state(self):
        return self._state

    @property
    def duration(self):
        return 0

    @property
    def position(self):
        return 0

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, v):
        self._volume = v

    def play(self, position=0):
        self._state = "playing"

    def pause(self):
        self._state = "paused"

    def resume(self):
        self._state = "playing"

    def stop(self):
        self._state = "stopped"

    def seek(self, position_mss):
        pass

    def release(self):
        pass


class PlayerModel:
    """Queue + playback control, all fields observable `DataBox`es."""

    REPEAT_MODES = ("off", "all", "one")

    def __init__(self, store):
        self.store = store
        if _MPV_AVAILABLE:
            try:
                self.player = MpvPlayer()
            except Exception as e:
                log.error("MpvPlayer init failed, using NullPlayer: %s", e)
                self.player = NullPlayer()
                try:
                    self.store.show_toast(f"mpv init failed: {e}", seconds=5.0)
                except Exception:
                    pass
        else:
            log.warning("libmpv unavailable, using NullPlayer")
            self.player = NullPlayer()
            try:
                self.store.show_toast("libmpv unavailable", seconds=5.0)
            except Exception:
                pass

        self.queue: list = []
        self.index: int = -1
        self._history: list[int] = []
        self._scrobbled = False
        self._muted_volume = None

        self.state = DataBox("stopped")
        self.position_ms = DataBox(0)
        self.duration_ms = DataBox(0)
        self.volume = DataBox(0.7)
        self.progress = DataBox(0.0)
        self.time_label = DataBox("0:00")
        self.dur_label = DataBox("0:00")
        self.playing = DataBox(None)
        self.playing_index = DataBox(-1)
        self.queue_rev = DataBox(0)
        self.cover_url = DataBox(None)
        self.shuffle = DataBox(False)
        self.repeat = DataBox("off")
        self.error = DataBox(None)

        self._position_emit_interval = 0.1   # ~10 Hz cap on position-driven UI updates
        self._last_position_emit = 0.0

        self.player.volume = 0.7
        self.player.on_state_change = self._on_state
        self.player.on_position_change = self._on_position
        self.player.on_duration_change = self._on_duration
        self.player.on_loaded = self._on_loaded

    # -- event plumbing ----------------------------------------------------

    @property
    def current(self):
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def _on_state(self, state):
        if self.store._shutdown_done:
            return
        self.state.set(state)
        if state == "completed":
            self._on_track_ended()

    def _on_position(self, ms):
        if self.store._shutdown_done:
            return
        dur = self.duration_ms.get() or int(self.player.duration or 0)
        frac = (ms / dur) if (dur and dur > 0) else 0.0
        # mpv emits time-pos far faster than the UI (or SMTC/MPRIS) can
        # consume it; each box set ends in a patch message to the client.
        # Throttle to ~10 Hz, flushing immediately on a seek so the bar snaps.
        prev = self.position_ms.get() or 0
        seeked = abs(ms - prev) > 3000
        now = time.monotonic()
        if not seeked and now - self._last_position_emit < self._position_emit_interval:
            return
        self._last_position_emit = now
        self.position_ms.set(ms)
        self.time_label.set(fmt_ms(ms))
        self.progress.set(max(0.0, min(1.0, frac)))

    def _on_duration(self, ms):
        if self.store._shutdown_done:
            return
        self.duration_ms.set(int(ms))
        self.dur_label.set(fmt_ms(ms))

    def _on_loaded(self):
        pass

    # -- queue control -----------------------------------------------------

    def _bump_queue(self):
        self.queue_rev.set(self.queue_rev.get() + 1)

    def play_queue(self, songs, index: int = 0, position: int = 0):
        if not songs:
            return
        self.queue = list(songs)
        self._history.clear()
        self._bump_queue()
        self.play_index(index, position)

    def play_index(self, index: int, position: int = 0):
        if not self.queue or not (0 <= index < len(self.queue)):
            return
        song_new = self.queue[index]
        old = self.playing.get()
        if old is not None and getattr(old, "id", None) != getattr(song_new, "id", None):
            self._maybe_submit(old)
        if 0 <= self.index < len(self.queue) and self.index != index:
            self._history.append(self.index)
        self.index = index
        song = self.queue[index]
        self.playing.set(song)
        self.playing_index.set(index)
        self._scrobbled = False

        cover = self.store.cover_url_for(song) if self.store else None
        self.cover_url.set(cover)
        notify = getattr(self.store, "notify_song", None)
        if notify is not None:
            notify(song)

        self.player.metadata = {
            "title": song.title or "",
            "artist": song.artist or "",
            "album": song.album or "",
            "url": getattr(song, "id", ""),
            "art_url": cover or "",
        }
        try:
            self.player.src = self.store.stream_url(song.id)
        except Exception as e:
            self.error.set(f"Proxy not ready: {e}")
            return
        self.position_ms.set(0)
        self.time_label.set("0:00")
        self.duration_ms.set(0)
        self.dur_label.set("0:00")
        self.progress.set(0.0)
        self.player.play(position)
        self._scrobble_nowplaying()

    def next(self):
        if not self.queue:
            return
        if self.index < 0:
            self.play_index(0)
            return
        n = len(self.queue)
        if self.repeat.get() == "one":
            self.play_index(self.index)
            return
        if self.shuffle.get():
            candidates = [i for i in range(n) if i != self.index]
            if not candidates:
                self.play_index(self.index)
            else:
                self.play_index(random.choice(candidates))
            return
        nxt = self.index + 1
        if nxt >= n:
            if self.repeat.get() == "all":
                self.play_index(0)
            else:
                self._stop_to_end()
            return
        self.play_index(nxt)

    def prev(self):
        if not self.queue:
            return
        if self.position_ms.get() > 3000 or self.player.state in ("stopped", "completed"):
            self.seek(0)
            return
        if self._history:
            self.play_index(self._history.pop())
            return
        if self.index > 0:
            self.play_index(self.index - 1)
        else:
            self.seek(0)

    def _stop_to_end(self):
        try:
            self.player.stop()
        except Exception:
            pass
        self.playing.set(None)
        self.playing_index.set(-1)
        self.cover_url.set(None)

    # -- queue editing -----------------------------------------------------

    def add_to_queue(self, songs):
        songs = [s for s in (songs or []) if s]
        if not songs:
            return
        if not self.queue:
            self.play_queue(songs, 0)
            return
        self.queue.extend(songs)
        self._bump_queue()

    def add_next(self, songs):
        songs = [s for s in (songs or []) if s]
        if not songs:
            return
        if not self.queue:
            self.play_queue(songs, 0)
            return
        self.queue[self.index + 1:self.index + 1] = songs
        self._bump_queue()

    def remove(self, i: int):
        if not (0 <= i < len(self.queue)):
            return
        was_current = i == self.index
        self.queue.pop(i)
        self._history.clear()
        if was_current:
            if not self.queue:
                self._stop_to_end()
            else:
                self.play_index(min(i, len(self.queue) - 1))
        else:
            if i < self.index:
                self.index -= 1
            self.playing_index.set(self.index)
        self._bump_queue()

    def move(self, from_i: int, to_i: int):
        n = len(self.queue)
        if from_i == to_i or not (0 <= from_i < n and 0 <= to_i < n):
            return
        item = self.queue.pop(from_i)
        self.queue.insert(to_i, item)
        if from_i == self.index:
            self.index = to_i
        elif from_i < self.index <= to_i:
            self.index -= 1
        elif to_i <= self.index < from_i:
            self.index += 1
        self.playing_index.set(self.index)
        self._history.clear()
        self._bump_queue()

    def jump(self, i: int):
        if not (0 <= i < len(self.queue)):
            return
        if i == self.index and self.player.state in ("playing", "paused"):
            return
        self.play_index(i)

    def clear(self):
        self._history.clear()
        self.queue = []
        self.index = -1
        try:
            self.player.stop()
        except Exception:
            pass
        self.playing.set(None)
        self.playing_index.set(-1)
        self.cover_url.set(None)
        self._bump_queue()

    def _on_track_ended(self):
        if self.current is not None:
            self._scrobble_submit(self.current)
        self.next()

    def toggle(self):
        st = self.player.state
        if st == "playing":
            self.pause()
        elif st == "paused":
            self.resume()
        elif self.current is not None:
            self.play_index(self.index, int(self.position_ms.get()))
        else:
            self.resume()

    def pause(self):
        self.player.pause()
        self._scrobble_nowplaying("paused")

    def resume(self):
        if self.player.state == "paused":
            self.player.resume()
            self._scrobble_nowplaying("playing")
        elif self.current is not None:
            self.play_index(self.index, int(self.position_ms.get()))
            self._scrobble_nowplaying("playing")

    def stop(self):
        self._maybe_submit(self.playing.get())
        self._scrobble_nowplaying("stopped", self.position_ms.get() or 0)
        self.player.stop()
        self.playing.set(None)
        self.playing_index.set(-1)

    def seek(self, position_ms: int):
        position_ms = max(0, int(position_ms))
        self.position_ms.set(position_ms)
        self.time_label.set(fmt_ms(position_ms))
        try:
            self.player.seek(position_ms)
        except Exception as e:
            log.warning("seek failed: %s", e)
        self._scrobble_nowplaying(self.player.state, position_ms)

    def seek_fraction(self, frac: float):
        dur = self.duration_ms.get()
        if dur and dur > 0:
            self.seek(int(dur * max(0.0, min(1.0, frac))))

    # -- volume ----------------------------------------------------------

    def set_volume(self, v: float):
        v = max(0.0, min(1.0, v))
        self.player.volume = v
        self.volume.set(v)

    def toggle_mute(self):
        if self.volume.get() > 0:
            self._muted_volume = self.volume.get()
            self.set_volume(0.0)
        else:
            self.set_volume(self._muted_volume or 0.7)

    # -- shuffle / repeat ------------------------------------------------

    def toggle_shuffle(self):
        self.shuffle.set(not self.shuffle.get())

    def cycle_repeat(self):
        modes = self.REPEAT_MODES
        cur = self.repeat.get()
        self.repeat.set(modes[(modes.index(cur) + 1) % len(modes)])

    def set_repeat(self, mode: str):
        if mode in self.REPEAT_MODES:
            self.repeat.set(mode)

    # -- scrobble --------------------------------------------------------

    def _maybe_submit(self, song):
        """Submit a real scrobble for `song` if a meaningful portion played.

        Natural end-of-track already submits via `_on_track_ended`; this covers
        manual skips / stops / picking another song, when `eof` is never hit.
        The `_scrobbled` flag prevents double-submitting the same track."""
        if not song or not self.store or self._scrobbled:
            return
        dur = self.duration_ms.get()
        pos = self.position_ms.get()
        frac = (pos / dur) if (dur and dur > 0) else 0.0
        if frac < 0.6 and pos < 120_000:
            return
        self._scrobble_submit(song)

    def _scrobble_nowplaying(self, state: str = "playing",
                             position_ms: int | None = None):
        song = self.current
        if not song or not self.store:
            return
        if position_ms is None:
            position_ms = self.position_ms.get() or 0
        # `reportPlayback` pins the position server-side as
        # `positionMs + elapsed_time` while playing, so a single report per
        # playback event (play / resume / pause / seek / stop) is enough —
        # re-sending periodically would just re-pin the estimate.

        async def _do():
            try:
                await self.store.server.set_now_playing(
                    song.id, state=state, position_ms=position_ms)
            except Exception as e:
                log.debug("now playing update failed: %s", e)

        self.store.run(_do())

    def _scrobble_submit(self, song):
        if not song or not self.store or self._scrobbled:
            return
        self._scrobbled = True

        async def _do():
            try:
                await self.store.server.scrobble(song.id, submission=True,
                                                 listen_time=int(time.time()))
            except Exception as e:
                log.debug("scrobble submit failed: %s", e)

        self.store.run(_do())

    # -- lifecycle -------------------------------------------------------

    def release(self):
        try:
            self.player.release()
        except Exception as e:
            log.warning("player release failed: %s", e)