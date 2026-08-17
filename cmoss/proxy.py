"""Local caching HTTP proxy — fully transparent for mpv and fawe Image.

Flow: mpv / Image request `http://127.0.0.1:<port>/stream/<id>` (or
`/cover/<id>/<size>`). The proxy fetches the bytes from the OpenSubsonic
server through `py-opensonic` (`AsyncConnection.stream()` /
`get_cover_art()`), streams them to the client *while* writing them to
the on-disk cache at the same time (tee). On a later request the data is
served straight from disk.

* HTTP Range is supported, so mpv seeking works.
* A single background download is shared between concurrent clients of
  the same id (and continues to completion even after all clients hang
  up, so the track is cached for next time).
* The downloader is resilient: a stalled/dropped connection is retried
  (resuming from the last cached byte via an HTTP Range request) instead
  of killing the streams being served to clients.
* LRU eviction keeps the cache under a size budget.
"""
import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
from libopensonic import AsyncConnection

from .config import Config, effective_port

log = logging.getLogger(__name__)

CHUNK = 64 * 1024
WAIT_TIMEOUT = 0.5

# Downloads are shared by the client streams; give a stalled remote a long
# leash (libopensonic defaults to a 60 s socket read timeout, which kills the
# cache download — and therefore playback — on slow/flaky connections).
_DOWNLOAD_SOCK_READ = 300.0
_DOWNLOAD_MAX_ATTEMPTS = 4


class _Retry(Exception):
    """Internal signal: restart the download attempt."""


class CacheEntry:
    __slots__ = (
        "key", "kind", "value", "size", "path", "meta_path", "cond", "thread",
        "total", "downloaded", "complete", "failed", "mime",
        "refs", "last_access",
    )

    def __init__(self, key, kind, value, size, path, meta_path, default_mime):
        self.key = key
        self.kind = kind
        self.value = value
        self.size = size
        self.path = path
        self.meta_path = meta_path
        self.cond = threading.Condition()
        self.thread = None
        self.total = None
        self.downloaded = 0
        self.complete = False
        self.failed = False
        self.mime = default_mime
        self.refs = 0
        self.last_access = time.time()


def _parse_range(header: str | None, total: int | None):
    """Return (start, end, is_partial). start/end are byte offsets."""
    if not header:
        return 0, None, False
    m = re.match(r"bytes=(\d*)-(\d*)", header.strip())
    if not m:
        return 0, None, False
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return 0, None, False
    if a == "":
        n = int(b)
        if total is None or n <= 0:
            return 0, None, False
        return max(0, total - n), total - 1, True
    start = int(a)
    end = int(b) if b else None
    return start, end, True


