"""Screens — declarative Flet components.

Each screen is a `@flet.component` subscribing to the `DataBox`es it renders
via `use_state(box)`. Any `box.set(...)` (including from worker threads such
as the mpv thread) triggers a background re-render of that component.
"""
from __future__ import annotations

import logging
import math
import threading

import flet as ft

from flet import component, memo, on_mounted, use_effect, use_memo, use_state

from ..lyrics import active_line_index
from ..reactive import DataBox, DerivedBox

log = logging.getLogger(__name__)
from .widgets import StarButton, album_grid, album_tile, fmt_dur, placeholder, song_list


def _prefill_server(c):
    host = c.server or "http://127.0.0.1"
    if c.port and f":{c.port}" not in host:
        return f"{host}:{c.port}"
    return host


# --------------------------------------------------------------------------
# Root / Login / layout
# --------------------------------------------------------------------------

@component
def TitleBar(store):
    use_state(store.window_maximized)
    maximized = store.window_maximized.get()
    page = ft.context.page

    def minimize(e):
        page.window.minimized = True
        page.update()

    def toggle_maximize(e):
        page.window.maximized = not maximized
        store.window_maximized.set(not maximized)
        page.update()

    async def close(e):
        exit(0)  # triggers atexit handlers, including store.shutdown()

    win_btn = lambda icon, tip, on_click, hover=None: ft.IconButton(
        icon=icon, icon_size=12, width=40, height=26, style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=0)),
        icon_color=ft.Colors.ON_SURFACE_VARIANT, hover_color=hover,
        tooltip=tip, on_click=on_click, padding=ft.Padding(0, 0, 0, 0),)

    return ft.Container(
        content=ft.Row(
            spacing=0,
            controls=[
                ft.WindowDragArea(
                    expand=True,
                    maximizable=True,
                    content=ft.Container(
                        expand=True,
                        height=26,
                        padding=ft.Padding.only(left=6),
                        content=ft.Row(
                            spacing=8,
                            controls=[
                                ft.Icon(ft.Icons.MUSIC_NOTE, size=15, width=12,
                                        color=ft.Colors.PRIMARY),
                                ft.Text("cmoss", size=13,
                                        weight=ft.FontWeight.BOLD),
                            ],
                        ),
                    ),
                ),
                win_btn(ft.Icons.MINIMIZE, "Minimize", minimize),
                win_btn(ft.Icons.FULLSCREEN_EXIT if maximized else ft.Icons.RECTANGLE_OUTLINED,
                        "Restore" if maximized else "Maximize", toggle_maximize),
                win_btn(ft.Icons.CLOSE, "Close", close,
                        hover=ft.Colors.ERROR),
            ], height=26
        ), bgcolor=ft.Colors.SURFACE_CONTAINER_LOW, border_radius=0
    )


# --------------------------------------------------------------------------
# DissolveSwitch — custom fade out / fade in (replaces ft.AnimatedSwitcher)
# --------------------------------------------------------------------------

_FADE_BUFFER_S = 0.04   # margin after a phase's animation completes
_MAX_LAYERS = 8         # keep-alive cache size (visited screens held mounted)


def _phase_s(duration):
    # one phase = half of the whole requested duration
    return max(0.1, duration / 2000.0)


def _hashable(v):
    try:
        hash(v)
        return True
    except TypeError:
        return False


def _layer_key(identity_value):
    return identity_value if _hashable(identity_value) else repr(identity_value)


def _run_fade(layer, opacity, state, target, duration, direction):
    """Animate the whole content's opacity: `out` 1.0→0.0, `in` 0.0→1.0.

    Each phase is a SINGLE opacity change animated by the client's implicit
    `animate_opacity` over the phase duration. A single uninterrupted animation
    completes at a predictable time, so the content swap (which must happen at
    true opacity 0 on screen) is scheduled only after the animation plus a
    settle buffer — never mid-fade. Timers are pure Python (no
    `on_animation_end`); stale ones from a superseded transition are no-ops
    because they check that `state` is still their own phase.
    """
    if state.get() != direction:
        return  # superseded by a newer transition
    phase = _phase_s(duration)

    if direction == "out":
        opacity.set(0.0)

        def swap():
            if state.get() != "out":
                return
            layer.set(target)
            state.set("in")
            _run_fade(layer, opacity, state, target, duration, "in")

        threading.Timer(phase + _FADE_BUFFER_S, swap).start()
    else:
        opacity.set(1.0)

        def done():
            if state.get() != "in":
                return
            state.set("idle")

        threading.Timer(phase + _FADE_BUFFER_S, done).start()


def _transition_to(layers, active, tick, state, old_key, new_key, duration):
    """Keep-alive fade: hide `old_key` layer, then reveal `new_key` layer.

    Both layers stay mounted in the stack; only their opacities animate. The
    new layer was already added to `layers` (opacity 0) by the render body.
    Timers are guarded by `state` so a superseded transition is a no-op.
    """
    phase = _phase_s(duration)

    def swap():
        if state.get() != "out":
            return
        if old_key in layers:
            layers[old_key]["op"] = 0.0
        active.set(new_key)
        if new_key in layers:
            layers[new_key]["op"] = 1.0
        tick.set(tick.get() + 1)
        state.set("in")

        def done():
            if state.get() != "in":
                return
            state.set("idle")

        threading.Timer(phase + _FADE_BUFFER_S, done).start()

    if old_key in layers:
        layers[old_key]["op"] = 0.0
    tick.set(tick.get() + 1)
    threading.Timer(phase + _FADE_BUFFER_S, swap).start()


def _fade_in_only(layers, key, tick, state, duration):
    """Fade a freshly mounted layer in from zero (`fade_in_on_mount`)."""
    phase = _phase_s(duration)

    def done():
        if state.get() != "in":
            return
        state.set("idle")

    if key in layers:
        layers[key]["op"] = 1.0
    tick.set(tick.get() + 1)
    threading.Timer(phase + _FADE_BUFFER_S, done).start()


def _cap_layers(layers, order, active_key, new_key, max_layers):
    """Evict the oldest hidden layer once the keep-alive cache is full."""
    while len(order) > max_layers:
        evicted = None
        for k in list(order):
            if k in (active_key, new_key):
                continue
            evicted = k
            break
        if evicted is None:
            break
        layers.pop(evicted, None)
        order.remove(evicted)


