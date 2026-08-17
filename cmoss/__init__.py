"""OpenSubsonic fawe client — layered: config/auth → proxy → server → player → store → ui."""

from .config import Config, load_config, save_config, parse_server_input
from . import auth
from .proxy import ProxyServer
from .server import Server, ServerError
from .player import PlayerModel
from .store import Store

__version__ = "0.1.0"

__all__ = [
    "Config",
    "load_config",
    "save_config",
    "parse_server_input",
    "auth",
    "ProxyServer",
    "Server",
    "ServerError",
    "PlayerModel",
    "Store",
]
