# cmoss

A lightweight desktop client for [OpenSubsonic](https://opensubsonic.net/) / [Navidrome](https://www.navidrome.org/) media servers, built with [Flet](https://flet.dev/) and [libmpv](https://mpv.io/).

## Features

- Subsonic / OpenSubsonic / Navidrome compatible
- Streaming with gapless playback (libmpv)
- Local caching proxy (transparent for player and cover art)
- Dynamic theming from cover art palette
- Synced lyrics (LRCLIB + Genius fallback)
- OS media integration (MPRIS on Linux, SMTC on Windows)
- Frameless window with custom titlebar
- Auto-update from GitHub releases (PyInstaller builds)

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Install

```bash
git clone https://github.com/kptmx/cmoss.git
cd cmoss
uv sync
```

## Run

Desktop:

```bash
uv run flet run
```

Web:

```bash
uv run flet run --web
```

## Build (Windows from Linux)

Requires WINE with Python 3.14 and all dependencies installed. Drop a Windows `libmpv-2.dll` into `winmpv/`.

```bash
./build_windows.sh
```

Output: `winbuild/dist/cmoss.exe`

## Project structure

```
cmoss/
├── bootstrap.py    # PyInstaller update overlay (MetaPathFinder)
├── updater.py      # GitHub release check + download
├── config.py       # Config persistence (~/.config/cmoss/)
├── server.py       # Async OpenSubsonic client
├── proxy.py        # Local caching HTTP proxy
├── player.py       # mpv playback model
├── store.py        # Reactive ViewModel (DataBox)
├── lyrics.py       # Synced lyrics fetching
├── palette.py      # Cover art color extraction
├── media_control.py # MPRIS / SMTC bridge
├── reactive.py     # DataBox observable cells
└── ui/
    ├── screens.py  # Declarative Flet screens
    └── widgets.py  # Shared widgets
```

## License

MIT
