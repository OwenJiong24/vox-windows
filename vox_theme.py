"""Shared visual design tokens and helpers (2026 palette, DPI, type scale).

Where this came from
--------------------
These helpers were written for the canvas-based GUI rebuild (commit 0f5e9fc, extended by
77c21aa) but the rebuild itself was reverted in 978b82f: its custom card container
(RoundedPanel, which sized itself from a place()d inner frame) rendered cards 33 px wide,
and the two-column tab layout clipped content on every tab that used it.

The helpers below are the parts of that work that are PURE STYLE or PURE GEOMETRY, with
no dependency on the reverted widget system. Keeping them in their own module lets the
working ttk-based GUI adopt the palette, the CJK font stack, the point-based type scale
and per-monitor v2 DPI without importing anything that can affect its layout.

Nothing here creates a widget or manages geometry, so adopting it cannot reintroduce the
container-sizing bug.

Provenance: extracted verbatim from 77c21aa:vox_windows/vox_gui.py
(_set_process_dpi_awareness, _display_scale, px, pt, detect_os_theme, build_palette,
resolve_ui_family, _rr_pts, round_rect, rr_radius_for, _wrap_label, _text_px and the
PT_* / CJK_FONT_STACK / CARD_* token block).
"""

from __future__ import annotations

import math
import os
import sys
import tkinter as tk
import tkinter.font as tkfont

# `core` is used only for get_dpi_scale() in _display_scale(). Extracting these helpers
# out of vox_gui.py WITHOUT their imports silently broke resolve_ui_family(): the
# NameError from the missing tkfont was swallowed by its own `except Exception`, leaving
# `available` empty so it ALWAYS fell back to "Segoe UI" — even though Noto Sans SC is
# installed and first in the stack. audit_consistency.py flagged all three missing names
# (core / math / tkfont) the moment the module existed.
import vox_core as core


def _set_process_dpi_awareness():
    """Claim per-monitor v2 DPI awareness.

    Must run before the first Tk window, otherwise Windows hands the process a
    virtualised desktop and the window is either blurred or the wrong size.
    Returns the call's success as a bool (the effective DPI is read separately,
    from core.get_dpi_scale(), so a shcore quirk cannot invalidate awareness).
    """
    try:
        import ctypes
        try:
            # 2 == PROCESS_PER_MONITOR_DPI_AWARE
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return True
        except Exception:
            # Older builds have no shcore: fall back to system DPI awareness so
            # at least the window is not bitmap-stretched.
            ctypes.windll.user32.SetProcessDPIAware()
            return True
    except Exception:
        return False


DPI_AWARE = _set_process_dpi_awareness()


def _display_scale():
    """Layout scale factor from the real DPI (fonts stay in POINTS)."""
    try:
        return float(core.get_dpi_scale())
    except Exception:
        try:
            import ctypes
            return ctypes.windll.user32.GetDpiForSystem() / 96.0
        except Exception:
            return 1.0


SCALE = _display_scale()


def px(v):
    """Scale a layout value in base points to device pixels (never below 1)."""
    try:
        return max(1, int(round(float(v) * SCALE)))
    except Exception:
        return max(1, int(v))


def pt(size):
    """Scale a point-based type size onto the same ramp as px()."""
    try:
        return max(6, int(round(float(size) * SCALE)))
    except Exception:
        return max(6, int(size))


PT_TITLE = 26


PT_SUB   = 12


PT_SEC   = 11


PT_ROW   = 13


PT_REPL  = 15


PT_FOOT  = 11


PT_SMALL = 10


PT_MONO  = 9


def detect_os_theme():
    """'light' | 'dark' from the Windows personalization registry.

    VOX_GUI_THEME=light|dark overrides it for QA screenshots (and for anyone
    who wants the console pinned to one theme regardless of the OS).
    """
    override = (os.environ.get("VOX_GUI_THEME") or "").strip().lower()
    if override in ("light", "dark"):
        return override
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return "light" if int(value) else "dark"
    except Exception:
        return "light"


