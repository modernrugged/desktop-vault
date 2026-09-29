"""Icons drawn in code.

Keeping the artwork here means the app is a pure-Python tree with no binary
assets to ship, and the icons pick up the theme colours automatically.
"""

from __future__ import annotations

import tkinter as tk

TRANSPARENT = "."

_FOLDER = [
    "................",
    "................",
    "..eeeee.........",
    ".eFFFFFe........",
    ".eFFFFFeeeeeeee.",
    ".eFFFFFFFFFFFFe.",
    ".eFFFFFFFFFFFFe.",
    ".eFFFFFFFFFFFFe.",
    ".eFFFFFFFFFFFFe.",
    ".eFFFFFFFFFFFFe.",
    ".eFFFFFFFFFFFFe.",
    ".eFFFFFFFFFFFFe.",
    ".eFFFFFFFFFFFFe.",
    "..eeeeeeeeeeee..",
    "................",
    "................",
]

_FILE = [
    "................",
    "...bbbbbbbb.....",
    "...bwwwwwwbb....",
    "...bwwwwwwbcb...",
    "...bwwwwwwwwwb..",
    "...bwwmmmmwwwb..",
    "...bwwwwwwwwwb..",
    "...bwwmmmmmwwb..",
    "...bwwwwwwwwwb..",
    "...bwwmmmmmwwb..",
    "...bwwwwwwwwwb..",
    "...bwwmmmwwwwb..",
    "...bwwwwwwwwwb..",
    "...bbbbbbbbbbb..",
    "................",
    "................",
]

_LOCK = [
    "................",
    "................",
    ".....aaaaaa.....",
    "....aa....aa....",
    "....a......a....",
    "....a......a....",
    "...aa......aa...",
    "..AAAAAAAAAAAA..",
    "..AAAAAAAAAAAA..",
    "..AAAAAkkAAAAA..",
    "..AAAAAkkAAAAA..",
    "..AAAAAkkAAAAA..",
    "..AAAAAAAAAAAA..",
    "..AAAAAAAAAAAA..",
    "................",
    "................",
]

_PALETTE = {
    ".": "#000001",          # stand-in colour, made transparent afterwards
    "F": "#E2A03F",          # folder fill
    "e": "#B77F2C",          # folder edge
    "w": "#D5D9E2",          # paper
    "b": "#6E7789",          # paper edge
    "c": "#959DAF",          # folded corner
    "m": "#8A92A4",          # text lines
    "a": "#8A92A4",          # shackle
    "A": "#5B8DEF",          # lock body
    "k": "#1B1E25",          # keyhole
}


def _build(rows, palette, scale: int = 1) -> tk.PhotoImage:
    height, width = len(rows), len(rows[0])
    img = tk.PhotoImage(width=width * scale, height=height * scale)
    data = " ".join(
        "{" + " ".join(palette[c] for c in row for _ in range(scale)) + "}"
        for row in rows for _ in range(scale)
    )
    img.put(data)
    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            if char == TRANSPARENT:
                for dy in range(scale):
                    for dx in range(scale):
                        img.transparency_set(x * scale + dx, y * scale + dy, True)
    return img


def load(_root: tk.Misc) -> dict:
    """Build every icon.  Must be called after a Tk root exists.

    These are pixel bitmaps, so they can only be enlarged by a whole number.
    The display scale is rounded to the nearest integer: 1x up to 125%, 2x at
    150% and above, which keeps them in proportion with the scaled row height
    without any blurry resampling.
    """
    from . import theme
    step = max(1, int(round(theme.SCALE)))
    return {
        "folder": _build(_FOLDER, _PALETTE, scale=step),
        "file": _build(_FILE, _PALETTE, scale=step),
        "lock": _build(_LOCK, _PALETTE, scale=step),
        "lock_big": _build(_LOCK, _PALETTE, scale=4 * step),
        "lock_app": _build(_LOCK, _PALETTE, scale=2 * step),
    }