@component
def DissolveSwitch(boxes, identity, render, duration=250, fade_in_on_mount=False,
                   keep_alive=True, cache_key=None, max_layers=_MAX_LAYERS):
    """Fade the content out, swap to the target, then fade it back in.

    On navigation the current content first fades out completely to the window
    background, the target is swapped in, and the new content fades in. Each
    phase is a single opacity change animated by the client's `animate_opacity`
    (one uninterrupted animation completes at a predictable time, so the swap
    always happens at true opacity 0). Timing is deterministic and driven from
    Python, without `ft.AnimatedSwitcher` or `on_animation_end`. Theme changes
    never touch the identity, so they do not start this transition — Flutter
    animates theme color changes on its own.

    With `keep_alive=True` (default) every visited screen stays mounted as a
    hidden stacked layer: navigating back to a visited screen is just an
    opacity fade instead of tearing the screen down and re-adding hundreds of
    controls. Layers are LRU-capped at `max_layers`. `cache_key` (a callable
    returning a hashable value) identifies the route when `identity` embeds a
    changing counter (e.g. the navigation generation) — without it the cache
    key is the `identity()` value itself.

    With `keep_alive=False` the original single-layer replace-fade is used for
    panels whose content must be re-created from live state on every render.

    With `fade_in_on_mount=True` the content additionally fades in from zero
    right after the component mounts (used for freshly opened panels).
    """
    for b in boxes:
        use_state(b)
    state = use_state(DataBox("idle"))[0]
    use_state(state)

    if not keep_alive:
        layer = use_state(DataBox(identity()))[0]
        opacity = use_state(DataBox(1.0))[0]
        started = use_state(DataBox(False))[0]
        use_state(layer)
        use_state(opacity)
        use_state(started)

        cur = identity()
        if state.get() == "idle" and cur != layer.get():
            state.set("out")
            _run_fade(layer, opacity, state, cur, duration, "out")
        if fade_in_on_mount:
            if not started.get():
                started.set(True)
                opacity.set(0.0)
                state.set("in")
            on_mounted(lambda: _run_fade(layer, opacity, state,
                                         layer.get(), duration, "in"))

        # Keep the rendered content stable across re-renders (e.g. opacity
        # fades): flet's patch diff no-ops on identical instances, so a fade
        # no longer re-walks the whole screen tree. When the identity changes,
        # the wrapper `key` below flips and the client replaces the subtree
        # wholesale instead of field-by-field diffing two screens.
        content = use_memo(lambda: render(layer.get()), [layer.get()])

        return ft.Container(
            expand=True,
            key=f"sw:{layer.get()!r}",
            opacity=opacity.get(),
            animate_opacity=ft.Animation(
                duration=ft.Duration(
                    milliseconds=int(_phase_s(duration) * 1000))),
            content=content,
        )

    # ---- keep-alive: stacked layers ------------------------------------
    tick = use_state(DataBox(0))[0]
    active = use_state(DataBox(None))[0]
    started = use_state(DataBox(False))[0]
    layers = use_state(lambda: {})[0]
    order = use_state(lambda: [])[0]
    seq = use_state(lambda: [0])[0]
    use_state(tick)
    use_state(active)
    use_state(started)

    cur = identity()
    key = cache_key() if cache_key is not None else _layer_key(cur)

    if key not in layers:
        layers[key] = {"comp": render(cur), "op": 0.0, "order": seq[0]}
        seq[0] += 1
        order.append(key)
        _cap_layers(layers, order, active.get(), key, max_layers)
        tick.set(tick.get() + 1)

    if active.get() is None:
        active.set(key)
        if fade_in_on_mount and not started.get():
            started.set(True)
            state.set("in")
            on_mounted(lambda: _fade_in_only(layers, key, tick, state,
                                             duration))
        else:
            layers[key]["op"] = 1.0
        tick.set(tick.get() + 1)
    elif state.get() == "idle" and key != active.get():
        state.set("out")
        _transition_to(layers, active, tick, state, active.get(), key,
                       duration)

    active_key = active.get()
    anim = ft.Animation(duration=ft.Duration(
        milliseconds=int(_phase_s(duration) * 1000)))
    layer_keys = [k for k in order if k != active_key]
    if active_key in layers:
        layer_keys.append(active_key)  # active on top for interactions
    controls = []
    for k in layer_keys:
        L = layers.get(k)
        if L is None:
            continue
        controls.append(ft.Container(
            expand=True,
            key=f"dsl:{k}",
            opacity=L["op"],
            animate_opacity=anim,
            ignore_interactions=(k != active_key),
            content=L["comp"],
        ))
    return ft.Stack(expand=True, controls=controls)


@component
def Root(store):
    # Foreground border: painted on top of the content so the hairline stays
    # visible along the frameless window's edges even where opaque panels
    # (player bar, sidebar) reach the border.
    return ft.Container(
        expand=True,
        foreground_decoration=ft.BoxDecoration(border=_WINDOW_EDGE),
        content=ft.Stack(
            expand=True,
            controls=[
                ft.Column(
                    spacing=0, expand=True,
                    controls=[
                        TitleBar(store),
                        DissolveSwitch(
                            boxes=[store.connected],
                            identity=lambda: store.connected.get(),
                            render=lambda c: MainLayout(store) if c else LoginScreen(store),
                        ),
                    ],
                ),
                _toast_overlay(store),
            ],
        ),
    )


@component
def LoginScreen(store):
    use_state(store.busy)
    busy = store.busy.get()

    server = ft.TextField(label="Server",
                          hint_text="https://subsonic.example.com:4533",
                          value=_prefill_server(store.config), expand=True)
    username = ft.TextField(label="Username",
                            value=store.config.username, expand=True)
    password = ft.TextField(label="Password", password=True,
                            can_reveal_password=True, expand=True)
    api_key = ft.TextField(label="API key (optional)",
                           value=store.config.api_key or "", expand=True)
    legacy = ft.Checkbox(value=store.config.legacy_auth,
                         label="Legacy (pre-1.13) auth")

    def connect(e=None):
        if busy:
            return
        store.connect(server.value or "", username.value or "",
                      password.value or "", (api_key.value or "").strip(),
                      legacy.value or False)

    card = ft.Container(
        width=380,
        height=400,
        expand=False,
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=16,
        padding=ft.Padding.all(24),
        content=ft.Column(
            spacing=6,
            controls=[
                ft.Text("cmoss", size=26, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.PRIMARY),
                server,
                username,
                password,
                api_key,
                legacy,
                ft.Container(height=4),
                ft.Row(
                    [
                        ft.Container(expand=True),
                        ft.FilledButton("Connect",
                                        on_click=lambda e: connect(),
                                        disabled=busy),
                        ft.Container(expand=True),
                    ],
                    spacing=8,
                ),
            ],
        ),
    )
    return ft.Container(
        expand=True, alignment=ft.Alignment.CENTER, content=card,
    )


@component
def Sidebar(store):
    use_state(store.screen)
    current = store.screen.get()
    items = [
        ("home", ft.Icons.HOME, "Home"),
        ("random", ft.Icons.PLAY_ARROW, "Random"),
        ("playlists", ft.Icons.MENU, "Playlists"),
        ("starred", ft.Icons.FAVORITE, "Starred"),
        ("search", ft.Icons.SEARCH, "Search"),
        ("artists", ft.Icons.MORE_VERT, "Artists"),
        ("genres", ft.Icons.INFO, "Genres"),
        ("settings", ft.Icons.SETTINGS, "Settings"),
    ]

    def nav(key):
        if key == "search":
            store.go_search("")
            return
        store.nav_top(key)

    main_items = []
    for key, icon, label in items:
        if key == "settings":
            continue
        selected = key == current
        main_items.append(ft.Container(
            height=36,
            padding=ft.Padding.symmetric(horizontal=12),
            border_radius=6,
            bgcolor=ft.Colors.SURFACE_CONTAINER if selected else None,
            ink=True,
            on_click=lambda e, k=key: nav(k),
            content=ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(icon, size=18,
                            color=ft.Colors.PRIMARY if selected
                            else ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text(label, size=13,
                            color=ft.Colors.ON_SURFACE if selected
                            else ft.Colors.ON_SURFACE_VARIANT),
                ],
            ),
        ))

    settings_selected = "settings" == current
    main_items.append(ft.Container(expand=True))
    main_items.append(ft.Container(
        height=36,
        padding=ft.Padding.symmetric(horizontal=12),
        border_radius=6,
        bgcolor=ft.Colors.SURFACE_CONTAINER if settings_selected else None,
        ink=True,
        on_click=lambda e: nav("settings"),
        content=ft.Row(
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(ft.Icons.SETTINGS, size=18,
                        color=ft.Colors.PRIMARY if settings_selected
                        else ft.Colors.ON_SURFACE_VARIANT),
                ft.Text("Settings", size=13,
                        color=ft.Colors.ON_SURFACE if settings_selected
                        else ft.Colors.ON_SURFACE_VARIANT),
            ],
        ),
    ))
    return ft.Container(
        padding=ft.Padding.symmetric(vertical=8, horizontal=6),
        content=ft.Column(
            spacing=2,
            controls=main_items,
        ),
    )