def build_palette(theme):
    """The full light or dark token pair.

    Values are the 2026 tonal palette from the native craft reference; dark is a
    TRUE BLACK background with elevated (lighter) cards — never derived by
    inverting light.  Tonal icon tiles carry their own `*_bg`/`*_fg` pairs and
    the dark tint inverts (dark tint, brighter glyph); the light pair is never
    reused in dark.
    """
    light = {
        "bg":           "#F2F2F7",
        "card":         "#FFFFFF",
        "entry":        "#F2F2F7",
        "text":         "#1D1D1F",
        "text2":        "#6E6E73",
        "text3":        "#C5C5C7",
        "accent":       "#0088FF",
        "accent_hover": "#0071DB",
        "accent_soft":  "#E7F2FF",
        "switch_on":    "#34C759",
        "switch_off":   "#D8D8DC",
        "sep":          "#C6C6C8",
        "hairline":     "#E7E7EA",
        "hero_bg":      "#F3F7FF",
        "hero_glow":    "#DCE9FF",
        "shadow":       "#D9D9DE",
        "shadow_soft":  "#E9E9EE",
        "dot_on":       "#34C759",
        "dot_off":      "#B0B0B6",
        "warn_bg":      "#FFF6E6",
        "warn_fg":      "#B25B00",
        "danger":       "#FF3B30",
        "danger_bg":    "#FFEBEA",
        "success_fg":   "#1E9E4A",
        "sel_bg":       "#DCE9FF",
        # ── tonal icon tiles (light): soft tint bg + same-hue saturated glyph ──
        "tile_blue_bg":   "#E5F0FF",
        "tile_green_bg":  "#E4F7EC",
        "tile_orange_bg": "#FFEFDF",
        "tile_purple_bg": "#F2E9FB",
        "tile_grey_bg":   "#ECECEF",
        "tile_blue_fg":   "#0A84FF",
        "tile_green_fg":  "#24A14A",
        "tile_orange_fg": "#F07E0B",
        "tile_purple_fg": "#9B4FD6",
        "tile_grey_fg":   "#6E6E73",
    }
    dark = {
        "bg":           "#000000",   # true black; cards are elevated, not grey-on-grey
        "card":         "#1C1C1E",
        "entry":        "#2C2C2E",
        "text":         "#F5F5F7",
        "text2":        "#98989D",
        "text3":        "#5A5A5F",
        "accent":       "#0091FF",   # accent brightens in dark
        "accent_hover": "#43ACFF",
        "accent_soft":  "#10233B",
        "switch_on":    "#30D158",
        "switch_off":   "#48484A",
        "sep":          "#3E3E41",
        "hairline":     "#2A2A2C",   # the dark card's own edge, no shadow
        "hero_bg":      "#0E1626",   # hero tint goes dark blue, not light
        "hero_glow":    "#12233A",
        "shadow":       "#000000",   # in dark the shadow is a shape, not a darkening
        "shadow_soft":  "#000000",
        "dot_on":       "#30D158",
        "dot_off":      "#6E6E73",
        "warn_bg":      "#2A1D0E",
        "warn_fg":      "#FF9F0A",
        "danger":       "#FF453A",
        "danger_bg":    "#2C1414",
        "success_fg":   "#30D158",
        "sel_bg":       "#123A5C",
        # ── tonal icon tiles (dark): the tint inverts, the glyph brightens ──
        "tile_blue_bg":   "#16233C",
        "tile_green_bg":  "#12271C",
        "tile_orange_bg": "#2A1D0E",
        "tile_purple_bg": "#211430",
        "tile_grey_bg":   "#262628",
        "tile_blue_fg":   "#4DA3FF",
        "tile_green_fg":  "#30D158",
        "tile_orange_fg": "#FF9F0A",
        "tile_purple_fg": "#BF5AF2",
        "tile_grey_fg":   "#98989D",
    }
    return dict(light) if theme == "light" else dict(dark)


