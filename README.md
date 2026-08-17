# cmoss

Desktop client for [OpenSubsonic](https://opensubsonic.net/) / [Navidrome](https://www.navidrome.org/) servers.

## Features

- Gapless playback (libmpv)
- Local caching proxy
- Dynamic theming from cover art
- Synced lyrics (LRCLIB + Genius)
- MPRIS / SMTC media integration
- Auto-update from GitHub releases

## Install

```bash
git clone https://github.com/kptmx/cmoss.git
cd cmoss
uv sync
```

## Run

```bash
uv run flet run
```

## Build (Windows from Linux)

Requires WINE with Python 3.14. Drop `libmpv-2.dll` into `winmpv/`.

```bash
./build_windows.sh
```

## Disclaimer

This project was largely written with AI assistance. The code is provided as-is — I make no guarantees about its correctness, security, or fitness for any purpose. Use it at your own risk.

## License

MIT