@component
def ProgressRow(p):
    """Time labels + seekable progress bar.

    Isolated so the per-tick position updates re-render only this small row
    instead of the whole `NowPlayingBar` (cover, title, transport buttons).
    """
    use_state(p.time_label)
    use_state(p.dur_label)
    use_state(p.progress)
    dragging, set_dragging = use_state(False)
    local_frac, set_local_frac = use_state(0.0)
    seek_w, set_seek_w = use_state(0.0)

    def frac(x):
        w = seek_w or 1
        return max(0.0, min(1.0, x / w))

    def pos(e):
        return e.local_position.x if e.local_position else 0.0

    return ft.Row(
        spacing=8,
        controls=[
            ut_text(p.time_label),
            ft.GestureDetector(
                expand=True,
                on_size_change=lambda e: set_seek_w(e.width),
                on_tap=lambda e: p.seek_fraction(frac(pos(e))),
                on_pan_start=lambda e: (set_dragging(True),
                                        set_local_frac(frac(pos(e))),
                                        p.seek_fraction(frac(pos(e)))),
                on_pan_update=lambda e: (set_local_frac(frac(pos(e))),
                                         p.seek_fraction(frac(pos(e)))),
                on_pan_end=lambda e: (set_dragging(False),
                                      p.seek_fraction(frac(pos(e)))),
                content=ft.ProgressBar(
                    value=local_frac if dragging else p.progress.get(),
                    bar_height=3,
                ),
            ),
            ut_text(p.dur_label),
        ],
    )


@component
def NowPlayingBar(store):
    p = store.player
    for box in (p.playing, p.cover_url, p.state, p.volume, p.shuffle, p.repeat):
        use_state(box)
    use_state(store.panel)

    cur = p.playing.get()
    playing = p.state.get() == "playing"
    title = cur.title if cur else ""
    artist = cur.artist if cur else ""
    repeat = p.repeat.get()
    shuffle_on = p.shuffle.get()

    star = None
    if cur is not None:
        star = StarButton(store, "song", cur.id, size=16)

    def ctrl(icon, tip, on_click, active=False):
        return ft.IconButton(
            icon=icon, icon_size=18, tooltip=tip, on_click=on_click,
            icon_color=ft.Colors.PRIMARY if active else ft.Colors.ON_SURFACE_VARIANT)

    play_icon = (ft.Icons.PAUSE_ROUNDED if playing
                 else ft.Icons.PLAY_ARROW_ROUNDED)
    play_btn = ft.Container(
        key=f"pb-{'pause' if playing else 'play'}",
        width=34, height=34, border_radius=17,
        bgcolor=ft.Colors.ON_SURFACE,
        alignment=ft.Alignment.CENTER,
        content=ft.Icon(play_icon, color=ft.Colors.SURFACE, size=20),
        on_click=lambda e: p.toggle(),
    )
    repeat_icon = ft.Icons.REPEAT_ONE if repeat == "one" else ft.Icons.REPEAT

    cover_key = f"cover-{cur.id if cur else 'none'}"
    cover_src = p.cover_url.get()

    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        content=ft.Row(
            spacing=12,
            controls=[
                # left — track info
                ft.Container(
                    width=240,
                    content=ft.Row(
                        spacing=10,
                        controls=[
                            ft.AnimatedSwitcher(
                                transition=ft.AnimatedSwitcherTransition.FADE,
                                duration=200,
                                content=(ft.Image(
                                    src=cover_src, width=44, height=44,
                                    fit=ft.BoxFit.COVER, border_radius=8,
                                    error_content=placeholder(44),
                                    key=cover_key)
                                    if cover_src
                                    else placeholder(44, key=cover_key)),
                            ),
                            ft.Column(
                                spacing=1, expand=True,
                                alignment=ft.MainAxisAlignment.CENTER,
                                controls=[
                                    ft.AnimatedSwitcher(
                                        transition=ft.AnimatedSwitcherTransition.FADE,
                                        duration=200,
                                        content=ft.Text(
                                            title, size=13,
                                            weight=ft.FontWeight.BOLD,
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                            key=f"title-{cur.id if cur else ''}"),
                                    ),
                                    ft.AnimatedSwitcher(
                                        transition=ft.AnimatedSwitcherTransition.FADE,
                                        duration=200,
                                        content=ft.Text(
                                            artist, size=11,
                                            color=ft.Colors.ON_SURFACE_VARIANT,
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                            key=f"artist-{cur.id if cur else ''}"),
                                    ),
                                ],
                            ),
                            star if star else ft.Container(width=40, height=40),
                        ],
                    ),
                ),
                # center — transport + progress
                ft.Column(
                    spacing=0, expand=True,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[
                        ft.Row(
                            spacing=6,
                            alignment=ft.MainAxisAlignment.CENTER,
                            controls=[
                                ctrl(ft.Icons.SHUFFLE, "Shuffle",
                                     lambda e: p.toggle_shuffle(),
                                     active=shuffle_on),
                                ctrl(ft.Icons.SKIP_PREVIOUS, "Previous",
                                     lambda e: p.prev()),
                                ft.AnimatedSwitcher(
                                    transition=ft.AnimatedSwitcherTransition.FADE,
                                    duration=150,
                                    content=play_btn,
                                ),
                                ctrl(ft.Icons.SKIP_NEXT, "Next",
                                     lambda e: p.next()),
                                ctrl(repeat_icon, "Repeat",
                                     lambda e: p.cycle_repeat(),
                                     active=repeat != "off"),
                            ],
                        ),
                        ProgressRow(p),
                    ],
                ),
                # right — side panels + volume
                ft.Container(
                    width=296,
                    content=ft.Row(
                        spacing=4,
                        controls=[
                            ctrl(ft.Icons.NOTES, "Lyrics",
                                 lambda e: store.toggle_panel("text"),
                                 active=store.panel.get() == "text"),
                            ctrl(ft.Icons.QUEUE_MUSIC, "Queue",
                                 lambda e: store.toggle_panel("queue"),
                                 active=store.panel.get() == "queue"),
                            ctrl(ft.Icons.ALBUM, "Overview",
                                 lambda e: store.toggle_panel("overview"),
                                 active=store.panel.get() == "overview"),
                            ft.Container(width=6),
                            ft.IconButton(
                                icon=ft.Icons.VOLUME_UP if p.volume.get() > 0
                                else ft.Icons.VOLUME_MUTE,
                                icon_size=18, tooltip="Mute",
                                on_click=lambda e: p.toggle_mute()),
                            ft.GestureDetector(
                                width=112, height=4,
                                on_tap=lambda e: p.set_volume(max(0.0, min(1.0, _x(e) / 112))),
                                on_pan_start=lambda e: p.set_volume(max(0.0, min(1.0, _x(e) / 112))),
                                on_pan_update=lambda e: p.set_volume(max(0.0, min(1.0, _x(e) / 112))),
                                content=ft.ProgressBar(
                                    value=p.volume.get(), bar_height=2),
                            ),
                        ],
                    ),
                ),
            ],
        ),
    )


def _x(e):
    return e.local_position.x if e.local_position else 0.0


def ut_text(box):
    """A Text bound to a DataBox (re-renders on change via component)."""
    return ft.Text(box.get(), size=11, color=ft.Colors.ON_SURFACE_VARIANT)


def _route_key(store):
    """Stable cache key for the keep-alive screen layers.

    Excludes the navigation generation counter (which changes on every nav) so
    returning to a visited route reuses its mounted layer. The payload
    discriminator keeps distinct instances of the same screen (different
    albums/artists/searches) in separate layers.
    """
    s = store.screen.get()
    p = store.cur_payload or {}
    if s in ("artist", "album", "playlist"):
        return (s, p.get(f"{s}_id"))
    if s == "genre":
        return (s, p.get("genre"))
    if s == "search":
        return (s, p.get("category") or p.get("query") or "")
    return (s,)


_PLAYER_BAR_H = 66   # height of the floating NowPlayingBar overlay
_FROST_SIGMA = 14.0  # backdrop blur strength for the frosted playerbar/panel
_FROST_ALPHA = 0.72  # tint opacity on top of the blurred backdrop
_WINDOW_EDGE = ft.Border.all(  # hairline drawn over the frameless window edges
    1, ft.Colors.with_opacity(0.18, ft.Colors.ON_SURFACE))
_RAIL_W = 140        # sidebar width (icon + label in a row)
_CONTENT_GAP = 12    # breathing room between the rail and the content column