CJK_FONT_STACK = (
    "Noto Sans SC",
    "Source Han Sans SC",
    "Segoe UI Variable Text",
    "Segoe UI",
    "Microsoft YaHei UI",
    "PingFang SC",
)


_W_BOLD = "bold"


def resolve_ui_family(root=None):
    """First CJK-capable family from CJK_FONT_STACK that Tk resolves."""
    try:
        available = {f.lower() for f in tkfont.families(root)}
    except Exception:
        available = set()
    for fam in CJK_FONT_STACK:
        if fam.lower() in available:
            return fam
    return "Segoe UI"


def _rr_pts(x1, y1, x2, y2, r):
    """Corner-arc points for a rounded rect (8 segments per corner).

    Sampling the true arc densely is what removes the kink/seam a naive
    `smooth=True` one-point-per-corner polygon leaves at the top-left corner.
    """
    x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
    r = float(max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0)))
    if r < 0.5:
        # Degenerate (or absurdly small) radius: a hard rectangle.  Never return
        # an empty list — create_polygon([]) raises IndexError, not "no shape".
        return [x1, y1, x2, y1, x2, y2, x1, y2]
    corners = ((x1 + r, y1 + r, 180, 270),
               (x2 - r, y1 + r, 270, 360),
               (x2 - r, y2 - r, 0, 90),
               (x1 + r, y2 - r, 90, 180))
    pts = []
    for cx, cy, a0, a1 in corners:
        for i in range(9):
            a = math.radians(a0 + (a1 - a0) * i / 8.0)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return pts


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    """Draw a rounded rectangle on `cv` via dense corner sampling, smooth=False."""
    pts = _rr_pts(x1, y1, x2, y2, r)
    if len(pts) < 6:
        pts = [x1, y1, x1 + 1, y1 + 1, x1 + 2, y2]
    return cv.create_polygon(pts, smooth=False, **kw)


def rr_radius_for(widget):
    """Generous card radius (~14pt) — 6–8pt reads as a 2015 form."""
    return px(14)


CARD_RADIUS = 14


TILE_RADIUS = 8


CARD_PAD_X = 20


CARD_PAD_Y = 14


def _wrap_label(text, max_chars=30):
    """Soft-wrap `text` with newlines so a single-line widget cannot demand huge width.

    ttk widgets measure their label on one line and reject -wraplength, so embedded
    newlines are the only way to bound the request. Breaks are placed at a nearby
    space / slash / CJK punctuation when one exists, otherwise the line is broken hard.

    Pure function (no Tk), so it is unit-tested directly.
    """
    t = str(text or "")
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    breaks = " \t/、，,；;）)】」》"
    out, line = [], ""
    for ch in t:
        line += ch
        if len(line) >= max_chars:
            # Prefer to break just after the last break opportunity in this line.
            cut = -1
            for i in range(len(line) - 1, max(0, len(line) - 12), -1):
                if line[i] in breaks:
                    cut = i + 1
                    break
            if cut <= 0:
                cut = len(line)
            out.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
    if line:
        out.append(line)
    return "\n".join(out)


def _text_px(text, font_tuple):
    """Rendered pixel width of `text` in `font_tuple` (for canvas-sized buttons)."""
    try:
        f = tkfont.Font(font=font_tuple)
        return f.measure(text)
    except Exception:
        return int(len(text) * 9)


# Apply per-monitor v2 DPI awareness at import time — it must be set before the first
# window exists, and this module is imported before any Tk object is created. The call
# sits at the BOTTOM on purpose: putting it in the header called it before it was
# defined.
_set_process_dpi_awareness()


def palette_for_current_theme():
    """(theme_name, palette) for whatever the OS is currently using."""
    name = detect_os_theme()
    return name, build_palette(name)
