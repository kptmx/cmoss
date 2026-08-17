"""Lyrics: synced (LRC) and plain lyric fetching.

Primary source is LRCLIB — a free, open, token-less database that supports
synchronised lyrics in the LRC format. If LRCLIB has no match we fall back to
scraping Genius' public web pages (no API token required, using the same
web endpoints the Genius frontend itself calls).
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = "cmoss/0.1"

LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
GENIUS_SEARCH = "https://genius.com/api/search/multi"

_LRC_TIME = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


@dataclass
class LyricLine:
    text: str
    time_ms: int | None = None  # None for unsynced / untimed lines


@dataclass
class Lyrics:
    lines: list[LyricLine]
    synced: bool
    source: str
    title: str = ""
    artist: str = ""


def parse_lrc(text: str) -> list[LyricLine]:
    """Parse an LRC document into lines with millisecond timestamps.

    Supports ``[mm:ss]``, ``[mm:ss.x]``, ``[mm:ss.xx]`` and ``[mm:ss.xxx]``
    plus several timestamps per line. Lines without a timestamp keep their
    position relative to the timed ones.
    """
    out: list[LyricLine] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        hits = list(_LRC_TIME.finditer(line))
        if not hits:
            out.append(LyricLine(line))
            continue
        body = _LRC_TIME.sub("", line).strip()
        for m in hits:
            ms = (int(m.group(1)) * 60 + int(m.group(2))) * 1000
            frac = m.group(3)
            if frac:
                ms += int(frac.ljust(3, "0")[:3])
            out.append(LyricLine(body, ms))
    return out


def active_line_index(lyrics, position_ms: int) -> int:
    """Index of the line playing at ``position_ms``, or -1 if unsynced.

    `lyrics` may also be a sentinel string ("loading") or `None` — treat any
    value without a `.synced` attribute as "no lyrics yet".
    """
    if not getattr(lyrics, "synced", False):
        return -1
    idx = -1
    for i, line in enumerate(lyrics.lines):
        if line.time_ms is not None and line.time_ms <= position_ms:
            idx = i
    return idx


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(),
                                   (b or "").lower()).ratio()


# -- LRCLIB ----------------------------------------------------------------

def _from_lrclib(data: dict) -> "Lyrics | None":
    if not data:
        return None
    synced = data.get("syncedLyrics")
    plain = data.get("plainLyrics")
    text = synced or plain
    if not text:
        return None
    return Lyrics(
        lines=parse_lrc(text),
        synced=bool(synced),
        source="lrclib",
        title=data.get("trackName") or "",
        artist=data.get("artistName") or "",
    )


async def _lrclib_get(session, title, artist, album=None, duration=None):
    params = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(duration)
    try:
        async with session.get(LRCLIB_GET, params=params) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception as e:
        log.debug("lrclib get failed: %s", e)
        return None
    return _from_lrclib(data)


async def _lrclib_search(session, title, artist, duration=None):
    params = {"q": f"{artist} {title}"}
    if duration:
        params["duration"] = int(duration)
    try:
        async with session.get(LRCLIB_SEARCH, params=params) as r:
            if r.status != 200:
                return None
            items = await r.json()
    except Exception as e:
        log.debug("lrclib search failed: %s", e)
        return None
    best, best_score = None, 0.0
    for item in items or []:
        if not (item.get("syncedLyrics") or item.get("plainLyrics")):
            continue
        score = 0.6 * _ratio(item.get("trackName") or "", title) + \
                0.4 * _ratio(item.get("artistName") or "", artist)
        if score > best_score:
            best_score, best = score, item
    if best is None or best_score < 0.35:
        return None
    return _from_lrclib(best)


# -- Genius (token-less web scraper) ----------------------------------------

def _genius_song_results(payload: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for section in (payload.get("response", {}).get("sections", []) or []):
        for hit in (section.get("hits", []) or []):
            res = hit.get("result") or {}
            url = res.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(res)
    return out


def _genius_artist(res: dict) -> str:
    primary = res.get("primary_artist") or {}
    return res.get("artist_names") or primary.get("name") or ""


async def _genius_page(session, result: dict) -> "Lyrics | None":
    url = result.get("url")
    if not url:
        return None
    try:
        async with session.get(url) as r:
            if r.status != 200:
                return None
            html = await r.text()
    except Exception as e:
        log.debug("genius page fetch failed: %s", e)
        return None
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select('div[data-lyrics-container="true"]') \
        or soup.select('div[class*="LyricsContainer"]') \
        or soup.select("div.lyrics")
    if not containers:
        return None
    lines = []
    for container in containers:
        for raw in container.get_text("\n").split("\n"):
            text = raw.strip()
            if text:
                lines.append(text)
    if not lines:
        return None
    return Lyrics(
        lines=[LyricLine(t) for t in lines],
        synced=False,
        source="genius",
        title=result.get("title") or "",
        artist=_genius_artist(result),
    )


async def _genius(session, title, artist) -> "Lyrics | None":
    params = {"q": f"{artist} {title}", "per_page": 5}
    try:
        async with session.get(GENIUS_SEARCH, params=params) as r:
            if r.status != 200:
                return None
            payload = await r.json()
    except Exception as e:
        log.debug("genius search failed: %s", e)
        return None
    results = _genius_song_results(payload)
    best, best_score = None, 0.0
    for res in results:
        score = 0.6 * _ratio(res.get("title") or "", title) + \
                0.4 * _ratio(_genius_artist(res), artist)
        if score > best_score:
            best_score, best = score, res
    if best is None or best_score < 0.35:
        return None
    return await _genius_page(session, best)


# -- service ----------------------------------------------------------------

class LyricsService:
    """Owns a shared aiohttp session and a per-song result cache."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, Lyrics | None] = {}
        self._inflight: set[str] = set()

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": USER_AGENT},
            )
        return self._session

    @staticmethod
    def _key(song) -> str:
        return getattr(song, "id", None) or \
            f"{getattr(song, 'artist', '') or ''}|{getattr(song, 'title', '') or ''}"

    async def fetch(self, song) -> "Lyrics | None":
        """Cached or live lookup for `song`; None if unavailable."""
        if song is None:
            return None
        key = self._key(song)
        if key in self._cache:
            return self._cache[key]
        if key in self._inflight:
            return None
        self._inflight.add(key)
        try:
            lyrics = await self._fetch(song)
            self._cache[key] = lyrics
            return lyrics
        finally:
            self._inflight.discard(key)

    async def _fetch(self, song):
        title = (song.title or "").strip()
        artist = (song.artist or "").strip()
        if not title or not artist:
            return None
        session = await self._sess()
        lyr = await _lrclib_get(session, title, artist,
                                getattr(song, "album", None) or None,
                                getattr(song, "duration", None) or None)
        if lyr is not None:
            return lyr
        lyr = await _lrclib_search(session, title, artist,
                                   getattr(song, "duration", None) or None)
        if lyr is not None:
            return lyr
        return await _genius(session, title, artist)

    def clear(self):
        self._cache.clear()

    async def close(self):
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
