"""Store — the app's reactive ViewModel.

Owns Server (async client) + PlayerModel, exposes observable `DataBox`es the
declarative UI subscribes to via `use_state(box)`, and implements navigation,
session boot, screen-data loading and toasts. Async side effects are
fire-and-forget, scheduled with `run()` on the Flet page event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time

import flet as ft

from .config import Config
from .lyrics import LyricsService
from .media_control import create_media_control
from .palette import extract_palette
from .player import PlayerModel
from .proxy import ProxyServer
from .reactive import DataBox, configure
from .server import Server, ServerError
from .updater import (download_update, get_latest_release, parse_version,
                      restart as _restart)

log = logging.getLogger(__name__)

PANEL_MIN_W = 240
PANEL_MAX_W = 560


class Store:
    def __init__(self, cfg: Config):
        self.config = cfg
        self.server = Server(cfg)
        self.proxy: ProxyServer | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set = set()
        self._shutdown_done = False

        self._page = None
        self._theme_seq = 0
        self._theme_lock = threading.Lock()
        self._theme_listener = None

        self.busy = DataBox(False)
        self.toast = DataBox([])
        self.toast_progress = DataBox({})  # toast_id -> remaining fraction (0..1)
        self._toast_seq = 0

        self.player = PlayerModel(self)
        self.media_control = None
        self.screen = DataBox("home")
        self.connected = DataBox(False)
        self.nav_counter = DataBox(0)
        self.panel = DataBox(None)
        self.panel_width = DataBox(cfg.panel_width)
        self._resize_start_x = 0.0
        self._resize_base_w = float(cfg.panel_width)
        self.window_maximized = DataBox(False)
        self.theme = DataBox(None)
        self.stack: list[tuple[str, dict]] = []
        self.albums: dict[str, DataBox] = {}
        self.songs: dict[str, DataBox] = {}
        self.playlist_songs: dict[str, DataBox] = {}
        self.albums_detail: dict[str, DataBox] = {}
        self.playlists = DataBox([])
        self.genres = DataBox([])
        self.artists = DataBox([])
        self.artist_detail = DataBox(None)
        self.starred = DataBox(None)
        self.starred_ids = DataBox(set())
        self.search = DataBox(None)
        self.lyrics = DataBox(None)
        self.lyrics_service = LyricsService()

        self.cur_payload: dict = {}
        self.cur_albums: str = "newest"
        self._notified_song_id = None

    # -- async scheduling -------------------------------------------------

    def attach(self, page):
        loop = page.loop
        self._loop = loop
        self._page = page
        configure(page, loop)
        # Hold a strong ref: Observable keeps listeners in a WeakSet, so a
        # bare bound method would be GC'd and never fire.
        self._theme_listener = self._on_theme
        self.theme.subscribe(self._theme_listener)

    def run(self, coro):
        """Fire-and-forget a coroutine on the Flet page event loop."""
        if self._loop is None or not self._loop.is_running():
            try:
                coro.close()
            except Exception:
                pass
            return
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        self._tasks.add(fut)
        fut.add_done_callback(self._tasks.discard)

    # -- boot ------------------------------------------------------------

    def boot(self):
        if self.media_control is None:
            self.media_control = create_media_control(self)
            self.media_control.start()
        self.busy.set(True)
        self.run(self._boot())

    async def _boot(self):
        try:
            await self.server.ping()
            self.proxy = ProxyServer(self.config.cache_dir, max_bytes=self.config.max_cache_bytes)
            self.proxy.configure(self.config)
            await self._load_screen("home")
            await self._load_playlists()
            await self._load_genres()
            await self._load_starred()
            self.screen.set("home")
            self.connected.set(True)
            self.show_toast(f"Connected to {self.config.server_display()}")
            self._check_update()
        except ServerError as e:
            self.show_toast(f"Login failed: {e}", seconds=5.0)
        except Exception as e:
            log.exception("boot failed")
            self.show_toast(f"Boot failed: {e}", seconds=5.0)
        finally:
            self.busy.set(False)
            if not self.connected.get():
                try:
                    await self.server.close()
                except Exception:
                    pass

    def connect(self, server: str, username: str, password: str,
                api_key: str, legacy_auth: bool = False):
        from .config import save_config

        try:
            cfg = Config.parse(server)
        except ValueError as e:
            self.show_toast(str(e))
            return
        cfg.username = username
        cfg.password = password
        cfg.api_key = api_key or None
        cfg.legacy_auth = legacy_auth
        cfg.panel_width = self.panel_width.get()
        self.config = cfg
        self.server = Server(cfg)
        save_config(cfg)
        self.boot()

    async def _wrap(self, coro):
        try:
            return await coro
        except ServerError as e:
            self.show_toast(f"Server error: {e}")
        except Exception as e:
            log.exception("request failed")
            self.show_toast(f"Request failed: {e}")
        return None

    # -- logout ---------------------------------------------------------

    def logout(self):
        """End the session: stop playback, switch back to the login screen,
        then tear down connections and purge account-bound state in the
        background."""
        self.player.stop()
        self.player.queue = []
        self.player.queue_rev.set(self.player.queue_rev.get() + 1)
        self.busy.set(False)
        self.screen.set("home")
        self.stack.clear()
        self.connected.set(False)
        self.theme.set(None)
        self.run(self._logout_teardown())

    async def _logout_teardown(self):
        from .config import save_config

        try:
            await self.server.close()
        except Exception:
            pass
        if self.proxy is not None:
            try:
                self.proxy.stop()
            except Exception:
                pass
            self.proxy = None
        if self.media_control is not None:
            try:
                self.media_control.close()
            except Exception:
                pass
            self.media_control = None
        cfg = self.config
        cfg.password = ""
        cfg.api_key = ""
        try:
            save_config(cfg)
        except Exception:
            pass
        for box in list(self.albums.values()):
            box.set([])
        for box in list(self.songs.values()):
            box.set([])
        for box in list(self.playlist_songs.values()):
            box.set([])
        for box in list(self.albums_detail.values()):
            box.set(None)
        self.artist_detail.set(None)
        self.starred.set(None)
        self.starred_ids.set(set())
        self.playlists.set([])
        self.genres.set([])
        self.artists.set([])
        self.search.set(None)
        self.lyrics.set(None)

    # -- navigation ------------------------------------------------------

    def _data(self, key):
        return self.albums.setdefault(key, DataBox([]))

    def _songs_for(self, key):
        return self.songs.setdefault(key, DataBox([]))

    def _album_detail_for(self, key):
        return self.albums_detail.setdefault(key, DataBox(None))

    def _playlist_songs_for(self, key):
        return self.playlist_songs.setdefault(key, DataBox([]))

    def go(self, screen: str, payload: dict | None = None):
        self.stack.append((self.screen.get(), dict(self.cur_payload)))
        self.cur_payload = payload or {}
        self.screen.set(screen)
        self.nav_counter.set(self.nav_counter.get() + 1)
        self.run(self._load_screen(screen))

    def go_artist(self, artist_id: str):
        self.go("artist", {"artist_id": artist_id})

    def go_album(self, album_id: str):
        self.go("album", {"album_id": album_id})

    def go_playlist(self, playlist_id: str):
        self.go("playlist", {"playlist_id": playlist_id})

    def go_genre(self, genre: str):
        self.go("genre", {"genre": genre})

    def go_search(self, query: str):
        self.go("search", {"query": query})

    def go_category(self, ltype: str):
        """Open the search screen showing every album of a home section type."""
        self.go("search", {"query": None, "category": ltype})

    def nav_top(self, screen: str):
        self.stack.clear()
        self.cur_payload = {}
        self.screen.set(screen)
        self.nav_counter.set(self.nav_counter.get() + 1)
        self.run(self._load_screen(screen))

    def back(self):
        if not self.stack:
            self.go("home")
            return
        prev, payload = self.stack.pop()
        self.screen.set(prev)
        self.cur_payload = payload or {}
        self.nav_counter.set(self.nav_counter.get() + 1)
        self.run(self._load_screen(prev))

    # -- screen data loading ---------------------------------------------

    async def _load_screen(self, screen: str):
        payload = self.cur_payload
        if screen == "home":
            await self._load_home_staged()
        elif screen == "random":
            await self._refresh_albums("random")
        elif screen == "playlists":
            await self._load_playlists()
        elif screen == "starred":
            await self._load_starred()
        elif screen == "genres":
            await self._load_genres()
        elif screen == "artists":
            await self._load_artists()
        elif screen == "search":
            q = payload.get("query")
            cat = payload.get("category")
            if q:
                res = await self._wrap(self.server.search3(q))
                self.search.set(res)
            elif cat:
                await self._load_category(cat)
        elif screen == "artist":
            await self._load_artist(payload.get("artist_id"))
        elif screen == "album":
            await self._load_album(payload.get("album_id"))
        elif screen == "playlist":
            await self._load_playlist(payload.get("playlist_id"))
        elif screen == "genre":
            await self._load_genre_songs(payload.get("genre"))

    # -- library ---------------------------------------------------------

    _HOME_STAGE_S = 0.08  # gap between home sections arriving (per-section diff)

    async def _load_home_staged(self):
        """Load the four home sections.

        All server calls run concurrently, but each section's `DataBox` is set
        in a separate event-loop turn (staggered by `_HOME_STAGE_S`) so every
        section is diffed and sent to the client on its own — the client fills
        the screen progressively instead of receiving ~600 controls in one
        giant patch, and the event loop is never blocked for the whole batch.
        """
        sections = (("newest", 60), ("recent", 30),
                    ("frequent", 30), ("random", 30))
        tasks = [
            asyncio.create_task(
                self._wrap(self.server.get_album_list2(lt, size=size)))
            for lt, size in sections
        ]
        for (lt, _size), task in zip(sections, tasks):
            res = await task
            if res is None:
                continue
            self._data(lt).set(res)
            self.cur_albums = lt
            await asyncio.sleep(self._HOME_STAGE_S)

    async def _refresh_albums(self, ltype: str, size: int = 60):
        res = await self._wrap(self.server.get_album_list2(ltype, size=size))
        if res is None:
            return
        self._data(ltype).set(res)
        self.cur_albums = ltype

    async def _load_artists(self):
        res = await self._wrap(self.server.get_artists())
        if res is not None:
            self.artists.set(res)

    async def _load_category(self, ltype: str):
        """Load the full album list for a home-section type into its own box
        (kept separate from the home sections so home keeps its short lists)."""
        res = await self._wrap(self.server.get_album_list2(ltype, size=500))
        if res is not None:
            self._data(f"cat:{ltype}").set(res)

    async def _load_artist(self, artist_id: str):
        res = await self._wrap(self.server.get_artist(artist_id))
        if res is None:
            return
        self.artist_detail.set(res)
        self._data("artist").set(list(res.album or []))

    async def _load_album(self, album_id: str):
        res = await self._wrap(self.server.get_album(album_id))
        if res is None:
            return
        self._album_detail_for(album_id).set(res)
        self._songs_for(album_id).set(list(res.song or []))

    async def _load_playlists(self):
        res = await self._wrap(self.server.get_playlists())
        if res is not None:
            self.playlists.set(res)

    async def _load_playlist(self, playlist_id: str):
        res = await self._wrap(self.server.get_playlist(playlist_id))
        if res is None:
            return
        self._playlist_songs_for(playlist_id).set(list(res.entry or []))

    async def _load_genre_songs(self, genre: str):
        res = await self._wrap(self.server.get_songs_by_genre(genre, count=200))
        if res is None:
            return
        self._songs_for(f"genre:{genre}").set(list(res))

    async def _load_starred(self):
        res = await self._wrap(self.server.get_starred2())
        if res is not None:
            self.starred.set(res)
            ids = set()
            for s in (res.song or []):
                ids.add(f"s:{s.id}")
            for a in (res.album or []):
                ids.add(f"a:{a.id}")
            self.starred_ids.set(ids)

    async def _load_genres(self):
        res = await self._wrap(self.server.get_genres())
        if res is not None:
            self.genres.set(res)

    # -- urls ------------------------------------------------------------

    def stream_url(self, song_id: str) -> str:
        if self.proxy is None:
            raise ServerError("proxy not started")
        return f"http://127.0.0.1:{self.proxy.port}/stream/{song_id}"

    def cover_url_for(self, item) -> str:
        if self.proxy is None:
            return None
        size = 300
        aid = getattr(item, "cover_art", None) or getattr(item, "id", None)
        return f"http://127.0.0.1:{self.proxy.port}/cover/{aid}/{size}"

    def cover_file_for(self, item) -> str | None:
        """Local cache path of the cover, if it is already fully cached."""
        if self.proxy is None:
            return None
        size = 300
        aid = getattr(item, "cover_art", None) or getattr(item, "id", None)
        return self.proxy.cached_cover_path(aid, size)

    # -- dynamic theme (from the current track's cover) -------------------

    def wait_cover(self, item, timeout: float = 30.0) -> str | None:
        """Block until the cover art for `item` is fully cached and return its
        local path (starting the download if needed)."""
        if self.proxy is None:
            return None
        aid = getattr(item, "cover_art", None) or getattr(item, "id", None)
        if not aid:
            return None
        path = self.proxy.cached_cover_path(aid, 300)
        if path:
            return path
        entry = self.proxy.get_entry("cover", f"{aid}/300")
        if entry is None:
            return None
        try:
            deadline = time.time() + timeout
            with entry.cond:
                while (not entry.complete and not entry.failed
                       and time.time() < deadline):
                    entry.cond.wait(0.2)
            return entry.path if entry.complete else None
        finally:
            self.proxy.release_entry(entry)

    def notify_song(self, song):
        """Kick off palette extraction + lyric lookup for a newly-started
        track. Runs on a daemon thread; a newer track supersedes an in-flight
        extraction via the sequence counter. Also raises a track toast when
        enabled in settings."""
        self.lyrics.set("loading" if song is not None else None)
        if song is not None:
            self.run(self._load_lyrics(song))
            if (self.config.track_toast
                    and getattr(song, "id", None) != self._notified_song_id):
                self._notified_song_id = getattr(song, "id", None)
                artist = getattr(song, "artist", "") or ""
                title = getattr(song, "title", "") or ""
                cover = self.cover_url_for(song) if self.proxy else None
                self.show_toast(f"{artist} — {title}" if artist else title,
                                seconds=4.0, cover=cover)
        if self.proxy is None or song is None:
            return
        with self._theme_lock:
            self._theme_seq += 1
            seq = self._theme_seq
        threading.Thread(target=self._theme_worker, args=(song, seq),
                         daemon=True, name="theme-extract").start()

    def _theme_worker(self, song, seq):
        try:
            path = self.wait_cover(song)
            if not path:
                return
            pal = extract_palette(path)
            if not pal:
                return
            with self._theme_lock:
                if seq != self._theme_seq:
                    return
            self.theme.set(pal)
        except Exception as e:
            log.debug("theme extraction failed: %s", e)

    async def _load_lyrics(self, song):
        """Fetch lyrics for `song` and publish them, unless a newer track
        already took over the box."""
        try:
            lyrics = await self.lyrics_service.fetch(song)
        except Exception as e:
            log.debug("lyrics fetch failed: %s", e)
            lyrics = None
        cur = self.player.playing.get()
        if cur is None:
            return
        if cur is song or getattr(cur, "id", None) == getattr(song, "id", None):
            self.lyrics.set(lyrics)

    def _on_theme(self, _sender, _field):
        self._apply_theme(self.theme.get())

    def _apply_theme(self, pal):
        page = self._page
        if page is None:
            return
        try:
            sb = ft.ScrollbarTheme(thumb_visibility=False, thickness=0)
            if pal:
                seed = pal.get("seed")
                page.theme = ft.Theme(color_scheme_seed=seed, scrollbar_theme=sb)
                page.dark_theme = ft.Theme(color_scheme_seed=seed,
                                           scrollbar_theme=sb)
            else:
                page.theme = ft.Theme(scrollbar_theme=sb)
                page.dark_theme = ft.Theme(scrollbar_theme=sb)
            page.update()
        except Exception as e:
            log.debug("theme apply failed: %s", e)

    # -- starred / ratings ------------------------------------------------

    def _set_starred(self, key: str, on: bool):
        ids = set(self.starred_ids.get())
        has = key in ids
        if on and not has:
            ids.add(key)
            self.starred_ids.set(ids)
        elif not on and has:
            ids.discard(key)
            self.starred_ids.set(ids)

    def is_starred(self, key: str) -> bool:
        return key in self.starred_ids.get()

    def _call_star(self, star: bool, sids=None, album_ids=None, artist_ids=None):
        async def _do():
            try:
                if star:
                    await self.server.star(sids=sids, album_ids=album_ids,
                                           artist_ids=artist_ids)
                else:
                    await self.server.unstar(sids=sids, album_ids=album_ids,
                                             artist_ids=artist_ids)
            except Exception as e:
                log.debug("star/unstar failed: %s", e)

        self.run(_do())

    def star_song(self, sid: str):
        self._set_starred(f"s:{sid}", True)
        self._call_star(True, sids=[sid])

    def unstar_song(self, sid: str):
        self._set_starred(f"s:{sid}", False)
        self._call_star(False, sids=[sid])

    def star_album(self, aid: str):
        self._set_starred(f"a:{aid}", True)
        self._call_star(True, album_ids=[aid])

    def unstar_album(self, aid: str):
        self._set_starred(f"a:{aid}", False)
        self._call_star(False, album_ids=[aid])

    # -- actions (fire-and-forget side effects) --------------------------

    def play(self, songs, index: int = 0):
        self.player.play_queue(songs, index)

    def play_shuffle(self, songs):
        if songs:
            self.play(songs, random.randrange(len(songs)))

    def add_to_queue(self, songs):
        self.player.add_to_queue(songs)

    def add_next(self, songs):
        self.player.add_next(songs)

    # -- side panel -------------------------------------------------------

    def toggle_panel(self, mode: str):
        self.panel.set(None if self.panel.get() == mode else mode)

    def set_panel_width(self, width: float):
        """Clamp and publish the right-panel width (live drag updates)."""
        w = int(max(PANEL_MIN_W, min(PANEL_MAX_W, width)))
        if w != self.panel_width.get():
            self.panel_width.set(w)

    def save_panel_width(self):
        """Persist the current panel width to the config file (drag end)."""
        from .config import save_config

        self.config.panel_width = self.panel_width.get()
        try:
            save_config(self.config)
        except Exception:
            pass

    # -- caching ----------------------------------------------------------

    def cache_stats(self):
        """Aggregate on-disk cache usage.

        Returns a dict with `files`/`bytes` totals plus per-kind counts and
        sizes for `stream` and `cover`.
        """
        stats = {"files": 0, "bytes": 0,
                 "streams": 0, "stream_bytes": 0,
                 "covers": 0, "cover_bytes": 0}
        root = self.config.cache_dir
        if not os.path.isdir(root):
            return stats
        for dirpath, _dirs, files in os.walk(root):
            kind = os.path.basename(dirpath)
            if kind not in ("stream", "cover"):
                continue
            for fn in files:
                if not fn.endswith(".bin"):
                    continue
                try:
                    sz = os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    continue
                stats["files"] += 1
                stats["bytes"] += sz
                if kind == "stream":
                    stats["streams"] += 1
                    stats["stream_bytes"] += sz
                else:
                    stats["covers"] += 1
                    stats["cover_bytes"] += sz
        return stats

    def clear_cache(self):
        """Delete all cached files and drop in-memory cache entries."""
        removed = 0
        root = self.config.cache_dir
        if os.path.isdir(root):
            for dirpath, _dirs, files in os.walk(root, topdown=False):
                for fn in files:
                    if fn.endswith((".bin", ".json")):
                        try:
                            os.remove(os.path.join(dirpath, fn))
                            removed += 1
                        except OSError:
                            pass
        if self.proxy is not None:
            self.proxy.clear_entries()
        self.show_toast(f"Cache cleared ({removed} files)")

    def set_cache_limit(self, megabytes: int):
        """Update the cache size budget (in MiB) and enforce it via eviction."""
        mib = max(0, int(megabytes))
        self.config.max_cache_bytes = mib * 1024 * 1024
        if self.proxy is not None:
            self.proxy.max_bytes = self.config.max_cache_bytes
            self.proxy._maybe_evict()
        from .config import save_config

        try:
            save_config(self.config)
        except Exception:
            pass
        self.show_toast(f"Cache limit set to {mib} MiB")

    def set_track_toast(self, value: bool):
        """Enable/disable the track-start notification and persist the choice."""
        self.config.track_toast = bool(value)
        from .config import save_config

        try:
            save_config(self.config)
        except Exception:
            pass

    def set_check_updates(self, value: bool):
        """Enable/disable update checking and persist the choice."""
        self.config.check_updates = bool(value)
        from .config import save_config

        try:
            save_config(self.config)
        except Exception:
            pass

    # -- toasts -----------------------------------------------------------

    def show_toast(self, msg: str, seconds: float = 3.0, cover: str | None = None,
                   persistent: bool = False, on_click=None, has_progress: bool = False,
                   dismissible: bool | None = None):
        """Push a notification onto the top-right stack.

        The card slides in once the client mounts it (`settle_toast`), and is
        dismissed after `seconds` — with a slide-out first, so removal is
        always animated. The countdown is driven by `_toast_clock` and pauses
        while the cursor hovers the card. `cover` (optional) is a thumbnail
        URL rendered at the left edge of the card.

        If *persistent* is ``True`` the countdown is not started and the toast
        stays on screen until explicitly dismissed.  *on_click* (optional) is
        a zero-argument callable invoked when the user clicks the card body."""
        self._toast_seq += 1
        if self._shutdown_done:
            return
        if dismissible is None:
            dismissible = not persistent
        toast = {"id": self._toast_seq, "msg": str(msg), "seconds": seconds,
                 "settled": False, "leaving": False,
                 "remaining": max(0.0, seconds), "paused": False,
                 "cover": cover, "persistent": persistent,
                 "on_click": on_click, "has_progress": has_progress,
                 "dismissible": dismissible}
        self.toast.set(list(self.toast.get() or []) + [toast])
        self._publish_toast_progress()
        if not persistent:
            self.run(self._toast_clock(toast["id"]))

    def settle_toast(self, toast_id: int):
        """Flip a toast to its settled position so the client slides it in.

        Called from the card's `on_mounted` hook, i.e. once the control is
        actually attached on the client — that guarantees the slide-in starts
        from the initial off-screen offset instead of teleporting in."""
        toasts = list(self.toast.get() or [])
        for t in toasts:
            if t["id"] == toast_id and not t.get("settled"):
                t["settled"] = True
                self.toast.set(toasts)
                return

    def set_toast_paused(self, toast_id: int, paused: bool):
        """Freeze/resume a toast's auto-dismiss countdown (hover pause).

        While paused the countdown stops AND the timer resets to the full
        duration, so the toast stays up as long as the cursor rests on it and
        restarts its countdown from scratch once the cursor leaves."""
        toasts = list(self.toast.get() or [])
        for t in toasts:
            if t["id"] == toast_id and t.get("paused") != paused:
                t["paused"] = paused
                if paused:
                    t["remaining"] = t.get("seconds", 0.0)
                self.toast.set(toasts)
                self._publish_toast_progress()
                return

    def dismiss_toast(self, toast_id: int):
        """Start the slide-out for a toast; it is removed once the animation
        has played out."""
        toasts = list(self.toast.get() or [])
        for t in toasts:
            if t["id"] == toast_id and not t.get("leaving"):
                t["leaving"] = True
                self.toast.set(toasts)
                self.run(self._remove_toast(toast_id))
                return

    async def _toast_clock(self, toast_id: int):
        """Tick the auto-dismiss countdown for one toast.

        Decrements the toast's `remaining` in place and publishes the live
        progress fraction through `toast_progress`, which the countdown bars
        subscribe to. Skips ticking while hover-paused or persistent; dismisses
        when the time runs out."""
        tick = 0.05  # 50 ms — smooth enough for the progress bar
        while True:
            await asyncio.sleep(tick)
            toasts = self.toast.get() or []
            t = next((x for x in toasts if x["id"] == toast_id), None)
            if t is None or t.get("leaving"):
                return
            if t.get("paused") or t.get("persistent"):
                continue
            t["remaining"] = max(0.0, t.get("remaining", 0.0) - tick)
            self._publish_toast_progress()
            if t["remaining"] <= 0:
                self.dismiss_toast(toast_id)
                return

    def _publish_toast_progress(self):
        """Push the live remaining-fraction for every active toast."""
        prog = {
            t["id"]: max(0.0, min(1.0, t["remaining"] / max(0.001, t.get("seconds", 0.0))))
            for t in (self.toast.get() or [])
            if not t.get("leaving")
        }
        self.toast_progress.set(prog)

    async def _remove_toast(self, toast_id: int):
        await asyncio.sleep(0.25)  # slide-out animation duration
        toasts = [t for t in (self.toast.get() or []) if t["id"] != toast_id]
        self.toast.set(toasts)

    def set_toast_progress(self, toast_id: int, fraction: float):
        """Manually set the progress fraction for a toast (0..1).

        Used for download-progress toasts whose progress is driven by bytes
        received rather than a time-based countdown."""
        toasts = list(self.toast.get() or [])
        for t in toasts:
            if t["id"] == toast_id:
                t["remaining"] = max(0.0, min(1.0, fraction))
                self.toast.set(toasts)
                self._publish_toast_progress()
                return

    def update_toast_msg(self, toast_id: int, msg: str):
        """Change the message text of an existing toast."""
        toasts = list(self.toast.get() or [])
        for t in toasts:
            if t["id"] == toast_id:
                t["msg"] = msg
                self.toast.set(toasts)
                return

    # -- auto-update ------------------------------------------------------

    def _check_update(self):
        """Fire-and-forget: check GitHub for a newer release."""
        self.run(self._do_check_update())

    async def _do_check_update(self):
        """Check GitHub releases; show a persistent toast if an update is
        available."""
        if not self.config.check_updates:
            return
        from . import __version__
        release = await get_latest_release()
        if release is None:
            return
        if parse_version(release.tag) <= parse_version(__version__):
            return
        self.show_toast(
            f"Update available: {release.tag}",
            persistent=True,
            dismissible=True,
            on_click=lambda r=release, tid=self._toast_seq:
                (self.dismiss_toast(tid), self._show_update_dialog(r)),
        )

    def _show_update_dialog(self, release):
        """Open the update confirmation dialog."""
        import flet as ft

        dialog = ft.AlertDialog(
            title=ft.Text(f"Update {release.tag}"),
            content=ft.Container(
                width=420, height=260,
                content=ft.Markdown(
                    release.body or "No release notes.",
                    selectable=True,
                    extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                ),
            ),
            actions=[
                ft.TextButton("Update", on_click=lambda e: (
                    self._page.pop_dialog(),
                    self._start_update(release.tag),
                )),
                ft.TextButton("Later", on_click=lambda e: (
                    self._page.pop_dialog(),
                )),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page.show_dialog(dialog)

    def _start_update(self, tag: str):
        """Begin downloading the update; show a progress toast."""
        self._update_toast_id = None
        self.run(self._do_update(tag))

    async def _do_update(self, tag: str):
        """Download and extract the update, then prompt restart."""
        self._update_toast_id = self._toast_seq + 1
        self.show_toast("Downloading update…", persistent=True, has_progress=True)

        def _progress(read: int, total: int):
            frac = read / total if total > 0 else 0.0
            self.set_toast_progress(self._update_toast_id, frac)

        ok = await download_update(tag, on_progress=_progress)
        if ok:
            self.update_toast_msg(
                self._update_toast_id,
                f"Update {tag} ready.",
            )
            # Switch the toast to a restart-prompt: replace on_click
            toasts = list(self.toast.get() or [])
            for t in toasts:
                if t["id"] == self._update_toast_id:
                    t["on_click"] = lambda: _restart()
                    t["cover"] = None
                    self.toast.set(toasts)
                    break
        else:
            self.update_toast_msg(self._update_toast_id, "Update failed.")
            # Auto-dismiss after 3 seconds: flip persistent and start clock
            toasts = list(self.toast.get() or [])
            for t in toasts:
                if t["id"] == self._update_toast_id:
                    t["persistent"] = False
                    t["has_progress"] = False
                    t["remaining"] = 3.0
                    t["seconds"] = 3.0
                    self.toast.set(toasts)
                    self.run(self._toast_clock(t["id"]))
                    break

    # -- lifecycle -------------------------------------------------------

    def shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        if self.media_control is not None:
            try:
                self.media_control.close()
            except Exception:
                pass
        try:
            self.player.release()
        except Exception:
            pass
        if self.proxy is not None:
            try:
                self.proxy.stop()
            except Exception:
                pass
        if self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.server.close(), self._loop)
            except Exception:
                pass
            try:
                asyncio.run_coroutine_threadsafe(self.lyrics_service.close(),
                                                 self._loop)
            except Exception:
                pass