class ProxyServer:
    def __init__(self, cache_dir: str, max_bytes: int = 2 * 1024**3):
        self.cache_dir = cache_dir
        self.max_bytes = max_bytes
        self._config: Config | None = None
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._httpd = None
        self.port = 0
        self._key_prefix = ""

    # -- lifecycle -----------------------------------------------------

    def configure(self, cfg: Config):
        self._config = cfg
        self._key_prefix = f"{cfg.server}:{effective_port(cfg)}"
        os.makedirs(self.cache_dir, exist_ok=True)
        if self._httpd is None:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, proxy=self))
            httpd.daemon_threads = True
            self.port = httpd.server_address[1]
            self._httpd = httpd
            threading.Thread(target=httpd.serve_forever, daemon=True, name="proxy-server").start()
            log.info("Cache proxy listening on 127.0.0.1:%d (cache dir %s)", self.port, self.cache_dir)
        return self.port

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def clear_entries(self):
        """Drop all in-memory cache entries (used when the cache is wiped)."""
        with self._lock:
            self._entries.clear()

    # -- public URLs exposed to mpv / Image -----------------------------

    def stream_url(self, sid: str) -> str:
        return f"http://127.0.0.1:{self.port}/stream/{sid}"

    def cover_url(self, aid: str, size: int | None = None) -> str:
        base = f"http://127.0.0.1:{self.port}/cover/{aid}"
        return f"{base}/{size}" if size else base

    def cached_cover_path(self, aid: str, size: int | None = None) -> str | None:
        """Absolute path of a fully cached cover, or None if not cached yet."""
        value = f"{aid}/{size}" if size else aid
        key = f"{self._key_prefix}/cover/{value}"
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                return entry.path if entry.complete else None
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        path = os.path.join(self.cache_dir, "cover", digest + ".bin")
        meta = self._load_meta(path + ".json")
        if meta and meta.get("complete") and os.path.exists(path):
            return path
        return None

    # -- internals ------------------------------------------------------

    def _resolve(self, kind: str, value: str) -> tuple[str, str, int | None, str]:
        """Return (kind, value, size, default_mime) for a request."""
        if kind == "stream":
            return "stream", value, None, "application/octet-stream"
        if kind == "cover":
            parts = value.split("/")
            aid, size = parts[0], (parts[1] if len(parts) > 1 else None)
            if size and not size.isdigit():
                raise ValueError(f"bad cover size {size!r}")
            return "cover", aid, int(size) if size else None, "image/jpeg"
        raise ValueError(f"unknown proxy kind {kind!r}")

    def get_entry(self, kind: str, value: str) -> CacheEntry | None:
        key = f"{self._key_prefix}/{kind}/{value}"
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                try:
                    rkind, rvalue, size, default_mime = self._resolve(kind, value)
                except Exception as e:
                    log.warning("proxy: cannot resolve %s/%s: %s", kind, value, e)
                    return None
                digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
                subdir = os.path.join(self.cache_dir, kind)
                os.makedirs(subdir, exist_ok=True)
                path = os.path.join(subdir, digest + ".bin")
                meta_path = path + ".json"
                entry = CacheEntry(key, rkind, rvalue, size, path, meta_path, default_mime)
                meta = self._load_meta(meta_path)
                if meta and meta.get("complete") and os.path.exists(path):
                    entry.total = meta.get("size")
                    entry.downloaded = entry.total or 0
                    entry.complete = True
                    entry.mime = meta.get("mime") or default_mime
                    try:
                        os.utime(path)
                    except OSError:
                        pass
                else:
                    self._ensure_started(entry)
                self._entries[key] = entry
            entry.refs += 1
            entry.last_access = time.time()
            return entry

    def release_entry(self, entry: CacheEntry):
        with self._lock:
            entry.refs = max(0, entry.refs - 1)

    def _ensure_started(self, entry: CacheEntry):
        with entry.cond:
            if entry.complete or entry.failed:
                return
            if entry.thread is None or not entry.thread.is_alive():
                entry.thread = threading.Thread(
                    target=self._download, args=(entry,), daemon=True,
                    name=f"proxy-dl-{entry.key[:20]}",
                )
                entry.thread.start()

    def _load_meta(self, meta_path: str) -> dict | None:
        try:
            with open(meta_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _write_meta(self, entry: CacheEntry):
        meta = {
            "complete": True,
            "size": entry.total,
            "mime": entry.mime,
            "key": entry.key,
            "ts": time.time(),
        }
        try:
            with open(entry.meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f)
        except OSError as e:
            log.warning("proxy: meta write failed: %s", e)

    def _download(self, entry: CacheEntry):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._download_async(entry))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("proxy: download failed for %s: %s", entry.key, e)
            with entry.cond:
                entry.failed = True
                entry.cond.notify_all()
        finally:
            loop.close()
            if entry.failed and not entry.complete:
                # Nothing more to serve or resume from — drop the partial file
                # so the next request for this id starts a clean download.
                try:
                    if os.path.exists(entry.path):
                        os.remove(entry.path)
                except OSError:
                    pass
                with self._lock:
                    if self._entries.get(entry.key) is entry:
                        del self._entries[entry.key]

    async def _download_async(self, entry: CacheEntry):
        """Download a stream, retrying on failures so a slow or flaky network
        does not kill the cache write — and therefore the streams currently
        being served to clients (mpv).

        A retry resumes from the last byte already written by asking the remote
        for an HTTP Range (`bytes=<downloaded>-`) and appending to the cache
        file, so the data mpv has already received is never re-read.
        """
        cfg = self._config
        attempt = 0
        while True:
            attempt += 1
            conn = AsyncConnection(
                cfg.server,
                username=cfg.username,
                password=cfg.password,
                api_key=cfg.api_key or None,
                port=effective_port(cfg),
                server_path=cfg.server_path,
                app_name=cfg.app_name,
                api_version=cfg.api_version,
                legacy_auth=cfg.legacy_auth,
                use_get=True,
            )
            try:
                # libopensonic's default 60 s socket read timeout aborts the
                # download — and playback — when a chunk takes a while to
                # arrive; give it a long leash instead.
                conn._timeout = aiohttp.ClientTimeout(
                    total=None, sock_connect=30, sock_read=_DOWNLOAD_SOCK_READ)
                resumed = entry.downloaded > 0
                try:
                    if entry.kind == "stream":
                        if resumed:
                            resp = await conn.stream(
                                entry.value,
                                byte_range=f"bytes={entry.downloaded}-")
                        else:
                            resp = await conn.stream(entry.value)
                    else:
                        resp = await conn.get_cover_art(
                            entry.value, size=entry.size)
                except Exception as e:
                    log.warning("proxy: request failed for %s (attempt %d): %s",
                                entry.key, attempt, e)
                    if attempt >= _DOWNLOAD_MAX_ATTEMPTS:
                        break
                    await asyncio.sleep(min(0.5 * (2 ** attempt), 6))
                    continue
                if resumed and resp.status == 200:
                    # The server ignored the Range header and sent the body from
                    # byte 0 — restart the cache file instead of appending
                    # duplicate bytes.
                    log.warning("proxy: server ignored Range for %s; restarting",
                                entry.key)
                    try:
                        with open(entry.path, "wb"):
                            pass
                    except OSError:
                        pass
                    with entry.cond:
                        entry.downloaded = 0
                        entry.total = None
                        entry.cond.notify_all()
                    resumed = False
                ctype = resp.headers.get("Content-Type")
                length = resp.headers.get("Content-Length")
                with entry.cond:
                    if ctype:
                        entry.mime = ctype
                    if length and not resumed:
                        try:
                            entry.total = int(length)
                        except ValueError:
                            pass
                    entry.cond.notify_all()
                try:
                    await self._write_body(entry, resp)
                except _Retry:
                    if attempt >= _DOWNLOAD_MAX_ATTEMPTS:
                        log.warning("proxy: giving up on %s after %d attempts",
                                    entry.key, attempt)
                        break
                    log.warning("proxy: download interrupted for %s (attempt %d); "
                                "retrying from byte %d",
                                entry.key, attempt, entry.downloaded)
                    await asyncio.sleep(min(0.5 * (2 ** attempt), 6))
                    continue
                with entry.cond:
                    entry.complete = True
                    if entry.total is None or entry.downloaded < entry.total:
                        entry.total = entry.downloaded
                    entry.cond.notify_all()
                self._write_meta(entry)
                self._maybe_evict()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("proxy: download failed for %s (attempt %d): %s",
                            entry.key, attempt, e)
                if attempt >= _DOWNLOAD_MAX_ATTEMPTS:
                    break
                await asyncio.sleep(min(0.5 * (2 ** attempt), 6))
            finally:
                try:
                    await conn.cleanup()
                except Exception:
                    pass
        with entry.cond:
            entry.failed = True
            entry.cond.notify_all()

    async def _write_body(self, entry: CacheEntry, resp):
        """Tee `resp`'s body into the cache file, advancing `entry.downloaded`.

        Raises `_Retry` if the remote connection dies partway through; the
        bytes written so far stay on disk so the next attempt can resume.
        """
        try:
            f = open(entry.path, "ab" if entry.downloaded > 0 else "wb")
        except OSError:
            raise _Retry from None
        try:
            async for chunk in resp.content.iter_chunked(CHUNK):
                if not chunk:
                    continue
                f.write(chunk)
                f.flush()
                with entry.cond:
                    entry.downloaded += len(chunk)
                    entry.cond.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _Retry
        finally:
            try:
                f.close()
            except OSError:
                pass
            try:
                resp.release()
            except Exception:
                pass

    def _maybe_evict(self):
        try:
            total = 0
            for root, _dirs, files in os.walk(self.cache_dir):
                for fn in files:
                    if fn.endswith(".bin"):
                        try:
                            total += os.path.getsize(os.path.join(root, fn))
                        except OSError:
                            pass
            if total <= self.max_bytes:
                return
            with self._lock:
                candidates = [e for e in self._entries.values() if e.complete and e.refs == 0]
            candidates.sort(key=lambda e: self._mtime(e.path))
            for e in candidates:
                if total <= self.max_bytes:
                    break
                try:
                    sz = os.path.getsize(e.path)
                    os.remove(e.path)
                    if os.path.exists(e.meta_path):
                        os.remove(e.meta_path)
                    total -= sz
                except OSError:
                    continue
                with self._lock:
                    if self._entries.get(e.key) is e:
                        del self._entries[e.key]
                log.info("proxy: evicted %s (cache now ~%.1f MiB)", e.key, total / 1048576)
        except Exception as e:
            log.warning("proxy: eviction error: %s", e)

    @staticmethod
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "OpenSubsonicCache/1.0"

    def __init__(self, *args, proxy: ProxyServer, **kwargs):
        self.proxy = proxy
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        log.debug("proxy: %s %s", self.command, self.path)

    # -- routing ----------------------------------------------------------

    def do_HEAD(self):
        self._route(respond_body=False)

    def do_GET(self):
        self._route(respond_body=True)

    def _route(self, respond_body: bool):
        path = self.path.split("?", 1)[0].strip("/")
        parts = path.split("/")
        if parts and parts[0] == "health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if respond_body:
                self.wfile.write(body)
            return
        if len(parts) >= 2 and parts[0] in ("stream", "cover"):
            self._serve_entry(parts[0], "/".join(parts[1:]), respond_body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- cache entry serving ----------------------------------------------

    def _serve_entry(self, kind: str, value: str, respond_body: bool):
        entry = self.proxy.get_entry(kind, value)
        if entry is None:
            self.send_error(502, "cannot resolve entry")
            return
        try:
            self.proxy._ensure_started(entry)
            # wait until the first bytes arrive (or the download fails)
            deadline = time.time() + 90
            with entry.cond:
                while (not entry.failed and not entry.complete and entry.downloaded == 0
                       and time.time() < deadline):
                    entry.cond.wait(WAIT_TIMEOUT)
                if entry.failed:
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

            total = entry.total
            start, end, partial = _parse_range(self.headers.get("Range"), total)

            if partial and total is not None and start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if partial:
                status = 206
                if total is not None:
                    if end is None or end >= total:
                        end = total - 1
                    clen = end - start + 1
                    cr = f"bytes {start}-{end}/{total}"
                else:
                    clen = None
                    cr = f"bytes {start}-*/"
            else:
                status = 200
                clen = total
                cr = None

            self.send_response(status)
            self.send_header("Content-Type", entry.mime or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            if cr:
                self.send_header("Content-Range", cr)
            if clen is not None:
                self.send_header("Content-Length", str(clen))
            self.end_headers()

            if respond_body:
                self._stream(entry, start, end)
        finally:
            self.proxy.release_entry(entry)

    def _stream(self, entry: CacheEntry, start: int, end: int | None):
        try:
            f = open(entry.path, "rb")
        except OSError:
            return
        try:
            f.seek(start)
            pos = start
            while True:
                if end is not None and pos > end:
                    break
                with entry.cond:
                    while True:
                        if entry.failed:
                            return
                        if entry.complete and pos >= entry.downloaded:
                            return
                        if pos < entry.downloaded:
                            break
                        entry.cond.wait(WAIT_TIMEOUT)
                    avail = entry.downloaded - pos
                    if end is not None:
                        avail = min(avail, end - pos + 1)
                    if avail <= 0:
                        continue
                data = f.read(min(avail, CHUNK))
                if not data:
                    # bytes not flushed to disk yet — wait and retry
                    with entry.cond:
                        if entry.failed:
                            return
                    time.sleep(0.01)
                    continue
                self.wfile.write(data)
                pos += len(data)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log.debug("proxy: stream aborted: %s", e)
        finally:
            f.close()
