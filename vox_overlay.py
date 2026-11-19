#!/usr/bin/env python3
"""
Vox Modern High-DPI Dynamic Island Floating Pill.

A sleek, modern, non-activating floating dynamic pill for Windows:
- Native Windows High-DPI V2 scaling (auto-adapts to 100%, 150%, 200%, 225% 4K/5K displays)
- Dark glass / macOS Dynamic Island capsule pill shape with anti-aliasing
- Draggable with mouse: click and move anywhere, remembers customized screen position, double-click to reset
- Default position: Bottom-center above taskbar (uses Windows work area metrics)
- Dynamic 9-bar audio frequency wave visualization while recording
- Real-time Sonix-style streaming preview text with animated typing cursor
- Uses Win32 WS_EX_NOACTIVATE & WS_EX_TOOLWINDOW to ensure it NEVER steals focus from target text editors
"""

import os
import sys
import json
import time
import math
import functools
import threading
import tkinter as tk
from datetime import datetime

# Import core for DPI scale, screen workarea and config
import vox_core as core

# Win32 window style manipulation
_HAS_WIN32 = False
try:
    import ctypes
    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_TOPMOST    = 0x00000008
    WS_EX_LAYERED    = 0x00080000
    LWA_ALPHA        = 0x00000002
    LWA_COLORKEY     = 0x00000001
    # SetLayeredWindowAttributes(hwnd, crKey, bAlpha, dwFlags) -> BOOL
    user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_byte, ctypes.c_ulong
    ]
    user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
    _HAS_WIN32 = True
except Exception:
    user32 = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "overlay_state.json")

# Theme colors (Modern Fluent / Dynamic Island Dark Glass)
COLOR_BG       = "#121214"  # Transparent key
COLOR_PILL     = "#18181b"  # Inner pill fill (zinc-900)
COLOR_BORDER   = "#3f3f46"  # Crisp glass border (zinc-700)
COLOR_TEXT     = "#f8fafc"  # Bright readable text
COLOR_MUTED    = "#a1a1aa"  # Subdued text
COLOR_REC      = "#ef4444"  # Red recording dot
COLOR_TRANS    = "#f59e0b"  # Amber transcribing
COLOR_DONE     = "#10b981"  # Emerald success
COLOR_CANCEL   = "#71717a"  # Gray cancel
FONT_FAMILY    = "Microsoft YaHei UI"


