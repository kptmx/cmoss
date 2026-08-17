"""Shared declarative Flet widgets: album tiles, song rows, star toggling."""
from __future__ import annotations

import flet as ft
from flet import component, memo, use_state

from ..player import fmt_ms


def placeholder(size, key=None):
    return ft.Container(
        key=key,
        width=size, height=size,
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=8,
        alignment=ft.Alignment.CENTER,
        content=ft.Text("music", color=ft.Colors.ON_SURFACE_VARIANT,
                         size=max(10, size // 6), text_align=ft.TextAlign.CENTER),
    )


@component
def StarButton(store, kind, item_id, size=16, tooltip="star"):
    """A reactive star toggle bound to the store's `starred_ids` box.

    Re-renders as soon as any star changes anywhere in the app, so clicking a
    star updates every list / the now-playing bar / the panel immediately."""
    use_state(store.starred_ids)
    key = f"{kind[0]}:{item_id}"
    on = store.is_starred(key)

    def toggle(e):
        if on:
            if kind == "song":
                store.unstar_song(item_id)
            else:
                store.unstar_album(item_id)
        else:
            if kind == "song":
                store.star_song(item_id)
            else:
                store.star_album(item_id)

    return ft.IconButton(
        icon=ft.Icons.FAVORITE if on else ft.Icons.FAVORITE_BORDER,
        icon_size=size, tooltip=tooltip,
        icon_color=ft.Colors.PRIMARY if on else ft.Colors.ON_SURFACE_VARIANT,
        on_click=toggle,
    )


@component
def AlbumTile(store, album, size=180):
    cover = store.cover_url_for(album) if store.proxy else None
    return ft.Container(
        width=size,
        on_click=lambda e, a=album: store.go_album(a.id),
        content=ft.Column(
            spacing=4,
            controls=[
                (ft.Image(src=cover, width=size, height=size,
                          fit=ft.BoxFit.COVER, border_radius=8)
                 if cover else placeholder(size)),
                ft.Text(album.name or "", size=13, weight=ft.FontWeight.BOLD,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Text(album.artist or "", size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ],
        ),
    )


album_tile = memo(AlbumTile)


def album_grid(store, albums, size=180):
    tiles = [album_tile(store, a, size=size, key=f"a:{a.id}")
             for a in (albums or [])]
    if not tiles:
        tiles = [ft.Text("No albums", color=ft.Colors.ON_SURFACE_VARIANT, size=13)]
    return ft.Row(controls=tiles, wrap=True, spacing=14, run_spacing=14)


def fmt_dur(seconds):
    try:
        return fmt_ms(int(seconds or 0) * 1000)
    except (TypeError, ValueError):
        return "0:00"


@component
def SongRow(store, song, index, songs, show_artist=True):
    cover = store.cover_url_for(song) if store.proxy else None
    artist = (song.artist if show_artist else "") or ""
    key = f"s:{song.id}"

    def toggle_star(e):
        if store.is_starred(key):
            store.unstar_song(song.id)
        else:
            store.star_song(song.id)

    title = ft.Container(
        expand=True,
        content=ft.Column(
            spacing=0,
            alignment=ft.MainAxisAlignment.CENTER,
            controls=[
                ft.Text(song.title or "", size=13, weight=ft.FontWeight.BOLD,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                *([ft.Text(artist, size=11,
                           color=ft.Colors.ON_SURFACE_VARIANT,
                           max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)]
                  if artist else []),
            ],
        ),
    )

    menu_items = [
        ft.PopupMenuItem(
            content="Play", icon=ft.Icons.PLAY_ARROW,
            on_click=lambda e, s=songs, i=index: store.play(s, i)),
        ft.PopupMenuItem(
            content="Play next", icon=ft.Icons.PLAYLIST_ADD,
            on_click=lambda e, s=song: store.add_next([s])),
        ft.PopupMenuItem(
            content="Add to queue", icon=ft.Icons.QUEUE_MUSIC,
            on_click=lambda e, s=song: store.add_to_queue([s])),
        ft.PopupMenuItem(),  # divider
    ]
    if getattr(song, "artist_id", None):
        menu_items.append(ft.PopupMenuItem(
            content="Go to artist", icon=ft.Icons.PERSON,
            on_click=lambda e, aid=song.artist_id: store.go_artist(aid)))
    if getattr(song, "album_id", None):
        menu_items.append(ft.PopupMenuItem(
            content="Go to album", icon=ft.Icons.ALBUM,
            on_click=lambda e, aid=song.album_id: store.go_album(aid)))
    menu_items += [
        ft.PopupMenuItem(),  # divider
        ft.PopupMenuItem(
            content="Favorite", icon=ft.Icons.FAVORITE_BORDER,
            on_click=toggle_star),
    ]

    row = ft.Container(
        height=52,
        padding=ft.Padding.symmetric(horizontal=8, vertical=4),
        border_radius=6,
        ink=True,
        on_click=lambda e, s=song, i=index, lst=songs: store.play(lst, i),
        content=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(str(index + 1), size=12, width=28,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Image(src=cover, width=40, height=40, fit=ft.BoxFit.COVER,
                         border_radius=6, error_content=placeholder(40)),
                title,
                StarButton(store, "song", song.id),
                ft.Text(fmt_dur(song.duration), size=11, width=38,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        text_align=ft.TextAlign.RIGHT),
            ],
        ),
    )
    return ft.ContextMenu(
        content=row,
        secondary_items=menu_items,
    )


song_row = memo(SongRow)


def song_list(store, songs, play_all_label="Play all", show_artist=True,
              play_all=True):
    songs = songs or []
    rows = []
    if songs and play_all:
        rows.append(ft.Row(
            controls=[
                ft.TextButton(play_all_label,
                              on_click=lambda e, s=songs: store.play(s, 0)),
                ft.Container(expand=True),
            ],
            spacing=8,
        ))
    for i, s in enumerate(songs):
        rows.append(song_row(store, s, i, songs,
                             show_artist=show_artist, key=f"s:{s.id}"))
    if not rows:
        rows.append(ft.Text("No songs",
                            color=ft.Colors.ON_SURFACE_VARIANT, size=13))
    return rows