"""GitHub release checker and updater for cmoss.

Checks the GitHub Releases API for new versions, downloads the source ZIP,
and extracts only ``.py`` files (plus directory structure) into the local
update directory so the bootstrap can overlay them on next launch.

All I/O is async via ``aiohttp`` (already a project dependency).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import zipfile
from dataclasses import dataclass

import aiohttp

from .bootstrap import data_dir, update_dir

log = logging.getLogger(__name__)

REPO = "kptmx/cmoss"
_API = f"https://api.github.com/repos/{REPO}/releases/latest"
_ZIP = "https://github.com/{repo}/archive/refs/tags/{tag}.zip"


# ── version helpers ────────────────────────────────────────────────────

def parse_version(v: str) -> tuple[int, ...]:
    """``'v0.2.0'`` → ``(0, 2, 0)``.

    Strips a leading ``v``/``V`` and splits on ``'.'``.  Non-numeric
    segments are silently dropped so ``'1.0-rc1'`` → ``(1, 0)``.
    """
    v = v.lstrip("vV")
    out: list[int] = []
    for part in v.split("."):
        try:
            out.append(int(part))
        except ValueError:
            break
    return tuple(out)


@dataclass
class Release:
    tag: str
    name: str
    body: str  # markdown release notes


# ── network ────────────────────────────────────────────────────────────

async def get_latest_release() -> Release | None:
    """Fetch the latest release metadata from GitHub.

    Returns ``None`` on any network / JSON error so callers can treat it
    as "no update available".
    """
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_API) as resp:
                if resp.status != 200:
                    log.warning("GitHub API %s", resp.status)
                    return None
                data = await resp.json()
        return Release(
            tag=data["tag_name"],
            name=data.get("name") or data["tag_name"],
            body=data.get("body") or "",
        )
    except Exception:
        log.exception("Failed to fetch latest release")
        return None


async def download_update(
    tag: str,
    on_progress: callable[[int, int], None] | None = None,
) -> bool:
    """Download the source ZIP for *tag* and extract ``.py`` files.

    Parameters
    ----------
    tag:
        Git tag (e.g. ``"v0.2.0"``).
    on_progress:
        Optional ``on_progress(bytes_read, total_bytes)`` callback.
        *total_bytes* may be ``-1`` if the server doesn't send
        ``Content-Length``.

    Returns ``True`` on success.
    """
    url = _ZIP.format(repo=REPO, tag=tag)
    dest = update_dir()

    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.warning("ZIP download failed: HTTP %s", resp.status)
                    return False

                total = int(resp.headers.get("Content-Length", -1))
                buf = io.BytesIO()
                read = 0
                async for chunk in resp.content.iter_chunked(65536):
                    buf.write(chunk)
                    read += len(chunk)
                    if on_progress:
                        on_progress(read, total)

        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            # ZIP entries start with ``<repo-tag>/`` prefix — strip it.
            prefix = None
            for info in zf.infolist():
                parts = info.filename.split("/", 1)
                if len(parts) > 1 and prefix is None:
                    prefix = parts[0] + "/"
                    break

            for info in zf.infolist():
                name = info.filename
                if prefix and name.startswith(prefix):
                    name = name[len(prefix):]
                if not name or name.endswith("/"):
                    continue
                if not name.endswith(".py"):
                    continue
                target = os.path.join(dest, name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())

        log.info("Update %s installed to %s", tag, dest)
        return True
    except Exception:
        log.exception("Failed to download update %s", tag)
        return False


def restart() -> None:
    """Replace the current process with a fresh launch.

    Uses ``os.execv`` so the new process inherits the same PID — this is
    the cleanest restart on both Linux and Windows.
    """
    os.execv(sys.executable, [sys.executable] + sys.argv)