@functools.lru_cache(maxsize=64)
def _stadium_points_cached(w, h, r0, inset, per_arc):
    """G4.1: exact stadium outline as a sampled point list — four straight sides
    plus four quarter-arc corners (r0 fixed), no seams, no B-spline wobble.
    `inset` offsets the whole outline (used for the inner fill layer).

    PERF: memoized on (w, h, r0, inset, per_arc). The pill outline only changes when
    the pill size changes, but it used to be rebuilt twice per 40 ms frame (384 trig
    calls + a 394-int list each time; measured 47.7 us/call). Returns a tuple so the
    cache entry can never be mutated by a caller."""
    w -= 2 * inset
    h -= 2 * inset
    r0 = min(r0, w // 2, h // 2)
    if r0 <= 0:
        return (inset, inset, w + inset, inset, w + inset, h + inset, inset, h + inset)
    pts = [(r0 + inset, inset), (w - r0 + inset, inset)]

    def arc(cx, cy, a0, a1):
        for i in range(1, per_arc + 1):
            a = math.radians(a0 + (a1 - a0) * i / per_arc)
            pts.append((cx + r0 * math.cos(a), cy + r0 * math.sin(a)))

    arc(w - r0 + inset, r0 + inset, -90, 0)     # top-right
    pts.append((w + inset, h - r0 + inset))
    arc(w - r0 + inset, h - r0 + inset, 0, 90)  # bottom-right
    pts.append((r0 + inset, h + inset))
    arc(r0 + inset, h - r0 + inset, 90, 180)    # bottom-left
    pts.append((inset, r0 + inset))
    arc(r0 + inset, r0 + inset, 180, 270)       # top-left
    flat = []
    for x, y in pts:
        flat.append(int(round(x)))
        flat.append(int(round(y)))
    return tuple(flat)


class VoxOverlay:
    """
    High-DPI Adaptive Controller and Canvas UI for the floating capsule pill.
    Can be run in-process or standalone via StandaloneOverlayPoller.
    """

    def __init__(self, master=None, position="bottom_center"):
        core.init_dpi_awareness()
        self.scale = core.get_dpi_scale()
        self.own_master = master is None
        self.root = tk.Tk() if self.own_master else tk.Toplevel(master)
        self.position = position
        self.cfg = core.ConfigManager.load()

        # Enlarged base dimensions for 4K/5K comfortable readability
        ov_cfg = self.cfg.get("overlay", {})
        self.base_w = ov_cfg.get("base_width", 540)
        self.base_h = ov_cfg.get("base_height", 66)
        # User-dragged custom width (bubble size adjustment); takes precedence over base_width.
        self.custom_width = ov_cfg.get("custom_width")
        if self.custom_width:
            self.base_w = int(self.custom_width)
        self.width = int(round(self.base_w * self.scale))
        self.height = int(round(self.base_h * self.scale))
        # G4: FIXED corner radius = the base pill's capsule radius. As the pill grows
        # taller it stays a stadium (straight sides, same corner arcs) — a balloon
        # capsule (r=h/2) would squeeze the content width to zero on tall pills.
        self.base_r = max(self.s(16), self.height // 2)
        self.custom_x = ov_cfg.get("custom_x")
        self.custom_y = ov_cfg.get("custom_y")

        # E1: user-adjustable bubble appearance (admin panel "气泡外观" card).
        # font_scale/line_scale multiply the s() design units; opacity = window alpha %.
        ui = ov_cfg.get("ui", {})
        self.ui_font_scale = max(0.6, min(2.0, float(ui.get("font_scale", 1.0))))
        self.ui_line_scale = max(0.7, min(2.0, float(ui.get("line_scale", 1.0))))
        self.ui_opacity = max(40, min(100, int(ui.get("opacity", 85))))
        # F6: done-state display durations (ms); the daemon mirrors these when pushing idle.
        self.done_short_ms = max(500, int(ui.get("done_short_ms", 2000)))
        self.done_long_ms = max(2000, int(ui.get("done_long_ms", 5000)))
        self._cfg_mtime = self._cfg_file_mtime()

        self.state = "idle"  # idle, recording, transcribing, done, cancelled, error
        self.text = ""
        self.hint = ""
        self.latency = None  # G6.4: last transcription latency (done-state timer)
        self.preview_text = ""
        self.level = 0.0      # 0.0 to 1.0 (audio RMS/peak)
        self.start_time = 0.0
        self._hide_timer = None
        self._tick_job = None
        self._anim_step = 0
        self.visible = False

        # Adaptive content height (complaint #1/#5): the pill grows vertically to fit
        # long done/error text instead of truncating it to a single line. self.height /
        # self.base_h remain the DEFAULT capsule size (tests assert width==base_w*scale,
        # height==base_h*scale); self.current_h is the LIVE height used for geometry,
        # canvas and drawing. It shrinks back to base when entering transient states.
        self.current_h = self.height
        # F4: grow (nearly) unbounded — capped only by ~62% of the screen work area
        # so the whole text is always visible (user: 之前 3x 上限只看得到一小部分).
        try:
            _sx, _sy, _sw, _sh = core.get_screen_workarea()
            self.max_content_h = max(self.height * 3, int(_sh * 0.62))
            # G6.3: the window is a FIXED max size (both dims). Windows drops the
            # layered/transparent-color attributes whenever a window is RESIZED
            # (WM_SETWINDOWPOS), which cannot be reliably restored — so the pill
            # grows/shrinks purely by redrawing inside this fixed window (bottom-left
            # anchored), and width changes never touch the geometry. See _draw_pill.
            # max_w must cover the widest the pill can be (base + s(800) grip drag) but
            # is capped to the work area so a multi-monitor span doesn't make a giant
            # off-screen window.
            self.max_w = max(self.width, min(_sw, int(self.s(1400))))
        except Exception:
            self.max_content_h = int(round(self.base_h * self.scale * 3))
            self.max_w = self.width
        self._render_lines = None       # precomputed wrapped lines for done/error state
        self._render_line_h = 0         # spacing used when those lines were wrapped
        self._render_font_scale = 1.0   # font shrink applied before the height cap (no scroll)
        # Streaming-preview multi-line layout (Sonic-style upward growth while recording):
        # precomputed wrapped lines + the font scale applied (font shrinks before the cap).
        self._preview_lines = None
        self._preview_line_h = 0
        self._preview_font_scale = 1.0
        self._preview_cache_key = None
        # PERF: the canvas keeps ONE reusable item inventory (created by
        # _create_canvas_items) and this cache remembers the last geometry/options
        # applied to each item, so a frame only touches what actually changed.
        self._items = {}
        self._item_cache = {}
        self._pool_n = 0
        # PERF: measured (cjk, latin) advance widths memoized by font tuple; the layout
        # drivers ask for these 12x per wrap (4 font scales x 3 fixed-point iterations).
        self._char_width_cache = {}
        # Roles shown by the frame currently being drawn (see _hide_unused).
        self._used = set()
        # PERF: last cursor applied to the canvas (<Motion> fires continuously).
        self._cursor_name = None
        # Current engine indicator (A7): set from overlay_state.json "engine" field.
        self.engine = ""
        self._win_x = 0
        self._win_y = 0
        # Resize (drag bottom-right grip) tracking.
        self._is_resizing = False
        self._resize_start_x_root = 0
        self._resize_start_w = 0

        # Drag tracking
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._drag_offset_x = 0
        self._drag_offset_y = 0
        self._is_dragging = False

        self._init_window()
        self._create_widgets()
        self._bind_drag_events()
        self.hide()

    def s(self, val):
        """Helper to scale a value according to display DPI."""
        return int(round(val * self.scale))

    @staticmethod
    def _stadium_inset_at(h, y, r0):
        """G4: horizontal distance from the window edge to the STADIUM boundary at
        height y (window height h, FIXED corner radius r0). The pill keeps its base
        corner radius as it grows (a stadium, not a balloon capsule): rows inside the
        top/bottom corner zones must inset to stay inside the arc; the middle band has
        the full width. At base height (h == 2*r0) this equals the true capsule."""
        if r0 <= 0:
            return 0
        if y < r0:
            dy = r0 - y
        elif y > h - r0:
            dy = y - (h - r0)
        else:
            return 0
        if dy >= r0:
            return r0
        return r0 - (r0 * r0 - dy * dy) ** 0.5

    def _content_rect_at(self, h, pad, line_h, margin_x):
        """G4: usable content x-range [x0, x1] for a content block of height h with
        rows spanning pad..(h-pad), clamped to the stadium boundary at the extreme
        row edges plus margin_x. Every drawn element (text, badge, grip) must live
        inside this rect — no content may cross the rounded corners."""
        r0 = self.base_r
        if r0 <= 0:
            return margin_x, self.width - margin_x
        # Use the EXTREME row edges (top of first row, bottom of last row): the
        # corners narrow fastest at the edges, so the edge inset >= the center inset.
        inset = max(self._stadium_inset_at(h, pad, r0), self._stadium_inset_at(h, h - pad, r0))
        return int(inset) + margin_x, self.width - int(inset) - margin_x

    @staticmethod
    def _cfg_file_mtime():
        try:
            return os.stat(core.CONFIG_PATH).st_mtime
        except Exception:
            return 0.0

    def _apply_ui_config(self, ov_cfg):
        """E1: hot-apply bubble appearance changes (admin panel 气泡外观).
        Font/line scales only affect NEW layout math (wrapping/height) — the existing
        layout math stays consistent because all sizes derive from these two factors.
        Base size changes resize the capsule; opacity changes re-apply window alpha."""
        ui = ov_cfg.get("ui", {})
        fs = max(0.6, min(2.0, float(ui.get("font_scale", 1.0))))
        ls = max(0.7, min(2.0, float(ui.get("line_scale", 1.0))))
        op = max(40, min(100, int(ui.get("opacity", 85))))
        changed = False
        if fs != self.ui_font_scale:
            self.ui_font_scale = fs
            changed = True
        if ls != self.ui_line_scale:
            self.ui_line_scale = ls
            changed = True
        if op != self.ui_opacity:
            self.ui_opacity = op
            self._apply_window_alpha()
        # F6: user-tunable done-state display durations (解决长文本“消失变慢”的观感)
        self.done_short_ms = max(500, int(ui.get("done_short_ms", 2000)))
        self.done_long_ms = max(2000, int(ui.get("done_long_ms", 5000)))
        new_w = ov_cfg.get("base_width", 0)
        new_h = ov_cfg.get("base_height", 0)
        if new_w and int(new_w) != self.base_w and not self.custom_width:
            self.base_w = int(new_w)
            self.width = int(round(self.base_w * self.scale))
            changed = True
        if new_h and int(new_h) != self.base_h:
            self.base_h = int(new_h)
            self.height = int(round(self.base_h * self.scale))
            changed = True
        if changed:
            # F4: height cap follows the screen, not the base capsule.
            try:
                _sx, _sy, _sw, _sh = core.get_screen_workarea()
                self.max_content_h = max(self.height * 3, int(_sh * 0.62))
            except Exception:
                self.max_content_h = max(self.height * 3, self.height * 6)
            # PERF: a font/line/opacity/size change alters the inventory's shape
            # contract (polygon point counts, text wrapping, fonts) — drop the reused
            # items so the next draw rebuilds them from scratch.
            self._invalidate_canvas_items()
            if self.state in ("idle", "recording"):
                self._set_base_size()
                self._draw_pill()

    def _apply_window_alpha(self):
        """E7: make the pill semi-transparent (LWA_ALPHA + existing colorkey).
        COLOR_BG stays the colorkey so the rounded-edge masking from -transparentcolor
        keeps working; LWA_ALPHA adds global opacity on top."""
        if not _HAS_WIN32 or user32 is None:
            return
        try:
            hwnd = self.root.winfo_id()
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not (style & WS_EX_LAYERED):
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
            # G6.3: COLORREF is 0x00BBGGRR — the naive int("121214",16) is RGB-swapped
            # and would set a non-matching key (silently disabling the colorkey).
            r, g, b = int(COLOR_BG[1:3], 16), int(COLOR_BG[3:5], 16), int(COLOR_BG[5:7], 16)
            cr_key = 0x00 | (b << 16) | (g << 8) | r
            user32.SetLayeredWindowAttributes(
                hwnd, cr_key, int(self.ui_opacity * 255 / 100), LWA_COLORKEY | LWA_ALPHA
            )
        except Exception:
            pass

    def _engine_label(self):
        """Small badge text for the engine indicator (A7 + F2: 具体模型/硬件名).
        Engine strings: 'groq (whisper-large-v3-turbo)' / 'sensevoice (cuda)'.
        User wants the model name, not a generic '本地引擎'."""
        eng = (self.engine or "").strip().lower()
        if not eng:
            return ""
        if eng.startswith("groq"):
            model = ""
            if "(" in eng:
                model = eng.split("(", 1)[1].rstrip(")")
            # G3: bare 'groq' (recording-state default) must fall back to the CONFIGURED
            # model, not a hardcoded Turbo — that was the "选了 V3 徽标还是 Turbo" bug.
            if not model:
                try:
                    model = self.cfg.get("engines", {}).get("groq", {}).get("model", "")
                except Exception:
                    model = ""
            short = "L3 Turbo" if "turbo" in model else ("L3" if "large-v3" in model else (model or "L3 Turbo"))
            return f"⚡ Groq · {short}"
        if eng.startswith("sensevoice"):
            hw = "5090" if "cuda" in eng else "CPU"
            return f"🤗 SenseVoice · {hw}"
        # G1: a typed spec used as the recording default, e.g. 'groq-l3' / 'sensevoice-cpu'.
        if eng in ("groq-turbo", "groq-l3", "sensevoice-cuda", "sensevoice-cpu"):
            if eng.startswith("groq"):
                return "⚡ Groq · L3 Turbo" if "turbo" in eng else "⚡ Groq · L3"
            return f"🤗 SenseVoice · {'5090' if 'cuda' in eng else 'CPU'}"
        return f"⚙ {eng}"

    # ── Adaptive Content Height helpers (complaint #1/#5) ────────────
    def _text_font(self):
        # Slimmer default type size (user feedback: font too large on 4K @225%).
        # Font sizing: tk re-scales the font size itself by the system DPI on
        # Windows (225% -> x2.25), so passing s(9)=20 produced ~45px glyphs while
        # layout coords (also s()-scaled) stayed at 29px lines -> overlap.
        # Passing the UNSCALED logical unit (-9) yields glyph == s(9) px, matching
        # the s()-scaled layout math at any DPI.
        # E1: multiply by the user's font_scale (admin panel 气泡外观).
        return (FONT_FAMILY, -max(6, int(self.s(9) * self.ui_font_scale)))

    def _engine_font(self):
        return (FONT_FAMILY, -max(5, int(self.s(8) * self.ui_font_scale)))

    def _char_widths(self):
        """Measure one CJK and one Latin character advance (px). Falls back to
        font-size heuristics if the canvas cannot measure (headless).

        PERF: memoized per font tuple. The measurement depends ONLY on the font, but
        every _wrap_to_lines call re-measured it, and the layout drivers call
        _wrap_to_lines 12x per layout (4 font scales x 3 fixed-point iterations) — so a
        single streaming-preview update created/destroyed 24 probe items and, worse,
        called root.update_idletasks() 24x (each drains the whole idle queue
        synchronously). Verified on Tk 8.6.15 that canvas.bbox() returns the SAME
        values with and without update_idletasks, so those calls are gone; line breaks
        are therefore byte-identical. Do NOT swap this for
        tkinter.font.Font.measure(): it reports the advance width (20px) while bbox
        reports 22px for the same CJK glyph at s(9), which WOULD change the wraps."""
        font = self._text_font()
        cached = self._char_width_cache.get(font)
        if cached is not None:
            return cached
        wc = wl = None
        try:
            t1 = self.canvas.create_text(10, 10, text="\u6d4b", font=font, anchor="w")
            b1 = self.canvas.bbox(t1)
            self.canvas.delete(t1)
            t2 = self.canvas.create_text(10, 10, text="a", font=font, anchor="w")
            b2 = self.canvas.bbox(t2)
            self.canvas.delete(t2)
            wc = (b1[2] - b1[0]) if b1 else None
            wl = (b2[2] - b2[0]) if b2 else None
            # NB: measured with the SAME font that renders the text; in the DPI-aware
            # overlay context (216 DPI) bbox() is already in device px, consistent
            # with the s()-scaled layout coords. Do NOT multiply by self.scale here.
        except Exception:
            pass
        if wc and wl:
            self._char_width_cache[font] = (wc, wl)
        return wc, wl

    def _wrap_to_lines(self, text, avail_w, max_lines):
        """Greedy word-wrap of `text` into exactly <= max_lines lines using measured
        char widths. Every break is at a word boundary; the final line, when text is
        truncated, carries an ellipsis. Never emits a trailing orphan line past the cap."""
        wc, wl = self._char_widths()
        wc = wc or (self.s(11) * 1.8)
        wl = wl or (self.s(11) * 0.95)
        cap = max(self.s(24), int(avail_w) - self.s(8))
        lines = []
        cur = ""
        cw = 0.0
        for ch in text:
            chw = wc if ord(ch) >= 0x4E00 else wl
            if cw + chw > cap and cur:
                if len(lines) >= max_lines - 1:
                    # Last allowed line: hard-truncate at a word boundary + ellipsis.
                    seg = cur
                    sp = seg.rfind(" ")
                    if sp > int(len(seg) * 0.5):
                        seg = seg[:sp]
                    lines.append(seg.rstrip() + "\u2026")
                    cur = ""
                    cw = 0.0
                    break
                lines.append(cur)
                cur = ""
                cw = 0.0
            cur += ch
            cw += chw
        if cur.strip():
            lines.append(cur.rstrip())
        if not lines:
            lines = [""]
        return lines

    def _render_text_block(self, color, x=None):
        """Draw the precomputed wrapped content lines, centred vertically in the pill
        (or at a given y when `x` is passed — used by the recording multi-line layout).
        PERF: renders into the reusable text-item pool instead of creating items."""
        lines = self._render_lines or [""]
        lh = self._render_line_h or max(self.s(9), int(self.s(13) * self.ui_line_scale))
        scale = getattr(self, "_render_font_scale", 1.0)
        # -int(s(9)*scale): glyph exactly s(9)*scale device px, matching s()-scaled layout.
        font = (FONT_FAMILY, -max(6, int(self.s(9) * scale * self.ui_font_scale)))
        self._ensure_pool(len(lines))
        if x is None:
            block_h = len(lines) * lh
            top = max(self.s(6), (self.current_h - block_h) // 2)
            # G4: text must stay inside the capsule boundary at every row.
            x0, x1 = self._content_rect_at(self.current_h, self.s(6), lh, self.s(6))
            tx0 = max(x0, self.s(48))  # clear the left state icon
            for i, ln in enumerate(lines):
                role = "ln%d" % i
                self._show(role)
                self._set_coords(role, tx0, top + i * lh)
                self._set_opts(role, text=ln or "", fill=color, font=font, anchor="nw")
        else:
            for i, ln in enumerate(lines):
                role = "ln%d" % i
                self._show(role)
                self._set_coords(role, x, self._render_top_y + i * lh)
                self._set_opts(role, text=ln or "", fill=color, font=font, anchor="nw")
        self._hide_pool(len(lines))

    def _resize_to(self, h):
        """Grow/shrink the pill: resize the window to the new height, bottom-anchored
        (the bottom edge stays fixed, the top edge moves). The transparent-color keying
        survives the resize because the E7 colorkey uses the correct BGR COLORREF
        (verified: pill corners stay transparent across resizes — desktop-reference)."""
        new_h = int(h)
        if new_h == self.current_h:
            return
        bottom = self._win_y + self.current_h
        self.current_h = new_h
        new_win_y = bottom - new_h
        self._win_y = new_win_y
        try:
            self.root.geometry(f"{self.width}x{new_h}+{self._win_x}+{new_win_y}")
        except Exception:
            pass

    def _set_base_size(self):
        """Return the pill to its default capsule size (transient states)."""
        self._resize_to(self.height)

    def _fit_content(self):
        """Wrap the current done/error text, recompute the pill height to fit it
        (clamped to [base, max_content]) — the font shrinks in steps BEFORE the cap is
        reached (no scrolling, per user requirement) — then resize (bottom-anchored) and draw.
        G4: available width is derived from the capsule boundary at the extreme rows
        (tall pills inset their top/bottom rows to stay inside the end caps); height
        and width are solved together in a small fixed-point loop."""
        t = (self.text or "").strip()
        pad = self.s(10)
        # E1: user line_scale multiplies the design line height (layout stays consistent
        # because glyph and line spacing both derive from the s() units).
        base_lh = max(self.s(9), int(self.s(13) * self.ui_line_scale))
        scale, line_h, lines = 1.0, base_lh, [""]
        h_est = self.height
        for scale in (1.0, 0.9, 0.8, 0.7):
            line_h = max(self.s(8), int(base_lh * scale))
            # G4 fixed-point: height -> capsule inset -> width -> line count -> height.
            for _ in range(3):
                x0, x1 = self._content_rect_at(h_est, pad, line_h, self.s(34))
                avail_w = max(self.s(40), x1 - x0)
                max_lines = max(2, int((max(self.s(16), self.max_content_h - 2 * pad)) // line_h))
                lines = self._wrap_to_lines(t, avail_w, max_lines)
                h_est = max(self.height, min(len(lines) * line_h + 2 * pad, self.max_content_h))
            if len(lines) * line_h + 2 * pad <= self.max_content_h:
                break
        self._render_lines = lines
        self._render_line_h = line_h
        self._render_font_scale = scale
        content_h = len(lines) * line_h + 2 * pad
        new_h = max(self.height, min(content_h, self.max_content_h))
        if abs(new_h - self.current_h) > 1:
            self._resize_to(new_h)  # updates self.current_h internally
        self._draw_pill()

    def _init_window(self):
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=COLOR_BG)

        # Usable screen work area excluding Windows taskbar
        sx, sy, sw, sh = core.get_screen_workarea()
        # Pill-sized window, bottom-anchored. `custom_x/custom_y` store the PILL's
        # bottom-left screen position; the window's top-left is derived from it (window
        # height == pill height). Growth extends the window UPWARD via _resize_to.
        pill_x = sx + (sw - self.width) // 2
        pill_bottom = sy + sh - self.height - self.s(36)
        if self.custom_x is not None and self.custom_y is not None:
            # Restore user's custom dragged pill position (clamped to screen).
            pill_x = max(sx, min(sx + sw - self.width, int(self.custom_x)))
            pill_bottom = max(sy + self.height, min(sy + sh - self.height, int(self.custom_y)))
        x = pill_x
        y = pill_bottom - self.height  # window top-left; window height == pill height
        self._win_x = x
        self._win_y = y
        # G6.4: geometry (size) is set BEFORE -transparentcolor, and the E7 colorkey
        # uses the correct BGR COLORREF, so the transparent keying works AND survives
        # the window resizes that grow/shrink the pill (verified via desktop-reference).
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")
        # Transparent color keying for rounded corners on Windows.
        try:
            self.root.wm_attributes("-transparentcolor", COLOR_BG)
        except Exception:
            pass
        # E7: global window alpha (semi-transparent pill, content shows through).
        self._apply_window_alpha()
        self._apply_no_activate()

    def _apply_no_activate(self):
        if not _HAS_WIN32 or user32 is None:
            return
        try:
            hwnd = int(self.root.wm_frame(), 16)
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
            )
        except Exception:
            pass

    def _on_drag_start(self, event):
        if self._in_grip_zone(event):
            # Bottom-right grip -> resize width, not move.
            self._is_resizing = True
            self._resize_start_x_root = getattr(event, "x_root", 0)
            self._resize_start_w = self.width
            return
        self._drag_start_x = getattr(event, "x_root", 0)
        self._drag_start_y = getattr(event, "y_root", 0)
        try:
            self._drag_offset_x = self._drag_start_x - self.root.winfo_x()
            self._drag_offset_y = self._drag_start_y - self.root.winfo_y()
        except Exception:
            self._drag_offset_x = 0
            self._drag_offset_y = 0
        self._is_dragging = True

    def _on_drag_motion(self, event):
        if self._is_resizing:
            dx = getattr(event, "x_root", 0) - self._resize_start_x_root
            self._apply_width(self._resize_start_w + dx)
            return
        if not self._is_dragging:
            return
        new_x = getattr(event, "x_root", 0) - self._drag_offset_x
        new_y = getattr(event, "y_root", 0) - self._drag_offset_y
        try:
            self.root.geometry(f"+{new_x}+{new_y}")
        except Exception:
            pass
        self._win_x = new_x
        self._win_y = new_y

    def _on_drag_release(self, event):
        if self._is_resizing:
            self._is_resizing = False
            try:
                c = core.ConfigManager.load()
                c.setdefault("overlay", {})["custom_width"] = int(round(self.width / self.scale))
                core.ConfigManager.save(c)
            except Exception:
                pass
            return
        self._is_dragging = False
        try:
            win_x = self.root.winfo_x()
            win_y = self.root.winfo_y()
        except Exception:
            win_x, win_y = 0, 0
        # Persist the PILL's bottom-left (window bottom == pill bottom) so the
        # _init_window restore logic (which expects pill coordinates) stays consistent.
        cur_x = win_x
        cur_y = win_y + self.current_h
        self.custom_x = cur_x
        self.custom_y = cur_y
        try:
            c = core.ConfigManager.load()
            c.setdefault("overlay", {})["custom_x"] = cur_x
            c["overlay"]["custom_y"] = cur_y
            core.ConfigManager.save(c)
        except Exception:
            pass

    def _on_double_click(self, event):
        # Double click resets pill position AND width to defaults.
        # G6.3: position = the PILL's bottom-left (window bottom == pill bottom).
        default_w = self.cfg.get("overlay", {}).get("base_width", 540)
        self.base_w = int(default_w)
        self._apply_width(int(round(self.base_w * self.scale)))
        sx, sy, sw, sh = core.get_screen_workarea()
        reset_pill_x = sx + (sw - self.width) // 2
        reset_pill_bottom = sy + sh - self.height - self.s(36)
        reset_x = reset_pill_x
        reset_y = reset_pill_bottom - self.height  # window top-left (window height == pill height)
        try:
            self.root.geometry(f"+{reset_x}+{reset_y}")
        except Exception:
            pass
        self._win_x = reset_x
        self._win_y = reset_y
        self.custom_x = None
        self.custom_y = None
        self.custom_width = None
        try:
            c = core.ConfigManager.load()
            c.setdefault("overlay", {})["custom_x"] = None
            c["overlay"]["custom_y"] = None
            c["overlay"]["custom_width"] = None
            core.ConfigManager.save(c)
        except Exception:
            pass

    def _on_cursor_motion(self, event):
        # PERF: <Motion> fires continuously while the pointer crosses the always-on
        # pill; only touch Tk when the cursor actually has to change.
        try:
            name = "sizing" if self._in_grip_zone(event) else "fleur"
            if name != self._cursor_name:
                self._cursor_name = name
                self.canvas.config(cursor=name)
        except Exception:
            pass

    def _bind_drag_events(self):
        for target in (self.root,):
            target.bind("<Button-1>", self._on_drag_start)
            target.bind("<B1-Motion>", self._on_drag_motion)
            target.bind("<ButtonRelease-1>", self._on_drag_release)
            target.bind("<Double-Button-1>", self._on_double_click)

    def _create_widgets(self):
        # Pill-sized canvas; the window resizes with the pill (bottom-anchored) and the
        # canvas follows via pack(fill=both). The transparent-color keying is set in
        # _init_window with the correct BGR colorkey (E7) so it survives resizes.
        self.canvas = tk.Canvas(
            self.root,
            width=self.width,
            height=self.height,
            bg=COLOR_BG,
            highlightthickness=0,
            bd=0,
            cursor="fleur",  # Move cursor to show it is draggable
        )
        self.canvas.pack(fill="both", expand=True)

        # Also forward drag bindings from canvas
        self.canvas.bind("<Button-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Motion>", self._on_cursor_motion)

    @staticmethod
    def _stadium_points(w, h, r0, inset=0, per_arc=48):
        """G4.1: exact stadium outline as a sampled point list (see
        _stadium_points_cached, which memoizes the computation). PERF: returns a fresh
        list so callers keep the original contract of owning a mutable list, but the
        expensive sampling itself runs once per distinct pill size."""
        return list(_stadium_points_cached(w, h, r0, inset, per_arc))

    # ── Reusable canvas item inventory (PERF) ─────────────────────────
    # The pill used to call canvas.delete("all") and rebuild ~19 items on every 40 ms
    # tick — including two 394-coordinate polygons — i.e. ~475 Tcl item creations per
    # second, a freshly formatted Tcl command string per item and a full
    # re-rasterization of both stadium layers every frame. The inventory below is
    # created ONCE and then updated in place with coords()/itemconfigure(), so a
    # steady-state frame only touches the handful of items whose data really changed.
    _BAR_COUNT = 9
    _GRIP_COUNT = 3

    def _create_canvas_items(self):
        """Create the fixed item inventory once, in the original paint order so the
        stacking (z-order) of every layer is unchanged."""
        c = self.canvas
        self._items = {}
        self._item_cache = {}
        self._pool_n = 0
        # Two stadium layers: border (outer) then fill (inner). The exact geometry is
        # still produced by _stadium_points, so the rendered shape is identical.
        self._items["border"] = c.create_polygon(
            0, 0, 1, 0, 1, 1, fill=COLOR_BORDER, outline="", smooth=False)
        self._items["fill"] = c.create_polygon(
            0, 0, 1, 0, 1, 1, fill=COLOR_PILL, outline="", smooth=False)
        # One oval + one glyph slot serve every state icon (recording dot, transcribing
        # spinner, done circle + check, cancelled/error dot).
        self._items["dot"] = c.create_oval(0, 0, 0, 0, fill=COLOR_REC, outline="",
                                           state="hidden")
        self._items["icon"] = c.create_text(0, 0, text="", fill="#ffffff",
                                            state="hidden")
        # Two single-line text slots: the timer/latency and the free-form label.
        self._items["timer"] = c.create_text(0, 0, text="", fill=COLOR_TEXT,
                                             anchor="w", state="hidden")
        self._items["label"] = c.create_text(0, 0, text="", fill=COLOR_TEXT,
                                             anchor="w", state="hidden")
        for i in range(self._BAR_COUNT):
            self._items["bar%d" % i] = c.create_rectangle(
                0, 0, 0, 0, fill=COLOR_REC, outline="", state="hidden")
        self._items["divider"] = c.create_line(0, 0, 0, 0, fill="#3f3f46", width=1,
                                               state="hidden")
        # Engine badge and resize grip. The reusable wrapped-text pool is inserted on
        # demand BELOW the badge (see _ensure_pool) so content lines stay underneath
        # both, exactly like the original create order.
        self._items["badge"] = c.create_text(0, 0, text="", fill=COLOR_MUTED,
                                             anchor="se", state="hidden")
        for i in range(self._GRIP_COUNT):
            self._items["grip%d" % i] = c.create_line(
                0, 0, 0, 0, fill=COLOR_MUTED, width=1, state="hidden")

    def _invalidate_canvas_items(self):
        """Force a full rebuild on the next draw. Needed only when the item inventory
        itself must change (font/scale/size changes), not for ordinary redraws."""
        self._items = {}
        self._item_cache = {}
        self._pool_n = 0
        try:
            self.canvas.delete("all")
        except Exception:
            pass

    def _ensure_pool(self, n):
        """Grow the reusable wrapped-text-line pool to at least n items."""
        while self._pool_n < n:
            role = "ln%d" % self._pool_n
            item = self.canvas.create_text(0, 0, text="", fill=COLOR_TEXT,
                                           anchor="nw", state="hidden")
            # Keep content lines below the badge/grip, matching the original order.
            try:
                self.canvas.tag_lower(item, self._items["badge"])
            except Exception:
                pass
            self._items[role] = item
            self._pool_n += 1

    def _hide_pool(self, keep):
        """Hide every pooled line item at index >= keep (no-op when already hidden)."""
        for i in range(keep, self._pool_n):
            self._show("ln%d" % i, False)

    def _set_coords(self, role, *coords):
        """canvas.coords() only when that item's geometry actually changed."""
        key = (role, "xy")
        if self._item_cache.get(key) == coords:
            return
        self._item_cache[key] = coords
        self.canvas.coords(self._items[role], *coords)

    def _set_opts(self, role, **opts):
        """canvas.itemconfigure() only for the options that actually changed."""
        key = (role, "cfg")
        cur = self._item_cache.get(key)
        if cur is not None and all(cur.get(k) == v for k, v in opts.items()):
            return
        cur = dict(cur) if cur else {}
        cur.update(opts)
        self._item_cache[key] = cur
        self.canvas.itemconfigure(self._items[role], **opts)

    def _show(self, role, visible=True):
        """Reveal/hide a reusable item instead of creating/destroying it. Roles shown
        here are recorded for _hide_unused so nothing needs a hide-then-show cycle."""
        if visible:
            self._used.add(role)
        self._set_opts(role, state="normal" if visible else "hidden")

    def _hide_unused(self):
        """Hide the fixed items this frame did not use (no-op once already hidden).
        Runs at the END of a draw instead of a blanket hide at the start, so an item
        that is visible in a given state is configured exactly once per frame."""
        for role in ("dot", "icon", "timer", "label", "divider", "badge"):
            if role not in self._used:
                self._show(role, False)
        for i in range(self._BAR_COUNT):
            role = "bar%d" % i
            if role not in self._used:
                self._show(role, False)
        for i in range(self._GRIP_COUNT):
            role = "grip%d" % i
            if role not in self._used:
                self._show(role, False)

    def _draw_pill(self):
        if not self._items:
            self._create_canvas_items()
        w, h = self.width, self.current_h
        b = max(1, self.s(1.4))

        # Draw the pill EXACTLY (F3 + G4): the old smooth=True polygon is a B-spline that
        # does not pass through its control points — on large pills the long edges sag
        # inward and the corner curves overshoot, which read as a visible "inner
        # rectangle" edge (user: 变大后出现直角边). G4: the shape is a STADIUM — a fixed
        # corner radius r0 (= the base capsule radius) with straight sides, so a tall
        # pill keeps its full content width instead of squeezing to a balloon capsule.
        # G4.1: draw each layer as ONE polygon whose outline is sampled (48 pts per
        # quarter arc) — the previous rectangle+oval piece construction left visible
        # aliased seams where the corner arcs met the straight bands (user screenshot:
        # 边上的直角边). A single continuous outline has no seams at any size.
        r0 = min(self.base_r, w // 2)
        self._show("border")
        self._set_coords("border", *self._stadium_points(w, h, r0))
        if r0 > b:
            self._show("fill")
            self._set_coords("fill", *self._stadium_points(w - 2 * b, h - 2 * b, r0 - b, b))
        else:
            self._show("fill", False)

        # PERF: nothing is hidden up-front. A blanket "hide everything, then let the
        # branch re-show what it needs" would cost every used item TWO itemconfigure
        # calls per frame. Each branch simply shows what it uses (recorded in _used)
        # and _hide_unused() hides the leftovers once, at the end of the draw.
        self._used = set()

        cy = h // 2

        # Draw state icon & content
        if self.state == "recording":
            self._anim_step = (self._anim_step + 1) % 20
            multi = bool(self._preview_lines and len(self._preview_lines) >= 2)
            # G4: header band (red dot + timer + wave bars) pinned to the BOTTOM of the
            # pill in multi-line mode — bottom-anchored growth means the pill extends
            # UPWARD from a fixed header, so the header sits in the wide base-capsule
            # band at the bottom and the streaming text fills the region ABOVE it.
            # (Drawing the header at the top of a tall pill put it in the narrow end-cap
            # zone — the overflow the user saw.)
            cy = (self.current_h - self.height // 2) if multi else (self.height // 2)

            # 1. Pulsating red recording badge
            pulse_radius = self.s(6.5) + int(math.sin(self._anim_step * math.pi / 10.0) * self.s(2.0))
            cx = self.s(28)
            self._show("dot")
            self._set_coords("dot", cx - pulse_radius, cy - pulse_radius,
                             cx + pulse_radius, cy + pulse_radius)
            self._set_opts("dot", fill=COLOR_REC, outline="")

            # 2. Live timer
            elapsed = time.monotonic() - self.start_time if self.start_time else 0.0
            time_str = f"{elapsed:.1f}s"
            self._show("timer")
            self._set_coords("timer", self.s(46), cy)
            self._set_opts("timer", text=time_str, fill=COLOR_TEXT,
                           font=(FONT_FAMILY, -max(7, int(self.s(10) * self.ui_font_scale)), "bold"))

            # 3. Dynamic audio wave bars (9 bars with smooth equalizer animation)
            bar_start_x = self.s(96)
            bar_w = max(2, self.s(3.5))
            bar_gap = max(2, self.s(3.0))
            max_h = self.s(24)
            # E2: boosted gain — neckband/clip-on mics deliver low RMS, the old x4.2
            # left the bars nearly frozen; x9.0 keeps them visibly alive at the same
            # level values (clamped at 1.0, so desktop mics just saturate a bit earlier).
            lvl = min(1.0, max(0.05, self.level * 9.0))

            for i in range(self._BAR_COUNT):
                phase = (i * 0.55) + (self._anim_step * 0.28)
                factor = 0.25 + 0.75 * (math.sin(phase) ** 2)
                bh = max(self.s(4), int(max_h * lvl * factor))
                bx = bar_start_x + i * (bar_w + bar_gap)
                by1 = cy - bh // 2
                by2 = cy + bh // 2
                bar_color = COLOR_REC if lvl > 0.8 else "#fb7185" if lvl > 0.3 else "#a1a1aa"
                role = "bar%d" % i
                self._show(role)
                self._set_coords(role, bx, by1, bx + bar_w, by2)
                self._set_opts(role, fill=bar_color, outline="")

            div_x = bar_start_x + self._BAR_COUNT * (bar_w + bar_gap) + self.s(8)
            self._show("divider")
            self._set_coords("divider", div_x, cy - self.s(12), div_x, cy + self.s(12))
            self._set_opts("divider", fill="#3f3f46", width=self.s(1))

            if multi:
                # Sonic-style multi-line streaming preview: full content, wrapped,
                # growing upward. Pulsating typing cursor on the live (last) line.
                lines = list(self._preview_lines)
                cursor = " ▎" if (self._anim_step // 4) % 2 == 0 else "  "
                lines[-1] = lines[-1] + cursor
                scale = self._preview_font_scale
                pfont = (FONT_FAMILY, -max(6, int(self.s(9) * scale * self.ui_font_scale)), "bold")
                # G4: text occupies the region ABOVE the bottom header band (Sonic-style
                # upward growth), vertically centred in that region, clamped to the
                # capsule boundary at the region edges.
                lh = self._preview_line_h
                region_top = self.s(8)
                region_h = max(lh, self.current_h - self.height - self.s(8) - self.s(8))
                top_y = region_top + max(0, (region_h - len(lines) * lh) // 2)
                x0, x1 = self._content_rect_at(self.current_h, region_top, lh, self.s(34))
                tx0 = max(x0, self.s(48))
                self._ensure_pool(len(lines))
                for i, ln in enumerate(lines):
                    role = "ln%d" % i
                    self._show(role)
                    self._set_coords(role, tx0, top_y + i * lh)
                    self._set_opts(role, text=ln or "", fill="#ffffff", font=pfont, anchor="nw")
                self._hide_pool(len(lines))
            else:
                # Divider line + single-line hint / preview tail (base capsule layout)
                hint_x = div_x + self.s(12)
                avail_w = w - hint_x - self.s(40)
                avail_chars = max(16, int(avail_w / self.s(11)))

                if self.preview_text:
                    disp_txt = self.preview_text.strip()
                    if len(disp_txt) > avail_chars:
                        disp_txt = "…" + disp_txt[-(avail_chars - 1):]
                    # Pulsating typing cursor for live streaming feel
                    cursor = " ▎" if (self._anim_step // 4) % 2 == 0 else "  "
                    disp_txt = disp_txt + cursor
                    txt_color = "#ffffff"
                    font_weight = "bold"
                else:
                    disp_txt = self.hint or "口述中 (松手完成 · 鼠标可拖动)"
                    txt_color = COLOR_MUTED
                    font_weight = "normal"

                self._show("label")
                self._set_coords("label", hint_x, cy)
                self._set_opts("label", text=disp_txt, fill=txt_color,
                               font=(FONT_FAMILY, -max(6, int(self.s(9) * self.ui_font_scale)), font_weight))
                self._hide_pool(0)
            self._draw_engine_badge()

        elif self.state == "transcribing":
            # Amber dot + spinner pulse
            cx = self.s(28)
            self._anim_step = (self._anim_step + 1) % 16
            pulse = int(math.sin(self._anim_step * math.pi / 8.0) * self.s(2.2))
            r_dot = self.s(6) + pulse
            self._show("dot")
            self._set_coords("dot", cx - r_dot, cy - r_dot, cx + r_dot, cy + r_dot)
            self._set_opts("dot", fill=COLOR_TRANS, outline="")
            self._show("label")
            self._set_coords("label", self.s(48), cy)
            self._set_opts("label", text="⏳ 正在极速转写中...", fill=COLOR_TEXT,
                           font=(FONT_FAMILY, -max(7, int(self.s(10) * self.ui_font_scale)), "bold"))
            self._hide_pool(0)
            self._draw_engine_badge()

        elif self.state == "done":
            # Emerald checkmark + preview text
            cx = self.s(28)
            r_circle = self.s(8)
            self._show("dot")
            self._set_coords("dot", cx - r_circle, cy - r_circle, cx + r_circle, cy + r_circle)
            self._set_opts("dot", fill=COLOR_DONE, outline="")
            self._show("icon")
            self._set_coords("icon", cx, cy)
            self._set_opts("icon", text="✓", fill="#ffffff",
                           font=(FONT_FAMILY, -max(6, int(self.s(9) * self.ui_font_scale)), "bold"))
            # G6.4: transcription latency timer, mirroring the recording timer (user:
            # 完结气泡里加个计时器，跟录音计时器页面很像). Shown right of the checkmark.
            if self.latency is not None:
                lat_str = f"{self.latency:.1f}s"
                self._show("timer")
                self._set_coords("timer", self.s(46), cy)
                self._set_opts("timer", text=lat_str, fill=COLOR_MUTED,
                               font=(FONT_FAMILY, -max(7, int(self.s(10) * self.ui_font_scale)), "bold"))
            # Adaptive multi-line result (complaint #1/#5): draw the precomputed
            # wrapped lines, centred vertically in the (possibly grown) pill.
            self._render_text_block(COLOR_TEXT)
            # E5: keep the engine badge in the done state too (user: 输出后右下角徽标消失).
            self._draw_engine_badge()

        elif self.state == "cancelled":
            cx = self.s(28)
            r_c = self.s(6)
            self._show("dot")
            self._set_coords("dot", cx - r_c, cy - r_c, cx + r_c, cy + r_c)
            self._set_opts("dot", fill=COLOR_CANCEL, outline="")
            self._show("label")
            self._set_coords("label", self.s(48), cy)
            self._set_opts("label", text="已撤销录音", fill=COLOR_MUTED,
                           font=(FONT_FAMILY, -max(7, int(self.s(11) * self.ui_font_scale))))
            self._hide_pool(0)

        elif self.state == "error":
            cx = self.s(28)
            r_e = self.s(6)
            self._show("dot")
            self._set_coords("dot", cx - r_e, cy - r_e, cx + r_e, cy + r_e)
            self._set_opts("dot", fill=COLOR_REC, outline="")
            # Adaptive multi-line error message (complaint #1/#5).
            if not (self._render_lines and any(self._render_lines)):
                err_text = (self.text or "").strip() or "未识别到有效语音"
                self._show("label")
                self._set_coords("label", self.s(48), cy)
                self._set_opts("label", text=err_text, fill="#fca5a5", font=self._text_font())
                self._hide_pool(0)
            else:
                self._render_text_block("#fca5a5")
        else:
            self._hide_pool(0)

        # Resize grip: three subtle diagonal ticks at the bottom-right corner (A2).
        self._draw_resize_grip()
        # Hide whatever this state does not use (single, cached no-op in steady state).
        self._hide_unused()
        # The pill fills the (pill-sized) canvas: drawn in 0..width, 0..current_h.

    def _draw_engine_badge(self):
        """Small engine indicator inside the pill, bottom-right (A7): which engine
        is serving the current recording / transcription."""
        label = self._engine_label()
        if not label:
            self._show("badge", False)
            return
        # G4: keep the badge inside the stadium boundary at ITS OWN y — the bottom
        # corner zone narrows, so clamp x to the boundary at the badge row.
        h = self.current_h
        badge_y = h - self.s(6)
        inset = self._stadium_inset_at(h, badge_y, self.base_r)
        x1 = self.width - int(inset) - self.s(6)
        self._show("badge")
        self._set_coords("badge", min(self.width - self.s(14), x1), badge_y)
        self._set_opts("badge", text=label, fill=COLOR_MUTED, font=self._engine_font(),
                       anchor="se")

    def _draw_resize_grip(self):
        """Bottom-right drag handle for width resizing (A2).
        G4: anchored inside the bottom-right CORNER (45° position inside the corner
        circle of radius r0) so the ticks never cross the stadium boundary at any
        pill height."""
        h = self.current_h
        r0 = min(self.base_r, self.width // 2)
        if r0 <= 0:
            for i in range(self._GRIP_COUNT):
                self._show("grip%d" % i, False)
            return
        cx = self.width - r0
        cy = h - r0
        g = self.s(6)
        base_x = cx - r0 * 0.38
        base_y = cy - r0 * 0.62
        for i in range(self._GRIP_COUNT):
            off = self.s(4) + i * self.s(4)
            x0 = base_x - off
            y0 = base_y
            role = "grip%d" % i
            self._show(role)
            self._set_coords(role, x0, y0, x0 + g, y0 - g)
            self._set_opts(role, fill=COLOR_MUTED, width=1)

    def _in_grip_zone(self, event):
        """True if the pointer is inside the bottom-right resize grip.
        G4: the grip sits in the bottom-right corner (45°); in canvas space (pill-sized
        window) its y is at the pill bottom (current_h)."""
        try:
            r0 = min(self.base_r, self.width // 2)
            if r0 <= 0:
                return False
            h = self.current_h
            bx = self.width - r0 - r0 * 0.38
            by = h - r0 - r0 * 0.62
            return (abs(getattr(event, "x", -1) - bx) <= self.s(14)
                    and abs(getattr(event, "y", -1) - by) <= self.s(14))
        except Exception:
            return False

    def _apply_width(self, new_w):
        """Change the pill width: resize the window to the new width (bottom-left corner
        stays fixed), then re-lay out and redraw. The transparent-color keying survives
        the resize (correct BGR colorkey, see _init_window)."""
        new_w = max(self.s(320), min(new_w, self.width + self.s(800)))
        if abs(new_w - self.width) < 2:
            return
        self.width = int(new_w)
        self.base_w = int(round(self.width / self.scale))
        try:
            self.root.geometry(f"{self.width}x{self.current_h}+{self._win_x}+{self._win_y}")
        except Exception:
            pass
        if self.state in ("done", "error"):
            self._render_lines = None  # force re-wrap at the new width
            self._fit_content()
        elif self.state == "recording":
            self._preview_cache_key = None
            self._update_preview_layout()
        else:
            self._draw_pill()

    def show(self):
        if not self.visible:
            self.root.deiconify()
            self._apply_no_activate()
            self.visible = True

    def hide(self):
        if self._tick_job is not None:
            try:
                self.root.after_cancel(self._tick_job)
            except Exception:
                pass
            self._tick_job = None
        if self.visible:
            self.root.withdraw()
            self.visible = False
        # FIX (stale tall window): a multi-line done/error grows the window via
        # _resize_to; assigning current_h alone left that tall window in place, so the
        # next show() drew the pill at the TOP of it (visually jumped up by
        # tall-base px) and _on_drag_release persisted a wrong bottom-left Y. Actually
        # shrink the window back — _resize_to is bottom-anchored, so the pill's bottom
        # edge stays where the user put it. It also resets current_h internally.
        if self.current_h != self.height:
            self._set_base_size()
        # Reset runtime height so a later show() starts from the default capsule.
        self.current_h = self.height
        self.state = "idle"
        self._preview_lines = []
        self._preview_cache_key = None
        self.preview_text = ""

    # ── Public State Changers ─────────────────────────────────────
    def show_recording(self, hint="口述中 (松手完成 · 鼠标可拖动)", start_time=None, preview=""):
        if self._hide_timer:
            self.root.after_cancel(self._hide_timer)
            self._hide_timer = None

        st = 0.0
        try:
            st = float(start_time) if start_time is not None else 0.0
        except Exception:
            st = 0.0
        # BUG-8: daemon publishes a system-wide monotonic timestamp. Reject an
        # old wall-clock value (from a not-yet-restarted daemon) — it is orders
        # of magnitude larger and would make the timer jump backwards.
        if st > 0 and st <= time.monotonic() + 1.0:
            self.start_time = st
        elif self.state != "recording" or not self.start_time:
            self.start_time = time.monotonic()

        self.state = "recording"
        self.hint = hint
        if preview:
            self.preview_text = preview
        # Reset streaming preview layout; grows upward as preview lines accumulate.
        self._preview_lines = []
        self._preview_cache_key = None
        # Transient state -> always default capsule height (grows back from a prior done).
        self._set_base_size()
        self.show()
        self._schedule_tick()

    def update_level(self, level):
        """Update live audio level (0.0 to 1.0) without resetting timers"""
        if level > self.level:
            self.level = self.level * 0.4 + level * 0.6
        else:
            self.level = self.level * 0.7 + level * 0.3

    def update_preview(self, text):
        """Update real-time streaming preview text while recording"""
        self.preview_text = text or ""
        self._update_preview_layout()

    def _update_preview_layout(self):
        """Sonic-style streaming growth: wrap the live preview into multiple lines and
        grow the pill UPWARD (bottom-anchored) so the full spoken content stays visible.
        Single-line previews keep the base capsule; the font shrinks in steps before the
        height cap (no scrolling). Cheap no-op when the text hasn't changed."""
        if self.state != "recording":
            return
        t = (self.preview_text or "").strip()
        if self._preview_cache_key == t:
            return
        self._preview_cache_key = t

        if not t:
            self._preview_lines = []
            self._preview_line_h = 0
            self._preview_font_scale = 1.0
            if abs(self.current_h - self.height) > 1:
                self._resize_to(self.height)
            return

        header_h = self.height
        engine_h = self.s(14) if self._engine_label() else 0
        pad = self.s(8)
        # E1: user line_scale (consistent with _fit_content).
        base_lh = max(self.s(9), int(self.s(13) * self.ui_line_scale))
        scale, line_h, lines = 1.0, base_lh, []
        h_est = header_h
        for scale in (1.0, 0.9, 0.8, 0.7):
            line_h = max(self.s(8), int(base_lh * scale))
            # G4 fixed-point: capsule boundary insets the extreme rows of a tall pill.
            for _ in range(3):
                x0, x1 = self._content_rect_at(h_est, pad, line_h, self.s(34))
                avail_w = max(self.s(40), x1 - x0)
                max_lines = max(
                    1, int((self.max_content_h - header_h - engine_h - 2 * pad) // line_h))
                lines = self._wrap_to_lines(t, avail_w, max_lines)
                h_est = min(self.max_content_h,
                            header_h + len(lines) * line_h + engine_h + 2 * pad)
            if header_h + len(lines) * line_h + engine_h + 2 * pad <= self.max_content_h:
                break
        self._preview_lines = lines
        self._preview_line_h = line_h
        self._preview_font_scale = scale

        if len(lines) <= 1:
            # Single line: stay in the base capsule, render inline (legacy path).
            self._preview_lines = []
            if abs(self.current_h - self.height) > 1:
                self._resize_to(self.height)
            return

        # G4: total height = bottom header band + text region above it.
        new_h = header_h + len(lines) * line_h + engine_h + 2 * pad
        if abs(new_h - self.current_h) > 1:
            self._resize_to(new_h)  # bottom-anchored; updates self.current_h internally

    def show_transcribing(self):
        if self._hide_timer:
            self.root.after_cancel(self._hide_timer)
            self._hide_timer = None
        self.state = "transcribing"
        # Transient state -> default capsule height.
        self._set_base_size()
        self.show()
        self._schedule_tick()

    def show_done(self, text="", latency=None):
        self.state = "done"
        self.text = text
        # G6.4: transcription latency for the done-state timer (like the recording timer).
        self.latency = float(latency) if latency is not None else None
        self.preview_text = ""
        # Adaptive: grow the pill to fit the full result (complaint #1/#5).
        self._fit_content()
        self.show()
        if self._hide_timer:
            self.root.after_cancel(self._hide_timer)
        # F6: durations are user-configurable (气泡外观 card); defaults 2s short / 5s long.
        dur = self.done_short_ms if len(text or "") <= 40 else self.done_long_ms
        self._hide_timer = self.root.after(dur, self.hide)

    def show_cancelled(self):
        self.state = "cancelled"
        self.preview_text = ""
        self._draw_pill()
        self.show()
        if self._hide_timer:
            self.root.after_cancel(self._hide_timer)
        self._hide_timer = self.root.after(800, self.hide)

    def show_error(self, message=""):
        self.state = "error"
        self.text = message
        self.preview_text = ""
        # Adaptive: grow the pill to fit a long error message (complaint #1/#5).
        self._fit_content()
        self.show()
        if self._hide_timer:
            self.root.after_cancel(self._hide_timer)
        self._hide_timer = self.root.after(2200, self.hide)

    def _schedule_tick(self):
        if self._tick_job is not None:
            try:
                self.root.after_cancel(self._tick_job)
            except Exception:
                pass
            self._tick_job = None

        if self.state in ("recording", "transcribing"):
            # PERF: arm the next tick BEFORE doing the draw work. The old order
            # (draw, then after(40)) made the real frame period 40 ms + draw time, so
            # a slow frame permanently lowered the rate and the animation drifted.
            self._tick_job = self.root.after(40, self._schedule_tick)
            self._draw_pill()


# ─── Standalone Runner / Poller Mode ──────────────────────────────
class StandaloneOverlayPoller:
    """Fallback runner that monitors overlay_state.json for decoupled processes."""

    def __init__(self):
        self.overlay = VoxOverlay()
        self._last_hide_ts = 0.0  # E6: wall-clock time of the last hide()
        # PERF: identity of the state file we last parsed, so an unchanged file is not
        # re-opened/re-parsed 25x per second (see _tick). None = "nothing read yet".
        self._state_sig = None
        # Wrap hide so EVERY hide path (auto-hide timer / idle state) is timestamped;
        # the stale-done check below relies on it to not resurrect a finished pill.
        _orig_hide = self.overlay.hide
        poller_self = self
        def _hide_and_mark():
            _orig_hide()
            poller_self._last_hide_ts = time.time()
        self.overlay.hide = _hide_and_mark
        self._tick()

    def _tick(self):
        try:
            # E1: hot-apply bubble appearance changes from the admin panel (config.json).
            mtime = self.overlay._cfg_file_mtime()
            if mtime and mtime != self.overlay._cfg_mtime:
                self.overlay._cfg_mtime = mtime
                ov_cfg = self.overlay.cfg.get("overlay", {})
                # Re-read fresh so GUI "保存并实时应用" takes effect without a restart.
                try:
                    self.overlay.cfg = core.ConfigManager.load()
                    ov_cfg = self.overlay.cfg.get("overlay", {})
                except Exception:
                    pass
                self.overlay._apply_ui_config(ov_cfg)
            if os.path.exists(STATE_FILE):
                # PERF: only parse the state file when it actually changed on disk. The
                # daemon debounces its writes, so while idle the file is untouched and
                # the old code still re-opened + re-parsed it 25x/second forever.
                try:
                    _st = os.stat(STATE_FILE)
                    _sig = (_st.st_mtime_ns, _st.st_size)
                except Exception:
                    _sig = None
                if _sig != self._state_sig:
                    with open(STATE_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._apply_state(data)
                    # Mark it applied only after the state machine ran cleanly, so a
                    # transient failure still retries on the next tick (as before).
                    self._state_sig = _sig
        except Exception:
            pass

        # PERF: adaptive cadence — 40 ms while the pill is visible or animating (keeps
        # the live waveform smooth), 250 ms while it is hidden and idle. An idle,
        # hidden overlay has no reason to wake up 25x/second.
        visible = (self.overlay.visible
                   or self.overlay.state in ("recording", "transcribing"))
        self.overlay.root.after(40 if visible else 250, self._tick)

    def _apply_state(self, data):
        """Apply one parsed overlay_state.json payload. This is the original state
        machine, extracted unchanged so _tick can skip the whole file read when the
        file on disk has not changed."""
        st = data.get("state", "idle")
        txt = data.get("text", "")
        lvl = float(data.get("level", 0.0))
        hint = data.get("hint", "")
        s_time = data.get("start_time")
        preview = data.get("preview", "")
        engine = data.get("engine", "")
        if engine:
            self.overlay.engine = engine

        if st == "recording":
            if self.overlay.state != "recording":
                self.overlay.show_recording(hint, start_time=s_time, preview=preview)
            else:
                if hint:
                    self.overlay.hint = hint
                self.overlay.update_preview(preview)
            self.overlay.update_level(lvl)
        elif st == "transcribing":
            if self.overlay.state != "transcribing":
                self.overlay.show_transcribing()
        elif st == "done":
            # E6: don't resurrect a stale done. hide() resets state to idle, so
            # a done entry that predates our last hide is the finished pill, not
            # a new one — re-showing it in a 40ms tick loop made the bubble
            # "never disappear" (hide for 40ms, re-show, repeat).
            stale = False
            upd = data.get("updated_at", "")
            if self._last_hide_ts and self.overlay.state == "idle":
                try:
                    from datetime import datetime as _dt
                    stale = _dt.fromisoformat(upd).timestamp() <= self._last_hide_ts
                except Exception:
                    stale = False
            if not stale and (self.overlay.state != "done" or self.overlay.text != txt):
                self.overlay.show_done(txt, latency=data.get("latency"))
        elif st == "cancelled":
            if self.overlay.state != "cancelled":
                self.overlay.show_cancelled()
        elif st == "error":
            if self.overlay.state != "error" or self.overlay.text != txt:
                self.overlay.show_error(txt)
        elif st == "idle":
            if self.overlay.state != "idle":
                self.overlay.hide()
                self._last_hide_ts = time.time()

    def run(self):
        self.overlay.root.mainloop()


def _self_register_pid():
    """Write our real OS pid to overlay.pid so the parent daemon / verifier can track
    liveness deterministically. Popen.pid is unreliable under some launch shims (sandbox
    wraps process creation), so the child reports its own getpid() instead. Removed on
    clean exit; cleared by the daemon's kill_overlay() as well."""
    try:
        with open(os.path.join(SCRIPT_DIR, "overlay.pid"), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


# ─── Parent watchdog (orphan prevention) ──────────────────────────
def _resolve_parent_pid():
    """PID of the daemon that owns this overlay, or None when running standalone.

    Resolution order: the --parent-pid argument passed by vox_daemon.spawn_overlay,
    then the VOX_PARENT_PID environment variable, then the daemon's vox.pid file.
    """
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--parent-pid" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return None
        if arg.startswith("--parent-pid="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                return None
    env = (os.environ.get("VOX_PARENT_PID") or "").strip()
    if env.isdigit():
        return int(env)
    try:
        with open(os.path.join(SCRIPT_DIR, "vox.pid"), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _start_parent_watchdog(overlay, parent_pid, interval=2.0):
    """Exit as soon as the owning daemon is gone.

    WHY THIS EXISTS: without it the overlay is an orphan that keeps its window on
    screen FOREVER. If the daemon is force-killed (a crash, Task Manager, or a test
    runner that kills only the parent), nothing ever clears the pill — the user is
    left with a stuck bubble that cannot be dismissed, and with no daemon running
    there is no tray menu to quit from either. That is exactly the observed
    "桌面卡死一个气泡不会退" failure.

    Standalone / visual-ping runs pass no parent pid and are therefore never killed.
    """
    if not parent_pid:
        return None

    def worker():
        while True:
            time.sleep(interval)
            try:
                alive = core._process_alive(parent_pid)
            except Exception:
                alive = True          # never tear down the UI on a probe error
            if alive:
                continue
            # Tidy teardown first, then guarantee it: this process must not linger.
            try:
                overlay.root.quit()
            except Exception:
                pass
            time.sleep(0.3)
            os._exit(0)

    thread = threading.Thread(target=worker, daemon=True,
                              name="vox-overlay-parent-watch")
    thread.start()
    return thread


if __name__ == "__main__":
    _self_register_pid()
    try:
        _poller = StandaloneOverlayPoller()
        # Watch the daemon that spawned us, so we can never become a stuck bubble.
        _start_parent_watchdog(_poller.overlay, _resolve_parent_pid())
        _poller.run()
    finally:
        try:
            gone = os.path.join(SCRIPT_DIR, "overlay.pid")
            if os.path.exists(gone):
                os.unlink(gone)
        except Exception:
            pass
