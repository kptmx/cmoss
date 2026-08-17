"""cmoss — a Flet client for OpenSubsonic media servers.

Run:  .venv/bin/python -m cmoss
"""
import atexit
import logging
import sys

import flet as ft

from .config import load_config
from .store import Store
from .ui.screens import Root

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("libopensonic").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


def main(page: ft.Page):
    page.title = "OpenSubsonic"
    page.window.width = 1100
    page.window.height = 600
    page.window.frameless = True
    page.padding = ft.Padding(0, 0, 0, 0)
    page.theme_mode = ft.ThemeMode.DARK
    sb = ft.ScrollbarTheme(thumb_visibility=False, thickness=0)
    page.theme = ft.Theme(scrollbar_theme=sb)
    page.dark_theme = ft.Theme(scrollbar_theme=sb)

    page.bgcolor = ft.Colors.SURFACE_CONTAINER_LOW
    # Animate ThemeData (color_scheme_seed) changes, e.g. when the dynamic
    # theme is extracted from the current track's cover.
    page.theme_animation_style = ft.AnimationStyle(
        duration=ft.Duration(milliseconds=400),
        curve=ft.AnimationCurve.EASE_IN_OUT,
    )

    store = Store(load_config())
    store.attach(page)

    async def _on_window_event(e: ft.WindowEvent):
        if e.type == ft.WindowEventType.CLOSE:
            # Tear down (incl. mpv) while the Flet event loop is still
            # running: python-mpv callbacks crash with SIGSEGV if they read
            # properties after the interpreter starts finalizing.
            page.window.prevent_close = False
            store.shutdown()
            await page.window.close()
        elif e.type == ft.WindowEventType.MAXIMIZE:
            store.window_maximized.set(True)
        elif e.type == ft.WindowEventType.UNMAXIMIZE:
            store.window_maximized.set(False)

    page.window.prevent_close = True
    page.window.on_event = _on_window_event
    atexit.register(store.shutdown)

    page.render(Root, store)

    # The window starts hidden (AppView.FLET_APP_HIDDEN). Show it only after
    # frameless/titlebar config has been flushed by the render update above,
    # otherwise the native frame flashes for the time it takes the first
    # patch to reach the client.
    page.window.visible = True
    page.update()

    if store.config.is_complete():
        store.boot()


def _boost_priority():
    """Keep the process's audio thread schedulable under load.

    Windows silently drops the priority of processes it considers background
    (unfocused window), which starves mpv's audio thread and causes the
    stutter. Disabling the dynamic boost and going a notch above normal makes
    the audio thread win scheduling against unrelated CPU load (and against
    Flet's own UI work, which runs at the process's base priority)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        proc = k.GetCurrentProcess()
        k.SetProcessPriorityBoost(proc, True)   # don't drop priority in background
        k.SetPriorityClass(proc, 0x8000)        # ABOVE_NORMAL_PRIORITY_CLASS
    except Exception:
        pass


if __name__ == "__main__":
    _boost_priority()
    ft.run(main=main, view=ft.AppView.FLET_APP_HIDDEN)