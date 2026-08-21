"""Persistent server configuration for the OpenSubsonic client.

Stored as JSON in ~/.config/cmoss/config.json (0600 perms).
The password is required to mint fresh salt/token pairs (Subsonic token
auth uses a random salt per request), so it is kept locally like any
desktop Subsonic client does.
"""
import json
import os
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

APP_NAME = "cmoss"
API_VERSION = "1.16.1"

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "cmoss")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "cmoss")


@dataclass
class Config:
    server: str = "http://localhost"
    port: int | None = None  # None → scheme default (80 http / 443 https)
    server_path: str = ""
    username: str = ""
    password: str = ""
    api_key: str = ""
    legacy_auth: bool = False
    app_name: str = APP_NAME
    api_version: str = API_VERSION
    cache_dir: str = CACHE_DIR
    max_cache_bytes: int = 2 * 1024 * 1024 * 1024
    panel_width: int = 320
    track_toast: bool = True  # show a notification when a track starts
    check_updates: bool = True  # check for updates on startup

    def is_complete(self) -> bool:
        return bool(self.username and (self.password or self.api_key))

    def server_display(self) -> str:
        host = urlsplit(self.server).hostname or self.server
        if self.port:
            host = f"{host}:{self.port}"
        return f"{self.username}@{host}"

    @classmethod
    def parse(cls, text: str) -> "Config":
        server, p, path = parse_server_input(text)
        return cls(server=server, port=p, server_path=path)

    @classmethod
    def from_dict(cls, d: dict):
        known = set(cls.__dataclass_fields__)
        data = {k: v for k, v in (d or {}).items() if k in known}
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)


def parse_server_input(text: str) -> tuple[str, int | None, str]:
    """Normalise a user-typed server string into (scheme://host, port, path).

    The port is only set when the user types it explicitly; when absent it is
    left `None` and resolved to the scheme default (80/443) at connection time,
    so no port is ever invented for the user.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Server URL is empty")
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    u = urlsplit(text)
    if not u.hostname:
        raise ValueError(f"Can't parse server URL: {text!r}")
    scheme = u.scheme or "http"
    server = f"{scheme}://{u.hostname}"
    return server, u.port, u.path.strip("/")


def effective_port(cfg: Config) -> int:
    """The concrete port to connect on: the user-specified one if set, else the
    scheme's standard port (443 for https, 80 otherwise)."""
    if cfg.port:
        return cfg.port
    scheme = urlsplit(cfg.server).scheme
    return 443 if scheme == "https" else 80


def load_config() -> Config:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return Config.from_dict(json.load(f))
        except Exception:
            pass
    return Config()


def save_config(cfg: Config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
