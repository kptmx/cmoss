"""Building authenticated OpenSubsonic endpoint URLs.

Mirrors libopensonic's auth scheme so the caching proxy can construct
upstream URLs with the same credentials:
  * api_key — `apiKey` query param;
  * token auth — random `s` salt + `t = md5(password + salt)`;
  * legacy — `p = enc:<hex(password)>`.
"""
import hashlib
import os
from urllib.parse import urlencode

from .config import Config, effective_port


def hex_encode(s: str) -> str:
    return "".join(f"{ord(c):02X}" for c in s)


def get_salt(length: int = 16) -> str:
    return hashlib.md5(os.urandom(100)).hexdigest()[:length]


def base_query(cfg: Config) -> dict:
    q = {"f": "json", "v": cfg.api_version, "c": cfg.app_name}
    if cfg.api_key:
        q["apiKey"] = cfg.api_key
    else:
        q["u"] = cfg.username
        if cfg.legacy_auth:
            q["p"] = "enc:" + hex_encode(cfg.password)
        else:
            salt = get_salt()
            q["s"] = salt
            q["t"] = hashlib.md5((cfg.password + salt).encode("utf-8")).hexdigest()
    return q


def endpoint_url(cfg: Config, method: str, **params) -> str:
    """Absolute URL for an OpenSubsonic REST endpoint (GET, .view suffix)."""
    q = base_query(cfg)
    q.update(params)
    base = f"{cfg.server}:{effective_port(cfg)}"
    path = cfg.server_path.strip("/")
    if path:
        base += "/" + path
    return f"{base}/rest/{method}.view?{urlencode(q)}"


def stream_url(cfg: Config, sid: str, max_bit_rate: int = 0) -> str:
    params = {"id": sid}
    if max_bit_rate:
        params["maxBitRate"] = max_bit_rate
    return endpoint_url(cfg, "stream", **params)


def cover_art_url(cfg: Config, aid: str, size: int | None = None) -> str:
    params = {"id": aid}
    if size:
        params["size"] = size
    return endpoint_url(cfg, "getCoverArt", **params)
