"""Bootstrap: redirect cmoss imports to an update dir when present.

When the app is frozen by PyInstaller, Python modules live inside the PYZ
archive and are loaded via a custom ``MetaPathFinder``.  To overlay updated
``.py`` files on top of the bundled ones we install *our own* finder at the
front of ``sys.meta_path`` so it wins the import race for every ``cmoss.*``
module that exists on disk in the update directory.

Usage from ``main.py``::

    from cmoss.bootstrap import bootstrap
    bootstrap()          # no-op when not frozen or no update present

    from cmoss.main import main   # now loads from update/ if available
    ft.run(main=main)
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import logging
import os
import sys
import types

log = logging.getLogger(__name__)

REPO = "kptmx/cmoss"


# ── platform data directory ────────────────────────────────────────────

def data_dir() -> str:
    """Return the per-user data directory for cmoss.

    * Linux / macOS: ``~/.local/share/cmoss``
    * Windows:       ``%LOCALAPPDATA%\\cmoss``
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "cmoss")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "cmoss")


def update_dir() -> str:
    """Return ``<data_dir>/update`` — the root of downloaded updates."""
    return os.path.join(data_dir(), "update")


# ── custom import finder / loader ──────────────────────────────────────

class _UpdateFinder(importlib.abc.MetaPathFinder):
    """Redirect ``cmoss.*`` imports to the on-disk update directory."""

    def __init__(self, root: str) -> None:
        self._root = root

    # importlib.abc.MetaPathFinder (Python ≥ 3.4)
    def find_module(  # type: ignore[override]
        self,
        fullname: str,
        path: object = None,
    ) -> _FileLoader | None:
        if not (fullname == "cmoss" or fullname.startswith("cmoss.")):
            return None

        parts = fullname.split(".")
        base = os.path.join(self._root, *parts)

        # package?
        init = os.path.join(base, "__init__.py")
        if os.path.isfile(init):
            return _FileLoader(fullname, init, is_package=True)

        # module?
        mod = os.path.join(
            os.path.join(self._root, *parts[:-1]),
            parts[-1] + ".py",
        ) if len(parts) > 1 else os.path.join(self._root, parts[0] + ".py")
        if os.path.isfile(mod):
            return _FileLoader(fullname, mod, is_package=False)

        return None


class _FileLoader(importlib.abc.Loader):
    """Load a single ``.py`` file from the update directory."""

    def __init__(self, fullname: str, path: str, is_package: bool) -> None:
        self.fullname = fullname
        self.path = path
        self.is_package = is_package

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        return None  # use default semantics

    def exec_module(self, module: types.ModuleType) -> None:
        if self.is_package:
            module.__path__ = [os.path.dirname(self.path)]

        with open(self.path, encoding="utf-8") as fh:
            source = fh.read()
        code = compile(source, self.path, "exec")
        exec(code, module.__dict__)  # noqa: S102


# ── public API ─────────────────────────────────────────────────────────

def bootstrap() -> None:
    """Install the update overlay finder when appropriate.

    Safe to call unconditionally — it is a no-op when:
    * the process is not frozen (development mode), or
    * no update has been downloaded yet (``update/cmoss/__init__.py``
      does not exist).
    """
    if not getattr(sys, "frozen", False):
        return

    init = os.path.join(update_dir(), "cmoss", "__init__.py")
    if not os.path.isfile(init):
        return

    # Purge any already-imported cmoss modules so the updated versions
    # are picked up on the next import.
    for key in list(sys.modules):
        if key == "cmoss" or key.startswith("cmoss."):
            del sys.modules[key]

    sys.meta_path.insert(0, _UpdateFinder(update_dir()))
    log.info("bootstrap: overlay active from %s", update_dir())