@component
def MainLayout(store):
    use_state(store.nav_counter)
    use_state(store.panel)
    content = DissolveSwitch(
        boxes=[store.nav_counter],
        identity=lambda: (store.screen.get(), store.cur_payload,
                          store.nav_counter.get()),
        cache_key=lambda: _route_key(store),
        render=lambda v: _content_for(store, *v),
    )
    body = ft.Stack(
        expand=True,
        controls=[
            ft.Row(
                spacing=0, expand=True,
                controls=[content],
            ),
            _player_bar_overlay(store),
            _sidebar_overlay(store),
            _panel_overlay(store),
        ],
    )
    return body


def _toast_overlay(store):
    """Top-right notification stack.

    Rendered above everything (including the login screen). Each toast is a
    keyed `_toast_card` that slides in from the right edge once mounted and
    slides back out when dismissed.

    The overlay is a plain positioned `Container` that keeps a constant
    control type regardless of how many toasts are queued: when the list is
    empty its content collapses to zero height, so it neither covers the UI
    nor forces Flet to swap the control type (a full-size `Stack` here caused
    the whole app to stop reacting to clicks after the last toast closed).
    """
    use_state(store.toast)
    toasts = store.toast.get() or []
    return ft.Container(
        right=12, top=38,
        width=360,
        content=ft.Column(
            spacing=8,
            controls=[_toast_card(store, t, key=f"toast:{t['id']}")
                      for t in toasts],
        ),
    )


@component
def _toast_card(store, t):
    """A single closable, self-dismissing notification card.

    Starts offset right (off-screen) so the implicit `animate_offset` plays the
    slide-in once `settle_toast` flips the card's flag on `on_mounted` — i.e.
    after the control is actually attached on the client. Dismissal flips
    `leaving`, which slides it back out to the same offset.

    Subscribes to `toast_progress` (the same observable the store publishes on
    every countdown tick) and reads its fraction directly from the box — the
    same live-update pattern the player's progress row uses — so the strip at
    the card's bottom edge shrinks as the auto-dismiss time runs out. Hovering
    pauses the countdown, freezing the strip.

    Persistent toasts (``t["persistent"]``) omit the close button and expose
    an ``on_click`` callback on the card body.
    """
    use_state(store.toast_progress)
    x = 1.2 if (not t.get("settled") or t.get("leaving")) else 0.0
    frac = store.toast_progress.get().get(t["id"], 1.0)
    if t.get("leaving"):
        frac = 0.0
    on_mounted(lambda: store.settle_toast(t["id"]))
    persistent = t.get("persistent")
    has_click = t.get("on_click") is not None
    return ft.Container(
        width=360,
        border_radius=6,
        blur=ft.Blur(_FROST_SIGMA, _FROST_SIGMA),
        bgcolor=ft.Colors.with_opacity(_FROST_ALPHA, ft.Colors.SURFACE),
        border=_WINDOW_EDGE,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        shadow=ft.BoxShadow(
            blur_radius=16,
            color=ft.Colors.with_opacity(0.35, ft.Colors.BLACK),
            offset=ft.Offset(0, 4),
        ),
        offset=ft.Offset(x, 0),
        animate_offset=ft.Animation(
            duration=ft.Duration(milliseconds=200),
            curve=ft.AnimationCurve.EASE_OUT,
        ),
        on_hover=lambda e, tid=t["id"] if not persistent else None: (
            store.set_toast_paused(tid, bool(e.data)) if tid else None
        ),
        on_click=lambda e, cb=t.get("on_click"): cb() if cb else None,
        ink=has_click,
        content=ft.Column(
            spacing=0,
            controls=[
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                    content=ft.Row(
                        spacing=8,
                        controls=[
                            *([ft.Image(
                                src=t["cover"], width=40, height=40,
                                fit=ft.BoxFit.COVER, border_radius=6,
                            )] if t.get("cover") else []),
                            ft.Text(t["msg"], size=13, expand=True, max_lines=3,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    color=ft.Colors.ON_SURFACE),
                            *([] if persistent else [
                                ft.IconButton(
                                    icon=ft.Icons.CLOSE, icon_size=14,
                                    icon_color=ft.Colors.ON_SURFACE_VARIANT,
                                    tooltip="Close",
                                    on_click=lambda e, tid=t["id"]: store.dismiss_toast(tid),
                                ),
                            ]),
                        ],
                    ),
                ),
                ft.ProgressBar(
                    value=frac,
                    bar_height=3,
                    color=ft.Colors.PRIMARY,
                    bgcolor=ft.Colors.with_opacity(0.10, ft.Colors.ON_SURFACE),
                    border_radius=ft.BorderRadius.only(
                        bottom_left=6, bottom_right=6),
                ),
            ],
        ),
    )


# --------------------------------------------------------------------------
# Right side panel (lyrics / queue / overview)
# --------------------------------------------------------------------------

def _resize_start(store):
    def h(e):
        store._resize_start_x = e.global_position.x
        store._resize_base_w = store.panel_width.get()
    return h


def _resize_update(store):
    def h(e):
        dx = store._resize_start_x - e.global_position.x
        store.set_panel_width(store._resize_base_w + dx)
    return h


def _resize_end(store):
    def h(e):
        store.save_panel_width()
    return h


def _panel_resize_handle(store):
    return ft.GestureDetector(
        on_pan_start=_resize_start(store),
        on_pan_update=_resize_update(store),
        on_pan_end=_resize_end(store),
        mouse_cursor=ft.MouseCursor.RESIZE_COLUMN,
        content=ft.Container(
            width=6,
            alignment=ft.Alignment(0, 0),
            content=ft.Container(
                width=3, height=36, border_radius=2,
                bgcolor=ft.Colors.OUTLINE_VARIANT,
            ),
        ),
    )


@component
def _sidebar_overlay(store):
    """Left navigation rail as an opaque overlay pinned over the content area."""
    use_state(store.screen)
    return ft.Container(
        left=0, top=0, bottom=_PLAYER_BAR_H, width=_RAIL_W,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        content=Sidebar(store),
    )


@component
def _panel_overlay(store):
    """Right panel as a drawer: always at full configured width, sliding in
    and out from the window's right edge (offset animation only, no size
    animation). Frosted: the backdrop blur applies to the content scrolling
    underneath the panel."""
    use_state(store.panel)
    use_state(store.panel_width)
    open_ = store.panel.get() is not None
    return ft.Container(
        right=0, top=0, bottom=_PLAYER_BAR_H,
        width=store.panel_width.get(),
        offset=ft.Offset(0.0 if open_ else 1.0, 0),
        animate_offset=ft.Animation(
            duration=ft.Duration(milliseconds=200),
            curve=ft.AnimationCurve.EASE_OUT if open_
            else ft.AnimationCurve.EASE_IN,
        ),
        blur=ft.Blur(_FROST_SIGMA, _FROST_SIGMA),
        bgcolor=ft.Colors.with_opacity(_FROST_ALPHA, ft.Colors.SURFACE),
        border=ft.Border(
            bottom=ft.BorderSide(1, ft.Colors.with_opacity(0.12, ft.Colors.ON_SURFACE)),
        ),
        content=RightPanel(store),
    )


def _player_bar_overlay(store):
    """Floating frosted now-playing bar pinned to the bottom of the content
    area, so the screens scroll underneath it and get blurred behind the
    translucent tint."""
    return ft.Container(
        left=0, right=0, bottom=0,
        height=_PLAYER_BAR_H,
        blur=ft.Blur(_FROST_SIGMA, _FROST_SIGMA),
        bgcolor=ft.Colors.with_opacity(_FROST_ALPHA, ft.Colors.SURFACE),
        content=NowPlayingBar(store),
    )


