"""Extract a Spotify-style color palette from album cover art.

The app theme (Material seed + page background) is derived from the current
track's cover. Pure Pillow + color math; every failure degrades to `None` so
the caller keeps the previous theme instead of flashing a wrong one.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover
    Image = None
    _PIL_AVAILABLE = False

SAMPLE = 48          # thumbnail size used for analysis
COLORS = 16          # palette depth after quantization
MIN_SHARE = 0.02     # min pixel share for a color to be a candidate
BG_NEUTRAL = (12, 12, 16)  # near-black tint mixed into the page background
BG_MIX = 0.86        # how far to push the dominant color toward BG_NEUTRAL
BG_DESAT = 0.5       # background desaturation amount
SEED_MIN_LUM = 0.35  # keep the seed bright enough to read against dark


def _lum(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _sat(rgb):
    mx, mn = max(rgb), min(rgb)
    return (mx - mn) / max(1, mx)


def _mix(a, b, t):
    return tuple(round(x * (1 - t) + y * t) for x, y in zip(a, b))


def _desat(rgb, k):
    g = round(_lum(rgb) * 255)
    return _mix(rgb, (g, g, g), k)


def _lift(rgb, min_lum):
    """Mix toward white until luminance reaches `min_lum` (keeps the hue)."""
    lum = _lum(rgb)
    if lum >= min_lum:
        return rgb
    f = (min_lum - lum) / max(1e-6, 1.0 - lum)
    return _mix(rgb, (255, 255, 255), f)


def _hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, round(c))) for c in rgb])


def extract_palette(path) -> dict | None:
    """Return ``{"seed": "#hex", "bg": "#hex"}`` for a cover image, or None.

    * `seed` — the most saturated color present (count-weighted), brightened
      enough to work as a Material color-scheme seed in dark mode.
    * `bg`   — the dominant color pushed toward near-black and desaturated, for
      the page background.
    """
    if not _PIL_AVAILABLE:
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((SAMPLE, SAMPLE))
            q = im.quantize(colors=COLORS, method=Image.Quantize.MEDIANCUT,
                            dither=Image.Dither.NONE)
            pal = q.getpalette()
            counts = q.getcolors()
    except Exception as e:
        log.debug("palette decode failed for %s: %s", path, e)
        return None
    if not counts:
        return None

    samples = [(n, (pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]))
               for n, i in counts]
    total = sum(n for n, _ in samples)
    dominant = max(samples, key=lambda s: s[0])[1]

    candidates = [(n, rgb) for n, rgb in samples
                  if n >= total * MIN_SHARE]
    accent, best = None, -1.0
    for n, rgb in candidates:
        s, l = _sat(rgb), _lum(rgb)
        if s < 0.18 or l < 0.30 or l > 0.95:
            continue
        score = n * s * (0.15 + l)
        if score > best:
            best, accent = score, rgb
    if accent is None:
        accent = (max(candidates, key=lambda c: c[0])[1] if candidates
                  else dominant)
    accent = _lift(accent, SEED_MIN_LUM)
    bg = _desat(_mix(dominant, BG_NEUTRAL, BG_MIX), BG_DESAT)
    return {"seed": _hex(accent), "bg": _hex(bg)}
