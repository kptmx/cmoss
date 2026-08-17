#!/usr/bin/env bash
#
# Build cmoss for Windows from Linux via WINE + `flet pack`.
#
# The WINE Python (3.14) must have:
#   flet==0.86.5 flet-cli==0.86.5 flet-desktop==0.86.5 PyInstaller
#   aiohttp beautifulsoup4 python-mpv Pillow jeepney mashumaro
#   winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections
#   winrt-Windows.Media winrt-Windows.System
# plus `libopensonic` copied into its site-packages (not published on PyPI).
#
# Optional playback support: drop a Windows libmpv runtime into ./winmpv/
# (libmpv-2.dll or mpv-2.dll plus every DLL it depends on). Without it the
# build still produces a working UI that degrades to a NullPlayer.
#
# Overridable env: WINE, WINEPREFIX, WINE_PY, DIST, MPV_DIR, DEBUG_CONSOLE=1
#
# Usage: ./build_windows.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WINE="${WINE:-wine}"
WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
WINE_PY="${WINE_PY:-$WINEPREFIX/drive_c/users/kptmx/AppData/Local/Programs/Python/Python314/python.exe}"
DIST="${DIST:-winbuild/dist}"
MPV_DIR="${MPV_DIR:-$HERE/winmpv}"

export WINEDEBUG="${WINEDEBUG:--all}"
export WINEPREFIX

log() { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }

# /home/x/y  ->  Z:\home\x\y
to_win() { printf 'Z:%s' "${1//\//\\}"; }

WINE_FLE="$(dirname "$WINE_PY")/Scripts/flet.exe"

if ! "$WINE" "$WINE_PY" -c 'import flet' >/dev/null 2>&1; then
    echo "ERROR: cannot run the WINE Python at $WINE_PY" >&2
    exit 1
fi

log "WINE Python: $WINE_PY"
"$WINE" "$WINE_PY" -c 'import flet, PyInstaller; print(f"  flet {flet.__version__} / PyInstaller {PyInstaller.__version__}")'

ARGS=(pack "$(to_win "$HERE/main.py")"
      --name cmoss
      --product-name "cmoss"
      --file-description "cmoss OpenSubsonic/Navidrome client"
      --product-version 0.1.0
      --file-version 0.1.0
      --company-name kptmx
      --copyright "Copyright (C) 2026 kptmx"
      --distpath "$DIST"
      -y)

if [ -n "${DEBUG_CONSOLE:-}" ]; then
    ARGS+=(--debug-console)
fi

MPV_DLL=""
if [ -d "$MPV_DIR" ] && ls "$MPV_DIR"/*.dll >/dev/null 2>&1; then
    log "Bundling mpv runtime from $MPV_DIR"
    for dll in "$MPV_DIR"/*.dll; do
        ARGS+=(--add-binary "$(to_win "$dll"):.")
    done
elif ls "$HERE"/libmpv-2.dll "$HERE"/mpv-2.dll "$HERE"/mpv-1.dll >/dev/null 2>&1; then
    for name in libmpv-2.dll mpv-2.dll mpv-1.dll; do
        if [ -f "$HERE/$name" ]; then
            MPV_DLL="$HERE/$name"
            break
        fi
    done
    log "Bundling mpv DLL $MPV_DLL"
    ARGS+=(--add-binary "$(to_win "$MPV_DLL"):.")
else
    echo "WARNING: no mpv runtime found (winmpv/ or *.dll); build will use NullPlayer" >&2
fi

log "Running: flet pack main.py ..."
"$WINE" "$WINE_FLE" "${ARGS[@]}"

EXE="$HERE/$DIST/cmoss.exe"
if [ -f "$EXE" ]; then
    log "Build complete -> $EXE"
else
    echo "ERROR: expected output $EXE not found" >&2
    exit 1
fi