@component
def RightPanel(store):
    use_state(store.panel)
    use_state(store.player.queue_rev)
    use_state(store.player.playing_index)
    use_state(store.player.playing)
    use_state(store.panel_width)
    titles = {"text": "Lyrics", "queue": "Queue", "overview": "Overview"}

    def panel_content(m):
        title = ft.Row(
            spacing=8,
            controls=[
                ft.Text(titles.get(m, ""), size=13, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                ft.IconButton(icon=ft.Icons.CLOSE, icon_size=16,
                              tooltip="Close",
                              on_click=lambda e: store.toggle_panel(m)),
            ],
        )
        if m == "queue":
            body = _queue_panel(store)
        elif m == "overview":
            body = _overview_panel(store)
        elif m == "text":
            body = LyricsPanel(store)
        else:
            body = _placeholder_panel(store, m)
        return ft.Column(spacing=8, expand=True, controls=[title, body])

    return ft.Row(
        spacing=0,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            _panel_resize_handle(store),
            ft.Container(
                expand=True,
                padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                content=DissolveSwitch(
                    boxes=[store.panel, store.player.queue_rev,
                           store.player.playing_index, store.player.playing],
                    identity=lambda: store.panel.get(),
                    render=lambda m: panel_content(m),
                    duration=150,
                    fade_in_on_mount=True,
                    keep_alive=False,
                ),
            ),
        ],
    )


def _placeholder_panel(store, mode):
    text = {
        "overview": "Nothing playing",
    }.get(mode, "")
    return ft.Container(
        expand=True, alignment=ft.Alignment.CENTER,
        content=ft.Text(text, size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                        text_align=ft.TextAlign.CENTER),
    )


_LYRICS_LINE_H = 24
_LYRICS_CHAR_W = 9.5
_LYRICS_AVAIL_W = 284
_LYRICS_SPACING = 4
_LYRICS_SCROLL_PAD = 48


def _lyrics_line_est_h(text):
    """Estimated wrapped height of one lyric line in pixels.

    Used for lines that have not been laid out yet; measured heights from
    `on_size_change` replace it once the line materializes in the viewport.
    """
    wrapped = max(1, math.ceil(len(text) * _LYRICS_CHAR_W / _LYRICS_AVAIL_W))
    return wrapped * _LYRICS_LINE_H


