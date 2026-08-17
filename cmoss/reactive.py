"""Reactive primitive for the declarative UI: a thread-safe observable cell.

`DataBox` is a minimal ``flet.Observable`` that carries a value in ``.value``
and re-renders every `@component` subscribed via ``use_state(box)`` whenever
`set()` is called. Callbacks from the mpv polling thread (or any worker
thread) may call ``set()`` freely — Flet's session scheduler is thread-safe.
"""
from __future__ import annotations

from typing import Any

from flet import Observable
from flet.controls.context import _context_page

_PAGE = None
_RT_LOOP = None


def configure(page, loop):
    """Bind the reactive layer to the running Flet page/loop so DataBox
    notifications get marshalled onto the page loop with its context set."""
    global _PAGE, _RT_LOOP
    _PAGE = page
    _RT_LOOP = loop


class DataBox(Observable):
    """An observable container of a single value.

    Read with ``.get()``, write with ``.set(value)``. Subscribing a component
    is done by calling ``use_state(box)`` inside a `@flet.component` body —
    the box notifying its subscribers triggers a background re-render.
    """

    def __init__(self, value: Any = None):
        object.__setattr__(self, "value", value)

    def get(self) -> Any:
        return object.__getattribute__(self, "value")

    def set(self, value: Any) -> None:
        object.__setattr__(self, "value", value)
        self._emit()

    def _emit(self):
        """Notify subscribers, marshalling onto the Flet page loop so that
        Flet's page context (a ContextVar) is populated for the notification.

        mpv's property observers fire on mpv's own background thread, where the
        page context var is unset; broadcasting there would make `Component`.
        failure after `set()` from the Flet loop occurs.
        """
        global _PAGE, _RT_LOOP
        page, loop = _PAGE, _RT_LOOP
        if loop is not None and page is not None and loop.is_running():
            def force():
                try:
                    _context_page.set(page)
                except Exception:
                    pass
                self.notify()
            loop.call_soon_threadsafe(force)
        else:
            self.notify()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"DataBox({self.get()!r})"


class DerivedBox(DataBox):
    """A `DataBox` that re-computes its value from source boxes and only
    notifies when the derived value actually changes.

    This decouples cheap, high-frequency source updates (e.g. the player's
    per-tick ``position_ms``) from expensive UI re-renders: a component that
    subscribes to a `DerivedBox` re-renders only when the mapped value (e.g.
    the active lyric line index) differs, not on every source tick.
    """

    def __init__(self, fn, *sources: DataBox):
        super().__init__(fn(*[s.get() for s in sources]))
        self._fn = fn
        self._sources = sources
        # `subscribe` holds listeners weakly, so the disposers (which close
        # over the bound `_on_source` method) must be kept alive here,
        # otherwise the subscriptions are garbage-collected immediately.
        self._disposers = []
        for source in sources:
            try:
                self._disposers.append(source.subscribe(self._on_source))
            except Exception:
                pass

    def _on_source(self, _sender, _field):
        try:
            value = self._fn(*[s.get() for s in self._sources])
        except Exception:
            return
        if value != self.get():
            self.set(value)