@component
def LyricsPanel(store):
    """Synced (karaoke) and plain lyrics for the current track.

    Subscribes to the *derived* active-line box rather than raw position, so
    the whole panel re-renders only when the highlighted line changes (about
    once per lyric line) instead of on every ~100 ms position tick.
    """
    use_state(store.lyrics)
    use_state(store.player.playing)
    active_box = use_state(
        lambda: DerivedBox(
            lambda ly, pos: active_line_index(ly, pos),
            store.lyrics, store.player.position_ms,
        )
    )[0]
    lyrics = store.lyrics.get()
    song = store.player.playing.get()
    active = active_box.get()

    if song is None:
        return ft.Container(
            expand=True, alignment=ft.Alignment.CENTER,
            content=ft.Text("Nothing playing", size=13,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            text_align=ft.TextAlign.CENTER),
        )
    if lyrics == "loading":
        return ft.Container(
            expand=True, alignment=ft.Alignment.CENTER,
            content=ft.Text("Loading lyrics...", size=13,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            text_align=ft.TextAlign.CENTER),
        )
    if not lyrics:
        return ft.Container(
            expand=True, alignment=ft.Alignment.CENTER,
            content=ft.Text("No lyrics found", size=13,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            text_align=ft.TextAlign.CENTER),
        )

    header = ft.Row(
        spacing=8,
        controls=[
            ft.Text(lyrics.source.upper(), size=10,
                    weight=ft.FontWeight.BOLD,
                    color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Container(expand=True),
            ft.Text("synced" if lyrics.synced else "plain", size=10,
                    color=ft.Colors.ON_SURFACE_VARIANT),
        ],
    )

    heights = use_state(lambda: {})[0]

    def measure(i):
        def handler(e):
            heights[i] = e.height
        return handler

    rows = [
        ft.Text(
            line.text,
            size=17,
            no_wrap=False,
            weight=ft.FontWeight.BOLD if i == active else None,
            color=(ft.Colors.PRIMARY if i == active
                   else ft.Colors.ON_SURFACE_VARIANT),
            on_size_change=measure(i),
        )
        for i, line in enumerate(lyrics.lines)
    ]
    lv_ref = use_state(lambda: ft.Ref())[0]
    list_view = ft.ListView(
        ref=lv_ref, expand=True, controls=rows, spacing=_LYRICS_SPACING,
        padding=ft.Padding.symmetric(vertical=6),
    )

    async def scroll_active():
        if active < 0:
            return
        lv = lv_ref.current
        if lv is None:
            return
        offset = 0.0
        for j in range(active):
            h = heights.get(j)
            if h is None:
                h = _lyrics_line_est_h(lyrics.lines[j].text)
            offset += h + _LYRICS_SPACING
        try:
            await lv.scroll_to(
                offset=max(0, offset - _LYRICS_SCROLL_PAD),
                duration=ft.Duration(milliseconds=250),
                curve=ft.AnimationCurve.EASE_OUT,
            )
        except Exception as e:
            log.debug("lyrics scroll failed: %s", e)

    use_effect(scroll_active, [active])

    return ft.Column(expand=True, spacing=6, controls=[header, list_view])


def _overview_panel(store):
    p = store.player
    song = p.playing.get()
    if song is None:
        return ft.Container(
            expand=True, alignment=ft.Alignment.CENTER,
            content=ft.Text("Nothing playing", size=13,
                            color=ft.Colors.ON_SURFACE_VARIANT),
        )

    cover = store.cover_url_for(song) if store.proxy else None

    def link(text, on_click):
        if not text:
            return None
        return ft.Container(
            on_click=on_click,
            content=ft.Text(text, size=12,
                            color=ft.Colors.PRIMARY if on_click
                            else ft.Colors.ON_SURFACE_VARIANT,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
        )

    artist_name = getattr(song, "display_artist", None) or getattr(song, "artist", None)
    artist_link = None
    if artist_name:
        artist_id = getattr(song, "artist_id", None)
        artist_link = link(artist_name,
                           (lambda e, aid=artist_id: store.go_artist(aid))
                           if artist_id else None)
    album_link = link(getattr(song, "album", None),
                      (lambda e, aid=song.album_id: store.go_album(aid))
                      if getattr(song, "album_id", None) else None)

    meta_rows = []

    def meta(label, value):
        if value:
            meta_rows.append(ft.Row(
                spacing=8,
                controls=[
                    ft.Text(label, size=11, width=70,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text(str(value), size=12, expand=True, max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS),
                ],
            ))

    meta("Year", song.year)
    meta("Genre", song.genre)
    if getattr(song, "track", None):
        meta("Track",
             f"{song.track}" + (f"/{song.disc_number}"
                                if getattr(song, "disc_number", None) else ""))
    if getattr(song, "bit_rate", None):
        meta("Bitrate", f"{song.bit_rate} kbps")

    star = StarButton(store, "song", song.id, size=20)

    info = ft.Column(
        spacing=2,
        controls=[x for x in [artist_link, album_link] if x],
    )
    return ft.Column(
        spacing=12, expand=True, scroll=ft.ScrollMode.AUTO,
        controls=[
            ft.Container(
                alignment=ft.Alignment.CENTER,
                content=ft.Image(src=cover, width=280, height=280,
                                 fit=ft.BoxFit.COVER, border_radius=12,
                                 error_content=placeholder(280)),
            ),
            ft.Text(song.title or "", size=16, weight=ft.FontWeight.BOLD,
                    max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
            info,
            ft.Row(
                spacing=8,
                controls=[
                    ft.Text(fmt_dur(song.duration), size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Container(expand=True),
                    star,
                ],
            ),
            ft.Column(spacing=6, controls=meta_rows)
            if meta_rows else ft.Container(),
        ],
    )


def _queue_row(store, song, i, is_current):
    p = store.player
    cover = store.cover_url_for(song) if store.proxy else None
    return ft.Container(
        key=f"q-{getattr(song, 'id', i)}",
        content=ft.Row(
            spacing=8,
            controls=[
                (ft.Icon(ft.Icons.MUSIC_NOTE, size=13, color=ft.Colors.PRIMARY)
                 if is_current else ft.Text(str(i + 1), size=11, width=14,
                                            color=ft.Colors.ON_SURFACE_VARIANT)),
                ft.Image(src=cover, width=36, height=36, fit=ft.BoxFit.COVER,
                         border_radius=6, error_content=placeholder(36)),
                ft.Container(
                    expand=True,
                    ink=True,
                    on_click=lambda e, i=i: p.jump(i),
                    content=ft.Column(
                        spacing=0,
                        alignment=ft.MainAxisAlignment.CENTER,
                        controls=[
                            ft.Text(song.title or "", size=12,
                                    weight=ft.FontWeight.BOLD if is_current else None,
                                    color=ft.Colors.PRIMARY if is_current else None,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text(song.artist or "", size=11,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ],
                    ),
                ),
                ft.IconButton(icon=ft.Icons.CLOSE, icon_size=14, tooltip="Remove",
                              icon_color=ft.Colors.ON_SURFACE_VARIANT,
                              on_click=lambda e, i=i: p.remove(i)),
            ],
        ),
        padding=ft.Padding.only(left=8, right=24, top=2, bottom=2),
        border_radius=6,
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_current else None,
    )


def _queue_panel(store):
    p = store.player
    q = p.queue
    if not q:
        return ft.Container(
            expand=True, alignment=ft.Alignment.CENTER,
            content=ft.Text("Queue is empty", size=13,
                            color=ft.Colors.ON_SURFACE_VARIANT),
        )

    def on_reorder(e):
        old, new = e.old_index, e.new_index
        if old is None or new is None:
            return
        moved = e.control.controls.pop(old)
        e.control.controls.insert(new, moved)
        p.move(old, new)

    rows = [_queue_row(store, s, i, i == p.index and p.playing.get() is not None)
            for i, s in enumerate(q)]
    return ft.Column(
        spacing=8, expand=True,
        controls=[
            ft.Row(
                spacing=8,
                controls=[
                    ft.Text(f"{len(q)} tracks", size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Container(expand=True),
                    ft.TextButton("Clear", on_click=lambda e: p.clear()),
                ],
            ),
            ft.ReorderableListView(
                controls=rows,
                expand=True,
                spacing=2,
                on_reorder=on_reorder,
            ),
        ],
    )


def _content_for(store, screen, payload, nav):
    """Pick the screen component currently active (dispatch by identity).

    Every screen gets a `key` embedding both a payload discriminator and the
    current navigation generation so that re-navigating to the same component
    function with different content forces a fresh mount (a stale, partially
    reconciled subtree can otherwise stay frozen while the player, backed by
    its own subscriptions, keeps updating)."""
    gen = lambda s: f"{s}-{nav}"
    if screen == "home":
        return HomeScreen(store, key=gen(f"home-{nav}"))
    if screen == "random":
        return HomeScreen(store, key=gen(f"random-{nav}"))
    if screen == "playlists":
        return PlaylistsScreen(store, key=gen("playlists"))
    if screen == "settings":
        return SettingsScreen(store, key=gen("settings"))
    if screen == "starred":
        return StarredScreen(store, key=gen("starred"))
    if screen == "search":
        return SearchScreen(
            store,
            query=payload.get("query", ""),
            category=payload.get("category"),
            key=gen(f"search-{payload.get('category') or payload.get('query') or ''}"))
    if screen == "artists":
        return ArtistsScreen(store, key=gen("artists"))
    if screen == "genres":
        return GenresScreen(store, key=gen("genres"))
    if screen == "artist":
        return ArtistScreen(store, artist_id=payload.get("artist_id"),
                            key=gen(f"artist-{payload.get('artist_id', '')}"))
    if screen == "album":
        return AlbumScreen(store, album_id=payload.get("album_id"),
                           key=gen(f"album-{payload.get('album_id', '')}"))
    if screen == "playlist":
        return PlaylistScreen(store, playlist_id=payload.get("playlist_id"),
                              key=gen(f"playlist-{payload.get('playlist_id', '')}"))
    if screen == "genre":
        return GenreScreen(store, genre=payload.get("genre"),
                           key=gen(f"genre-{payload.get('genre', '')}"))
    return HomeScreen(store, ltype="newest", key="home")


def screen_header(store, title, subtitle=None, back=True):
    texts = []
    if title:
        texts.append(ft.Text(title, size=20, weight=ft.FontWeight.BOLD,
                             max_lines=1, overflow=ft.TextOverflow.ELLIPSIS))
    if subtitle:
        texts.append(ft.Text(subtitle, size=12,
                             color=ft.Colors.ON_SURFACE_VARIANT))
    return ft.Row(
        margin=ft.Margin.only(left=8, right=8, top=4, bottom=4),
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.IconButton(icon=ft.Icons.ARROW_BACK,
                          on_click=lambda e: store.back(),
                          tooltip="back") if back else ft.Container(),
            ft.Column(
                texts,
                spacing=2, expand=True,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            ft.IconButton(ft.Icons.REFRESH, tooltip="refresh",
                          on_click=lambda e: store.run(
                              store._load_screen(store.screen.get()))),
        ],
    )


def _empty(msg):
    return ft.Text(msg, size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                   margin=ft.Margin.only(left=12, top=4))


# --------------------------------------------------------------------------
# Library screens
# --------------------------------------------------------------------------

HOME_SECTIONS = (("newest", "New"), ("recent", "Recently Added"),
                 ("frequent", "Frequently Played"), ("random", "Random"))
HOME_SECTION_TITLES = dict(HOME_SECTIONS)


def _section_heading(title, subtitle=None, show_all=None):
    controls = [ft.Text(title, size=17, weight=ft.FontWeight.BOLD)]
    if subtitle:
        controls.append(ft.Text(subtitle, size=12,
                                color=ft.Colors.ON_SURFACE_VARIANT))
    if show_all:
        controls.append(ft.Container(expand=True))
        controls.append(ft.TextButton("Show all", on_click=show_all,
                                      icon=ft.Icons.ARROW_FORWARD))
    return ft.Row(
        margin=ft.Margin.only(left=8, right=8, top=9, bottom=4),
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=controls,
    )


def _cover_box(src, size, text):
    """Square cover with a monogram fallback when there is no artwork."""
    if src:
        return ft.Image(src=src, width=size, height=size,
                        fit=ft.BoxFit.COVER, border_radius=8)
    return ft.Container(
        width=size, height=size, border_radius=8,
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        alignment=ft.Alignment.CENTER,
        content=ft.Text((text or "?")[:2].upper(), size=max(14, size // 4),
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        text_align=ft.TextAlign.CENTER),
    )


@component
def HomeScreen(store):
    boxes = {}
    for lt, _ in HOME_SECTIONS:
        box = store.albums.get(lt) or store._data(lt)
        use_state(box)
        boxes[lt] = box
    inset = _RAIL_W + _CONTENT_GAP - 8
    children = []
    for lt, title in HOME_SECTIONS:
        albums = boxes[lt].get()
        if albums:
            children.append(ft.Container(
                margin=ft.Margin(left=inset),
                content=ft.Column(
                    controls=[_section_heading(
                        title,
                        show_all=(lambda e, l=lt: store.go_category(l)))],
                ),
            ))
            children.append(ft.Container(
                margin=ft.Margin(left=inset),
                padding=ft.Padding(top=8, right=8, bottom=8),
                content=ft.Row(
                    controls=[album_tile(store, a, key=f"a:{a.id}")
                              for a in albums],
                    scroll=ft.ScrollMode.AUTO,
                    spacing=14,
                ),
            ))
    if len(children) == 1:
        children.append(ft.Container(
            margin=ft.Margin(left=inset),
            height=80, alignment=ft.Alignment.CENTER,
            content=ft.Text("Loading...", size=13,
                            color=ft.Colors.ON_SURFACE_VARIANT)))
    return ft.ListView(expand=True, padding=ft.Padding(bottom=_PLAYER_BAR_H + 8),
                       controls=children)


@component
def ArtistTile(store, artist, size=150):
    name = artist.name or ""
    src = getattr(artist, "artist_image_url", None)
    if not src and getattr(artist, "cover_art", None):
        src = store.cover_url_for(artist)
    count = getattr(artist, "album_count", 0) or 0
    return ft.Container(
        width=size,
        on_click=lambda e, a=artist: store.go_artist(a.id),
        content=ft.Column(
            spacing=4,
            controls=[
                _cover_box(src, size, name),
                ft.Text(name, size=13, weight=ft.FontWeight.BOLD,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Text(f"{count} albums" if count else "",
                        size=11, color=ft.Colors.ON_SURFACE_VARIANT,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ],
        ),
    )


artist_tile = memo(ArtistTile)


@component
def ArtistsScreen(store):
    use_state(store.artists)
    artists = store.artists.get() or []
    groups: dict[str, list] = {}
    for a in artists:
        letter = ((a.sort_name or a.name or "")[:1] or "?").upper()
        groups.setdefault(letter, []).append(a)
    controls = []
    for letter in sorted(groups):
        controls.append(_section_heading(letter))
        controls.append(ft.Container(
            padding=ft.Padding.all(8),
            content=ft.Row(
                controls=[artist_tile(store, a, key=f"art:{a.id}")
                          for a in groups[letter]],
                wrap=True, spacing=12, run_spacing=12,
            ),
        ))
    if not groups:
        controls.append(_empty("No artists"))
    return ft.ListView(expand=True, padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8, bottom=_PLAYER_BAR_H + 8), controls=controls)


@component
def GenresScreen(store):
    use_state(store.genres)
    genres = store.genres.get() or []
    cards = []
    for g in genres:
        name = getattr(g, "value", None) or getattr(g, "name", "") or ""
        count = getattr(g, "song_count", 0) or 0
        cards.append(ft.Container(
            width=220,
            on_click=lambda e, n=name: store.go_genre(n),
            padding=ft.Padding.symmetric(horizontal=16, vertical=14),
            border_radius=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            content=ft.Row(
                spacing=8,
                controls=[
                    ft.Text(name, size=14, weight=ft.FontWeight.BOLD,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                            expand=True),
                    ft.Text(str(count), size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                ],
            ),
        ))
    controls = []
    if cards:
        controls.append(ft.Container(
            padding=ft.Padding.all(8),
            content=ft.Row(controls=cards, wrap=True,
                           spacing=8, run_spacing=8),
        ))
    else:
        controls.append(_empty("No genres"))
    return ft.ListView(expand=True, padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8, bottom=_PLAYER_BAR_H + 8), controls=controls)


@component
def SettingsScreen(store):
    use_state(store.connected)
    use_state(store.toast)
    cfg = store.config

    def info_row(label, value):
        return ft.Row(
            spacing=8,
            controls=[
                ft.Text(label, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Container(expand=True),
                ft.Text(value, size=12, weight=ft.FontWeight.BOLD,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ],
        )

    def _fmt_bytes(n):
        n = float(n)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if n < 1024 or unit == "TiB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PiB"

    conn_card = ft.Container(
        padding=ft.Padding.all(16),
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=12,
        content=ft.Column(
            spacing=8,
            controls=[
                ft.Text("Connection", size=13, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.PRIMARY),
                info_row("Server", cfg.server_display()),
                info_row("Username", cfg.username),
                info_row("Cache directory", cfg.cache_dir),
            ],
        ),
    )

    stats = store.cache_stats()
    limit_mib = cfg.max_cache_bytes // (1024 * 1024)
    size_field = ft.TextField(
        label="Cache limit, MiB",
        value=str(limit_mib),
        keyboard_type=ft.KeyboardType.NUMBER,
        width=140,
        dense=True,
    )

    def apply_limit(e):
        try:
            v = int((size_field.value or "").strip())
        except ValueError:
            store.show_toast("Enter a whole number of MiB")
            return
        store.set_cache_limit(v)

    cache_card = ft.Container(
        padding=ft.Padding.all(16),
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=12,
        content=ft.Column(
            spacing=8,
            controls=[
                ft.Text("Cache", size=13, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.PRIMARY),
                info_row("Files", f"{stats['files']} ({_fmt_bytes(stats['bytes'])})"),
                info_row("Tracks", f"{stats['streams']} ({_fmt_bytes(stats['stream_bytes'])})"),
                info_row("Covers", f"{stats['covers']} ({_fmt_bytes(stats['cover_bytes'])})"),
                ft.Container(height=4),
                ft.Row(
                    spacing=8,
                    controls=[
                        size_field,
                        ft.FilledButton("Apply", on_click=apply_limit),
                        ft.OutlinedButton(
                            "Clear cache",
                            on_click=lambda e: store.clear_cache(),
                            style=ft.ButtonStyle(
                                bgcolor=ft.Colors.ERROR_CONTAINER,
                                color=ft.Colors.ON_ERROR_CONTAINER,
                            ),
                        ),
                    ],
                ),
            ],
        ),
    )

    notif_card = ft.Container(
        padding=ft.Padding.all(16),
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=12,
        content=ft.Column(
            spacing=8,
            controls=[
                ft.Text("Notifications", size=13, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.PRIMARY),
                ft.Checkbox(
                    label="Show a notification when a track starts playing",
                    value=cfg.track_toast,
                    on_change=lambda e: store.set_track_toast(
                        bool(e.control.value)),
                ),
            ],
        ),
    )

    return ft.Container(
        expand=True,
        padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8,
                           right=24, top=16, bottom=_PLAYER_BAR_H + 8),
        content=ft.Column(
            spacing=12,
            expand=True,
            controls=[
                ft.Text("Settings", size=20, weight=ft.FontWeight.BOLD),
                conn_card,
                notif_card,
                cache_card,
                ft.Container(expand=True),
                ft.FilledButton(
                    "Log out",
                    icon=ft.Icons.LOGOUT,
                    on_click=lambda e: store.logout(),
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.ERROR_CONTAINER,
                        color=ft.Colors.ON_ERROR_CONTAINER,
                    ),
                ),
            ],
        ),
    )


@component
def PlaylistsScreen(store):
    use_state(store.playlists)
    use_state(store.toast)
    pls = store.playlists.get() or []
    tiles = []
    for pl in pls:
        cover = (store.cover_url_for(pl)
                 if store.proxy and getattr(pl, "cover_art", None) else None)
        count = getattr(pl, "song_count", 0) or 0
        tiles.append(ft.Container(
            width=170,
            on_click=lambda e, p=pl: store.go_playlist(p.id),
            content=ft.Column(
                spacing=4,
                controls=[
                    _cover_box(cover, 170, pl.name or ""),
                    ft.Text(pl.name or "", size=13, weight=ft.FontWeight.BOLD,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(f"{count} tracks" if count else (pl.owner or ""),
                            size=11, color=ft.Colors.ON_SURFACE_VARIANT,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ],
            ),
        ))
    controls = []
    if tiles:
        controls.append(ft.Container(
            padding=ft.Padding.all(8),
            content=ft.Row(controls=tiles, wrap=True,
                           spacing=14, run_spacing=14),
        ))
    else:
        controls.append(_empty("No playlists"))
    return ft.ListView(expand=True, padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8, bottom=_PLAYER_BAR_H + 8), controls=controls)


@component
def PlaylistScreen(store, playlist_id):
    box = store.playlist_songs.get(playlist_id) or store._playlist_songs_for(playlist_id)
    use_state(box)
    songs = box.get() or []
    pl = next((p for p in store.playlists.get() if p.id == playlist_id), None)
    name = (pl.name or "Playlist") if pl else "Playlist"
    subtitle = f"{len(songs)} tracks" if songs else (pl.owner if pl else "")
    return ft.ListView(
        expand=True,
        padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8,
                           bottom=_PLAYER_BAR_H + 8),
        controls=[screen_header(store, "", back=True)]
        + song_list(store, songs, play_all_label="Play"),
    )


@component
def StarredScreen(store):
    use_state(store.starred)
    use_state(store.starred_ids)
    starred = store.starred.get()
    ids = store.starred_ids.get()
    controls = []
    if starred is not None:
        songs = [s for s in (starred.song or []) if f"s:{s.id}" in ids]
        albums = [a for a in (starred.album or []) if f"a:{a.id}" in ids]
        if songs:
            controls.append(_heading("Songs"))
            controls.extend(song_list(store, songs, show_artist=True))
        if albums:
            controls.append(_heading("Albums"))
            controls.append(ft.Container(
                padding=ft.Padding.all(8),
                content=album_grid(store, albums, size=150),
            ))
    if len(controls) == 1:
        controls.append(_empty("Loading..." if starred is None
                                else "Nothing starred yet"))
    return ft.ListView(expand=True, padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8, bottom=_PLAYER_BAR_H + 8), controls=controls)


def _heading(text):
    return ft.Text(text, size=14, weight=ft.FontWeight.BOLD,
                   margin=ft.Margin.only(left=8, top=6))


@component
def SearchScreen(store, query="", category=None):
    use_state(store.search)
    field = ft.TextField(hint_text="Search", expand=True)

    def do_search(q=None):
        q = (q if q is not None else field.value or "").strip()
        if q:
            store.go_search(q)

    if category:
        box = store.albums.get(f"cat:{category}") or store._data(f"cat:{category}")
        use_state(box)
        albums = box.get() or []
        sections = [
            screen_header(store, "", back=True),
        ]
        if albums:
            sections.append(ft.Container(
                padding=ft.Padding.all(8),
                content=album_grid(store, albums, size=180),
            ))
        else:
            sections.append(_empty("Loading..."))
        return ft.ListView(expand=True, padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8, bottom=_PLAYER_BAR_H + 8), controls=sections)

    res = store.search.get()
    sections = [
        ft.Row(
            margin=ft.Margin.symmetric(horizontal=8),
            spacing=8,
            controls=[field, ft.FilledButton("Go",
                                             on_click=lambda e: do_search())],
        ),
    ]
    if res is not None:
        artists = list(res.artist or [])
        albums = list(res.album or [])
        songs = list(res.song or [])
        if artists:
            sections.append(_heading("Artists"))
            sections.append(ft.Container(
                padding=ft.Padding.all(8),
                content=ft.Row(
                    controls=[artist_tile(store, a, size=120, key=f"art:{a.id}")
                              for a in artists],
                    wrap=True, spacing=12, run_spacing=12,
                ),
            ))
        if albums:
            sections.append(_heading("Albums"))
            sections.append(ft.Container(
                padding=ft.Padding.all(8),
                content=album_grid(store, albums, size=140),
            ))
        if songs:
            sections.append(_heading("Songs"))
            sections.extend(song_list(store, songs))
        if not (artists or albums or songs):
            sections.append(_empty("No results"))
    return ft.ListView(expand=True, padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8, bottom=_PLAYER_BAR_H + 8), controls=sections)


@component
def ArtistScreen(store, artist_id):
    box = store.albums.get("artist") or store._data("artist")
    use_state(box)
    use_state(store.artist_detail)
    artist = store.artist_detail.get()
    albums = box.get() or []
    name = (artist.name if artist else "") or next(
        (a.name or "" for a in store.artists.get() if a.id == artist_id),
        "Artist")
    src = (getattr(artist, "artist_image_url", None) if artist else None)
    if not src and artist and getattr(artist, "cover_art", None):
        src = store.cover_url_for(artist)
    count = getattr(artist, "album_count", 0) or len(albums)

    hero = ft.Container(
        padding=ft.Padding.all(12),
        content=ft.Row(
            spacing=16,
            controls=[
                _cover_box(src, 160, name),
                ft.Column(
                    expand=True,
                    spacing=6,
                    controls=[
                        ft.Text(name, size=22, weight=ft.FontWeight.BOLD,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(f"{count} albums", size=12,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                    ],
                ),
            ],
        ),
    )
    controls = [screen_header(store, "", back=True), hero]
    if albums:
        controls.append(ft.Container(
            padding=ft.Padding.all(8),
            content=album_grid(store, albums),
        ))
    else:
        controls.append(_empty("No albums"))
    return ft.ListView(expand=True, padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8, bottom=_PLAYER_BAR_H + 8), controls=controls)


@component
def AlbumScreen(store, album_id):
    box = store.songs.get(album_id) or store._songs_for(album_id)
    info = store.albums_detail.get(album_id) or store._album_detail_for(album_id)
    use_state(box)
    use_state(info)
    album = info.get()
    songs = box.get() or []

    controls = [screen_header(store, "", back=True)]
    if album is None:
        controls.append(ft.Container(
            height=80, alignment=ft.Alignment.CENTER,
            content=ft.Text("Loading...", size=13,
                            color=ft.Colors.ON_SURFACE_VARIANT)))
        return ft.ListView(expand=True, padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8, bottom=_PLAYER_BAR_H + 8), controls=controls)

    cover = store.cover_url_for(album)
    artist_name = album.display_artist or album.artist or ""
    artist_link = None
    if artist_name:
        artist_link = ft.Container(
            on_click=(lambda e, aid=album.artist_id: store.go_artist(aid))
            if album.artist_id else None,
            content=ft.Text(artist_name, size=14, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.PRIMARY if album.artist_id else None,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
        )
    genre = album.genre or (album.genres[0].name
                            if getattr(album, "genres", None) else None)
    meta_parts = []
    if album.year:
        meta_parts.append(str(album.year))
    if genre:
        meta_parts.append(genre)
    meta_parts.append(f"{len(songs)} tracks")
    total = sum((s.duration or 0) for s in songs)
    if total:
        meta_parts.append(fmt_dur(total))
    meta = " • ".join(meta_parts)

    hero = ft.Container(
        padding=ft.Padding.all(12),
        content=ft.Row(
            spacing=16,
            controls=[
                _cover_box(cover, 200, album.name or ""),
                ft.Column(
                    expand=True,
                    spacing=6,
                    controls=[
                        ft.Text(album.name or "", size=22,
                                weight=ft.FontWeight.BOLD, max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        artist_link if artist_link else ft.Container(),
                        ft.Text(meta, size=12,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                        ft.Container(height=6),
                        ft.Row(
                            spacing=8,
                            controls=[
                                ft.FilledButton(
                                    "Play", icon=ft.Icons.PLAY_ARROW,
                                    on_click=lambda e: store.play(songs, 0)),
                                ft.FilledTonalButton(
                                    "Shuffle", icon=ft.Icons.SHUFFLE,
                                    on_click=lambda e: store.play_shuffle(songs)),
                                ft.FilledTonalButton(
                                    "Add to queue", icon=ft.Icons.QUEUE_MUSIC,
                                    on_click=lambda e: store.add_to_queue(songs)),
                                StarButton(store, "album", album_id, size=24),
                            ],
                        ),
                    ],
                ),
            ],
        ),
    )

    list_controls = []
    discs: dict[int, list] = {}
    for s in songs:
        discs.setdefault(s.disc_number or 1, []).append(s)

    def _disc_key(d):
        try:
            return int(d)
        except (TypeError, ValueError):
            return 1 << 30

    if len(discs) > 1:
        for disc in sorted(discs, key=_disc_key):
            list_controls.append(_heading(f"Disc {disc}"))
            list_controls.extend(song_list(store, discs[disc],
                                           show_artist=True, play_all=False))
    else:
        list_controls.extend(song_list(store, songs,
                                       show_artist=True, play_all=False))
    controls += [hero] + list_controls
    return ft.ListView(expand=True, padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8, bottom=_PLAYER_BAR_H + 8), controls=controls)


@component
def GenreScreen(store, genre):
    box = store.songs.get(f"genre:{genre}") or store._songs_for(f"genre:{genre}")
    use_state(box)
    songs = box.get() or []
    return ft.ListView(
        expand=True,
        padding=ft.Padding(left=_RAIL_W + _CONTENT_GAP - 8,
                           bottom=_PLAYER_BAR_H + 8),
        controls=[screen_header(store, "", back=True)]
        + song_list(store, songs),
    )