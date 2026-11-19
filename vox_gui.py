#!/usr/bin/env python3
"""
Vox Manager — Settings & Diagnostic Control Center (Windows).

Features:
- Live audio level meter & manual test record/transcribe
- Instant microphone picker with friendly names & 1-second auto-calibration
- Hotkey configuration & presets (adaptive hold/tap, Esc to cancel)
- Audio cue feedback & smart clipboard protection toggles
- Groq API Key & STT Engine priority management
- Instant hot-reload: applied settings take effect immediately without restarting daemon
"""

import os
import sys
import json
import time
import threading
import subprocess
import socket

# ── Fast single-instance pre-claim, BEFORE heavy imports ────────────
# A losing launcher (a 2nd manager click) must exit in MILLISECONDS, not
# after 5–60s of import stalls. Claiming the named mutex up front means a
# loser never pays for numpy/PIL/vox_core/tkinter at all, so no zombie
# process lingers on a heavily-loaded disk.
_GUI_MUTEX_NAME = "Global\\Vox_Windows_GUI_SingleInstance_Mutex"
_GUI_PRE_HANDLE = None
# Set VOX_GUI_NO_SINGLETON=1 to import this module as a LIBRARY (tests, tooling,
# automation) without the guard terminating the host process. Without this escape
# hatch, `import vox_gui` calls sys.exit(0) whenever a manager window is already
# open — which silently kills any script that imports it.
if not os.environ.get("VOX_GUI_NO_SINGLETON"):
    try:
        import ctypes as _ct
        # BUG-3 consistency fix: preserve the last-error slot and declare the
        # full prototype so the 64-bit HANDLE is not truncated by ctypes.
        _k32 = _ct.WinDLL("kernel32", use_last_error=True)
        _k32.CreateMutexW.restype = _ct.c_void_p
        _k32.CreateMutexW.argtypes = [_ct.c_void_p, _ct.c_int, _ct.c_wchar_p]
        _GUI_PRE_HANDLE = _k32.CreateMutexW(None, False, _GUI_MUTEX_NAME)
        if _GUI_PRE_HANDLE and _ct.get_last_error() == 183:  # ALREADY_EXISTS
            # A manager window is alive: ask it to surface, then exit fast.
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vox.gui.restore"),
                          "w", encoding="utf-8") as _f:
                    _f.write(str(time.time()))
            except Exception:
                pass
            sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        _GUI_PRE_HANDLE = None

import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
from PIL import Image, ImageTk

try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Vox.VoiceTyping.App.1.0")
except Exception:
    pass

try:
    import keyboard
    _KB_OK = True
except Exception:
    keyboard = None
    _KB_OK = False

import vox_core as core
from vox_core import log

# 2026 design tokens: palette, CJK font stack, point/px helpers, DPI awareness.
# Deliberately a separate module of PURE style/geometry — it creates no widgets and
# manages no layout, so it cannot affect how cards size themselves.
import vox_theme as T

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON_SCRIPT = os.path.join(SCRIPT_DIR, "vox_daemon.py")
# Single-instance + restore via a Win32 named mutex + flag file (NOT a TCP
# port): the ~48100-48200 range is reserved on this machine (WSL2/Hyper-V
# dynamic exclusion), so the old 48124 RESTORE listener could never bind and
# the "wake existing window" path silently spawned duplicate windows.
def _claim_gui_instance():
    """Single-instance guard for the manager window.

    The real claim happens at module load (_GUI_PRE_HANDLE, before heavy
    imports) so a losing launcher exits in milliseconds. This function only
    registers the winning handle; it returns True iff WE are the instance.
    """
    global _GUI_INSTANCE_HANDLE
    _GUI_INSTANCE_HANDLE = _GUI_PRE_HANDLE  # keep alive; kernel frees on exit
    return _GUI_INSTANCE_HANDLE is not None

HOTKEY_PRESETS = [
    ("Shift (单键长按/单击)", ["shift"], ""),
    ("Ctrl+Alt (推荐)", ["control", "alt"], ""),
    ("Alt+Space", ["alt"], "space"),
    ("Ctrl+Shift+K", ["control", "shift"], "k"),
    ("F5", [], "f5"),
    ("F8", [], "f8"),
]
MODIFIERS = [("Ctrl", "control"), ("Alt", "alt"), ("Shift", "shift"), ("Win", "win")]
_MOD_TOKENS = ("control", "ctrl", "alt", "shift", "win")

DEFAULT_DEV_PROMPT = (
    "用户在讨论软件开发、代码实现、AI与大模型、技术方案的中文口述内容，"
    "常夹英文技术术语，比如 Python、JavaScript、Git、API、prompt、token、LLM、"
    "Whisper、Groq、CUDA、commit、PR、bug 等。"
)


def get_daemon_pid():
    """Live daemon PID from the pid file.

    BUG-2 fix: this used to call OpenProcess directly. On Windows an exited
    process object survives while any handle to it is open (subprocess holds
    one), so a killed daemon kept looking alive: the GUI then refused to start
    a new one and treated "close window" as "minimize to tray". The shared
    vox_win32.process_alive (GetExitCodeProcess == STILL_ACTIVE) is the single
    correct implementation — reuse it instead of growing a second one.
    """
    if not os.path.exists(core.PID_FILE):
        return None
    try:
        with open(core.PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        return pid if core._process_alive(pid) else None
    except Exception:
        return None


def start_daemon():
    pid = get_daemon_pid()
    if pid:
        return True
    try:
        subprocess.Popen(
            [sys.executable, DAEMON_SCRIPT],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception as e:
        messagebox.showerror("启动失败", f"无法启动守护进程: {e}")
        return False


def stop_daemon():
    # 1. Graceful exit signal first (daemon cleans up its own overlay children)
    try:
        with open(os.path.join(SCRIPT_DIR, "vox.stop"), "w", encoding="utf-8") as f:
            f.write("stop")
    except Exception:
        pass

    # 2. Wait for a graceful exit. BUG-10 fix: use the pid-file + GetExitCodeProcess
    #    probe (a single syscall) instead of find_existing_daemon_pids(), which
    #    spawns a full Win32_Process CIM scan on EVERY 100 ms iteration (up to
    #    20 scans, seconds of delay and visible GUI freeze on exit/stop).
    for _ in range(20):
        if not get_daemon_pid():
            break
        time.sleep(0.1)

    # 3. Force-kill ANY remaining vox_daemon.py processes (by command line, not just the
    #    pidfile) so no shadow instance survives (fixes B1 double-instance).
    killed = []
    try:
        killed = core.kill_all_vox_daemons()
    except Exception:
        killed = []
    # Belt-and-suspenders: direct taskkill on anything still alive
    try:
        for pid in core.find_existing_daemon_pids():
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    except Exception:
        pass

    if os.path.exists(core.PID_FILE):
        try:
            os.unlink(core.PID_FILE)
        except Exception:
            pass
    return killed


class VoxManagerApp(tk.Tk):
    BG       = "#f8f9fa"
    CARD     = "#ffffff"
    BORDER   = "#e2e8f0"
    TEXT     = "#0f172a"
    MUTED    = "#64748b"
    ACCENT   = "#2563eb"
    ACCENT_HOVER = "#1d4ed8"
    REC      = "#ef4444"
    REC_DARK = "#dc2626"
    SUCCESS  = "#10b981"
    FONT     = "Microsoft YaHei UI"

    # Fallbacks for the palette-derived surface tokens, used only if a palette key is
    # missing. SURFACE is the inset "field" fill that used to be written inline as
    # #f8fafc / #f1f5f9 on listboxes, text boxes and small buttons.
    SURFACE  = "#f8fafc"
    NEUTRAL  = "#e2e8f0"
    WARN_FG  = "#92400e"
    WARN_BG  = "#fef3c7"

    # Level-meter cadence. 80 ms while recording (a live level meter earns it), 1 Hz
    # when idle. See _tick_meter for the measured cost of the old unconditional 80 ms.
    METER_TICK_ACTIVE_MS = 80
    METER_TICK_IDLE_MS   = 1000

    # Original token -> 2026 palette key (vox_theme.build_palette). The class values
    # above remain the fallback for a palette miss; _apply_palette_tokens() overwrites
    # them per instance once the OS theme is known. REC_DARK has no distinct 2026 token,
    # so it shares `danger` — the pressed state then relies on ACCENT_HOVER-style
    # feedback rather than a darker red.
    _PALETTE_MAP = {
        "BG": "bg",
        "CARD": "card",
        "BORDER": "hairline",
        "TEXT": "text",
        "MUTED": "text2",
        "ACCENT": "accent",
        "ACCENT_HOVER": "accent_hover",
        "REC": "danger",
        "REC_DARK": "danger",
        "SUCCESS": "success_fg",
        # Inset/surface fills that used to be hardcoded hex literals scattered through
        # this file (#f8fafc / #f1f5f9 / #e2e8f0 / the #fef3c7 warn pair). In dark mode
        # those stayed bright and looked broken, so they are palette tokens now.
        "SURFACE": "entry",
        "NEUTRAL": "switch_off",
        "WARN_FG": "warn_fg",
        "WARN_BG": "warn_bg",
    }

    def __init__(self):
        super().__init__()
        # Palette FIRST, before anything reads self.BG/self.CARD or configures a widget.
        # Placed later it looked like it worked — every widget built in _build_ui() got
        # the new colours — but the root window itself had already been painted with the
        # old class-level #f8f9fa, which is exactly the kind of half-applied restyle
        # that is easy to miss.
        #
        # Colours and type only: no widget is created and no geometry manager is touched,
        # so this cannot reintroduce the card-sizing bug the canvas rebuild had (cards
        # rendered 33 px wide).
        self._theme = T.detect_os_theme()
        self.P = T.build_palette(self._theme)
        self._apply_palette_tokens()
        self.FONT = T.resolve_ui_family(self)

        self.dpi_scale = core.get_dpi_scale()
        self.title("🎙️ Vox 管理控制中心")
        # Adaptive window size: scale with real DPI (no artificial 1.35 clamp) but never
        # exceed ~90% of the monitor work area (fixes B10 — on 4K/225% the old clamp made
        # the panel far too small; now it grows correctly and stays on-screen).
        base_w, base_h = 860, 840
        try:
            _, _, mon_w, mon_h = core.get_screen_workarea()
        except Exception:
            mon_w, mon_h = 1600, 900
        mon_w, mon_h = max(1024, mon_w), max(768, mon_h)
        win_w = min(max(760, int(base_w * self.dpi_scale)), int(mon_w * 0.9))
        win_h = min(max(700, int(base_h * self.dpi_scale)), int(mon_h * 0.9))
        self.geometry(f"{win_w}x{win_h}")
        self.minsize(760, 680)
        self.configure(bg=self.BG)

        # ── Window Icon (taskbar + title bar) ──
        ico_path = core.ensure_vox_ico()
        if ico_path and os.path.exists(ico_path):
            try:
                self.iconbitmap(ico_path)
            except Exception:
                pass
            try:
                ico_img = Image.open(ico_path)
                self._icon_photo = ImageTk.PhotoImage(ico_img.resize((64, 64), Image.LANCZOS))
                self.iconphoto(True, self._icon_photo)
            except Exception:
                pass

        self.cfg = core.ConfigManager.load()
        self.pipeline = core.AudioPipeline()
        self._meter_running = False
        # Tracks whether the idle placeholder has already been painted, so the idle
        # meter tick can skip redundant widget writes (see _tick_meter).
        self._meter_idle_written = False
        self._recording_manual = False
        self._manual_audio = None
        self._calibrating = False
        self._recording_hotkey = False

        self._setup_style()
        self._build_ui()
        self._load_config_to_ui()

        # ── Window close -> minimize to tray instead of quit ──
        self.protocol("WM_DELETE_WINDOW", self._on_close_window)

        # Single-instance was enforced in _claim_gui_instance() (named mutex).
        # The "surface this window" wake signal is a flag file checked in the
        # ~2s refresh loop below (port-free; the old 48124 TCP listener could
        # never bind on this machine — that range is reserved).

        # Start live level timer
        self.after(200, self._tick_meter)
        # Periodic daemon status refresh
        self.after(500, self._periodic_refresh)

    def _on_close_window(self):
        """On window close: if daemon is running, minimize to tray; otherwise truly exit."""
        pid = get_daemon_pid()
        if pid:
            self.withdraw()  # Hide window; daemon + tray icon remain active
        else:
            self._shutdown()

    def _shutdown(self):
        """Full shutdown: destroy window (the named mutex releases automatically)."""
        self.destroy()

    def _check_restore_request(self):
        """Surface the window if a launcher/daemon requested it (flag file).
        Called from the ~2s refresh loop — port-free and crash-proof."""
        try:
            if core.gui_check_restore_request():
                self._restore_window()
        except Exception:
            pass

    def _restore_window(self):
        """Bring window back from minimized/withdrawn state."""
        self.deiconify()
        self.lift()
        self.focus_force()

    def _apply_palette_tokens(self):
        """Copy the 2026 palette onto the attribute names this file already uses.

        Every call site in this GUI says `bg=self.CARD`, `fg=self.MUTED` and so on, so
        remapping the tokens here restyles the whole window (light AND dark) without
        touching a single widget — which is the whole point of doing it this way.
        """
        for attr, key in self._PALETTE_MAP.items():
            val = self.P.get(key)
            if isinstance(val, str):
                setattr(self, attr, val)

    def _setup_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=self.BG, foreground=self.TEXT, font=(self.FONT, 10))
        st.configure("TFrame", background=self.BG)
        st.configure("TLabel", background=self.BG, foreground=self.TEXT)
        st.configure("TButton", background=self.CARD, foreground=self.TEXT, font=(self.FONT, 10), padding=(10, 6))
        st.configure("TCheckbutton", background=self.CARD, foreground=self.TEXT, font=(self.FONT, 10))
        st.configure("TCombobox", font=(self.FONT, 10), padding=5)
        # The trough used to be a hardcoded light grey ("#e2e8f0"), which stayed light
        # even in dark mode. Take it from the palette instead.
        st.configure("Horizontal.TProgressbar", background=self.ACCENT,
                     troughcolor=self.P.get("switch_off", "#e2e8f0"))

    def _card(self, parent, **pack):
        outer = tk.Frame(parent, bg=self.CARD, highlightbackground=self.BORDER, highlightthickness=1)
        # B10: track outer card frames so the tabbed (categorized) panel can show/hide by category.
        try:
            self._cards_outer.append(outer)
        except Exception:
            pass
        # C1: the content frame is grid-managed (two columns) — defer card placement
        # to _apply_tab, which packs them into the columns. Other parents pack normally.
        if parent is getattr(self, "_content_frame", None):
            pass
        else:
            outer.pack(**pack)
        inner = tk.Frame(outer, bg=self.CARD, padx=20, pady=14)
        inner.pack(fill="both", expand=True)
        return inner

    def _title(self, parent, text):
        tk.Label(parent, text=text, bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9, "bold")).pack(anchor="w", pady=(0, 10))

    def _apply_tab(self, category):
        """B10: show only the cards belonging to `category`, hide the rest; highlight active tab.
        C1: cards with >=2 per tab are laid out in the two columns (fill the wide window);
        single-card tabs span the full width."""
        self._active_tab = category
        for outer in self._cards_outer:
            if self._cat_of.get(outer) == category:
                pass  # re-packed below
            else:
                outer.pack_forget()
        cards = [o for o in self._cards_outer if self._cat_of.get(o) == category]
        if len(cards) >= 2:
            self._content_frame.grid_columnconfigure(0, weight=1)
            self._content_frame.grid_columnconfigure(1, weight=1)
            self._col_left.grid()
            self._col_right.grid()
            for i, o in enumerate(cards):
                o.pack(in_=self._col_left if i % 2 == 0 else self._col_right,
                       fill="both", expand=True)
        else:
            self._content_frame.grid_columnconfigure(1, weight=0)
            self._col_right.grid_remove()
            for o in cards:
                o.pack(in_=self._col_left, fill="both", expand=True)
        for name, b in self._tab_btns.items():
            active = (name == category)
            b.config(bg=self.ACCENT if active else self.CARD,
                     fg="white" if active else self.MUTED)

    def _scrollable(self, parent):
        wrap = tk.Frame(parent, bg=self.BG)
        wrap.pack(fill="both", expand=True)
        cv = tk.Canvas(wrap, bg=self.BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=cv.yview)
        vsb.pack(side="right", fill="y")
        cv.configure(yscrollcommand=vsb.set)
        cv.pack(side="left", fill="both", expand=True)
        frame = tk.Frame(cv, bg=self.BG)
        cid = cv.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(cid, width=e.width))

        def _on_mousewheel(event):
            try:
                cv.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass

        cv.bind_all("<MouseWheel>", _on_mousewheel)
        return frame

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=self.BG)
        hdr.pack(fill="x", padx=24, pady=(16, 6))
        tk.Label(hdr, text="Vox", bg=self.BG, fg=self.TEXT, font=(self.FONT, 22, "bold")).pack(side="left")
        tk.Label(hdr, text="极速智能语音转写 · 丝滑原生交互", bg=self.BG, fg=self.MUTED, font=(self.FONT, 10)).pack(side="left", padx=(12, 0), pady=(8, 0))

        # Daemon Status Banner
        self.status_badge = tk.Label(hdr, text="检测中...", bg=self.NEUTRAL, fg=self.TEXT, font=(self.FONT, 9, "bold"), padx=10, pady=4)
        self.status_badge.pack(side="right")

        # Scrollable container
        container = self._scrollable(self)
        col = tk.Frame(container, bg=self.BG)
        col.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        self._content_frame = col
        # C1: two-column card area — tabs with >=2 cards fill the wide window
        # side-by-side (no huge empty middle); single-card tabs span full width.
        self._col_left = tk.Frame(col, bg=self.BG)
        self._col_right = tk.Frame(col, bg=self.BG)
        col.grid_columnconfigure(0, weight=1)
        col.grid_columnconfigure(1, weight=1)
        col.grid_rowconfigure(0, weight=1)
        self._col_left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._col_right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # B10: group the formerly flat single-column cards into categorized tabs
        # (触发与热键 / 麦克风 / 引擎与文本 / 历史记录 / 系统). Build order below maps
        # to the category index table in _idx_cat.
        self._cards_outer = []
        self._tab_cats = [
            "🎯 触发与热键",   # dictation(0), hotkey(3)
            "🎚️ 麦克风",       # mic(2)
            "🧠 引擎与文本",   # result(1), prompt(4), fidelity(5), engine(7)
            "🫧 气泡外观",     # appearance(9)
            "🕘 历史记录",     # history(8)
            "⚙️ 系统",         # perf(6)
        ]
        self._cat_of = {}
        self._active_tab = self._tab_cats[0]

        self._build_dictation_card(col)
        self._build_result_card(col)
        self._build_mic_card(col)
        self._build_hotkey_card(col)
        self._build_prompt_card(col)
        self._build_fidelity_card(col)
        self._build_perf_card(col)
        self._build_engine_card(col)
        self._build_history_card(col)
        self._build_appearance_card(col)

        # Assign each built card to a category by its build-order index.
        _idx_cat = {
            0: self._tab_cats[0], 3: self._tab_cats[0],
            2: self._tab_cats[1],
            1: self._tab_cats[2], 4: self._tab_cats[2], 5: self._tab_cats[2], 7: self._tab_cats[2],
            9: self._tab_cats[3],
            8: self._tab_cats[4],
            6: self._tab_cats[5],
        }
        for i, outer in enumerate(self._cards_outer):
            self._cat_of[outer] = _idx_cat.get(i, self._tab_cats[0])

        # Category tab bar.
        tabbar = tk.Frame(self, bg=self.BG)
        tabbar.pack(fill="x", padx=24, pady=(10, 0))
        self._tab_btns = {}
        for name in self._tab_cats:
            b = tk.Button(tabbar, text=name, command=(lambda n=name: self._apply_tab(n)),
                          bg=self.CARD, fg=self.TEXT, bd=0, font=(self.FONT, 9, "bold"),
                          padx=16, pady=6, cursor="hand2", highlightbackground=self.BORDER,
                          highlightthickness=1)
            b.pack(side="left", padx=(0, 8))
            self._tab_btns[name] = b

        self._apply_tab(self._active_tab)

        # Bottom Action Bar
        bar = tk.Frame(self, bg=self.BG)
        bar.pack(fill="x", padx=24, pady=(0, 14))

        def make_btn(txt, cmd, fg, bg=self.CARD):
            b = tk.Button(bar, text=txt, command=cmd, bg=bg, fg=fg, bd=0, font=(self.FONT, 10), padx=16, pady=7,
                          cursor="hand2", highlightbackground=self.BORDER, highlightthickness=1)
            b.pack(side="left", padx=(0, 8))
            return b

        make_btn("启动守护进程", self._action_start, self.TEXT)
        make_btn("重启守护进程", self._action_restart, self.TEXT)
        make_btn("停止守护进程", self._action_stop, self.REC)
        make_btn("🧹 退出GPU/释放显存", self._action_offload_gpu, self.WARN_FG, bg=self.WARN_BG)
        make_btn("保存并实时应用", self.save_and_apply_all, "#ffffff", bg=self.ACCENT)
        # G5: config backup/restore — move the whole setup to another machine.
        make_btn("⬆ 导出配置", self._export_config, self.MUTED)
        make_btn("⬇ 导入配置", self._import_config, self.MUTED)

        tk.Button(bar, text="退出管理台", command=self._shutdown, bg=self.BG, fg=self.MUTED, bd=0,
                  font=(self.FONT, 10), padx=16, pady=7, cursor="hand2").pack(side="right")
        tk.Button(bar, text="最小化到托盘", command=self.withdraw, bg=self.BG, fg=self.MUTED, bd=0,
                  font=(self.FONT, 10), padx=16, pady=7, cursor="hand2").pack(side="right", padx=(0, 8))

    # ── History Archive Card (complaint #6) ────────────────────────
    def _build_history_card(self, col):
        card = self._card(col, fill="x", pady=(0, 12))
        self._title(card, "历史记录（最近转写存档）")

        # G5: history search — filter the archive live (the dictation log grows fast).
        srch = tk.Frame(card, bg=self.CARD)
        srch.pack(fill="x", pady=(0, 6))
        self.v_hist_search = tk.StringVar()
        e = ttk.Entry(srch, textvariable=self.v_hist_search)
        e.pack(side="left", fill="x", expand=True)
        tk.Label(srch, text="搜索", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 8)).pack(side="left", padx=(8, 0))
        # ONE binding, debounced. This used to be bound to both <KeyRelease> and a
        # StringVar trace, so every keystroke ran the query twice — and each run
        # opened a fresh SQLite connection plus CREATE TABLE/INDEX, a COUNT(*) and a
        # 200-row SELECT. Typing in this box was ~10-20 connects and DDL statements
        # per second, which is what made the window feel like it was stuttering.
        self.v_hist_search.trace_add("write", lambda *a: self._hist_search_changed())

        lst_frame = tk.Frame(card, bg=self.CARD)
        lst_frame.pack(fill="both", expand=True)

        self.hist_list = tk.Listbox(lst_frame, bg=self.SURFACE, fg=self.TEXT, bd=0,
                                    font=(self.FONT, 9), activestyle="none",
                                    selectbackground=self.ACCENT, selectforeground="white",
                                    exportselection=False)
        vsb = ttk.Scrollbar(lst_frame, orient="vertical", command=self.hist_list.yview)
        self.hist_list.configure(yscrollcommand=vsb.set)
        self.hist_list.bind("<Double-Button-1>", lambda e: self._hist_copy_selected())
        self.hist_list.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.hist_lbl_status = tk.Label(card, text="", bg=self.CARD, fg=self.MUTED,
                                        font=(self.FONT, 9), anchor="w")
        self.hist_lbl_status.pack(fill="x", pady=(6, 0))

        btn_row = tk.Frame(card, bg=self.CARD)
        btn_row.pack(fill="x", pady=(8, 0))

        def hist_btn(txt, cmd):
            b = tk.Button(btn_row, text=txt, command=cmd, bg=self.SURFACE, fg=self.TEXT, bd=0,
                          font=(self.FONT, 9), padx=12, pady=5, cursor="hand2",
                          highlightbackground=self.BORDER, highlightthickness=1)
            b.pack(side="left", padx=(0, 6))
            return b

        hist_btn("📋 复制选中", self._hist_copy_selected)
        hist_btn("🔄 刷新", self._hist_refresh)
        hist_btn("🗑 清空全部", self._hist_clear)

        try:
            self._hist_refresh()
        except Exception:
            pass

    def _hist_search_changed(self):
        """Debounce the history filter so typing does not hammer SQLite."""
        job = getattr(self, "_hist_debounce_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._hist_debounce_job = self.after(250, self._hist_apply_filter)

    def _hist_apply_filter(self):
        """Re-filter the already-loaded rows in memory — no database round trip."""
        self._hist_debounce_job = None
        self._hist_render()

    def _hist_refresh(self):
        """Reload from the archive, then render."""
        try:
            self._hist_rows_all = core.read_history(200)
        except Exception:
            self._hist_rows_all = []
        self._hist_render()

    def _hist_render(self):
        rows = list(getattr(self, "_hist_rows_all", []))
        # G5: live search filter (case-insensitive, matches text/engine/timestamp).
        q = (self.v_hist_search.get().strip().lower()
             if hasattr(self, "v_hist_search") else "")
        if q:
            rows = [r for r in rows if q in str(r.get("text", "")).lower()
                    or q in str(r.get("engine", "")).lower()
                    or q in str(r.get("timestamp", "")).lower()]
        # Remember exactly what is on screen. Copy-by-index then cannot drift out of
        # sync with the filtered view (it used to index an UNFILTERED read_history()
        # result, so after any search "copy selected" copied the wrong entry).
        self._hist_rows_visible = rows
        self.hist_list.delete(0, self.hist_list.size())
        for i, r in enumerate(rows, 1):
            ts = str(r.get("timestamp", "?"))[:19].replace("T", " ")
            garb = " \u26a0" if r.get("garbage_flag") else ""
            flag = ""
            if not r.get("pasted"):
                flag += " ·未粘贴"
            if not r.get("copied"):
                flag += " ·未复制"
            self.hist_list.insert(tk.END,
                                  f"{i}. [{ts}] {r.get('chars', 0)}字 {r.get('engine')} \"{str(r.get('text', ''))[:40]}\"{garb}{flag}")
        extra = f" · 搜索“{q}”" if q else ""
        self.hist_lbl_status.config(
            text=f"共 {len(rows)} 条{extra}（⚠=低置信）。选中后点【复制选中】或双击可复制全文。")

    def _hist_copy_selected(self):
        sel = self.hist_list.curselection()
        if not sel:
            return
        try:
            rows = getattr(self, "_hist_rows_visible", None)
            if rows is None:          # list not rendered yet
                self._hist_refresh()
                rows = getattr(self, "_hist_rows_visible", [])
            idx = sel[0]
            if 0 <= idx < len(rows):
                text = rows[idx].get("text", "")
                try:
                    self.clipboard_clear()
                    self.clipboard_append(text)
                    self.update_idletasks()
                    tail = text if len(text) <= 60 else text[:60] + "…"
                    self.hist_lbl_status.config(text=f"已复制到剪贴板：{tail}")
                except Exception as e:
                    self.hist_lbl_status.config(text=f"复制失败: {e}")
        except Exception:
            pass

    def _hist_clear(self):
        try:
            core.clear_history()
            self._hist_refresh()
        except Exception:
            pass

    def _build_dictation_card(self, col):
        card = self._card(col, fill="x", pady=(0, 12))
        self._title(card, "测试口述 (无需热键)")

        self.btn_rec = tk.Button(card, text="●  点击测试录音", command=self.toggle_manual_rec,
                                 bg=self.REC, fg="white", bd=0, font=(self.FONT, 12, "bold"),
                                 pady=10, cursor="hand2", activebackground=self.REC_DARK, activeforeground="white")
        self.btn_rec.pack(fill="x", pady=(0, 10))

        self.meter_bar = ttk.Progressbar(card, mode="determinate", maximum=1.0)
        self.meter_bar.pack(fill="x", ipady=2)

        self.meter_label = tk.Label(card, text="电平: —", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9), anchor="w")
        self.meter_label.pack(fill="x", pady=(4, 0))

    def _build_result_card(self, col):
        card = self._card(col, fill="x", pady=(0, 12))
        self._title(card, "最近转写结果")

        self.txt_result = tk.Text(card, height=4, bg=self.SURFACE, fg=self.TEXT, bd=0, wrap="word",
                                  font=("Consolas", 10), highlightthickness=1, highlightbackground=self.BORDER)
        self.txt_result.pack(fill="both", expand=True)

        self.lbl_result_meta = tk.Label(card, text="等待录音测试...", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9), anchor="w")
        self.lbl_result_meta.pack(fill="x", pady=(6, 0))

    def _build_mic_card(self, col):
        card = self._card(col, fill="x", pady=(0, 12))
        self._title(card, "麦克风设备与校准")

        row = tk.Frame(card, bg=self.CARD)
        row.pack(fill="x", pady=(0, 6))

        self.cbo_mic_var = tk.StringVar()
        self.cbo_mic = ttk.Combobox(row, textvariable=self.cbo_mic_var, state="readonly", width=42)
        self.cbo_mic.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.btn_calib = tk.Button(row, text="⚡ 一键校准最强麦克风", command=self.quick_calibrate_mic,
                                   bg=self.SURFACE, fg=self.TEXT, bd=0, font=(self.FONT, 9), padx=12, pady=5,
                                   cursor="hand2", highlightbackground=self.BORDER, highlightthickness=1)
        self.btn_calib.pack(side="right")

        self.lbl_mic_hint = tk.Label(card, text="提示: 若发现蓝牙耳机不收音，请点击【一键校准】自动锁定有效麦克风",
                                     bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9), anchor="w")
        self.lbl_mic_hint.pack(fill="x")

    def _build_hotkey_card(self, col):
        card = self._card(col, fill="x", pady=(0, 12))
        self._title(card, "全局热键与交互触发配置")

        self.lbl_hotkey_preview = tk.Label(card, text="当前热键: SHIFT", bg=self.CARD, fg=self.ACCENT, font=(self.FONT, 11, "bold"), anchor="w")
        self.lbl_hotkey_preview.pack(fill="x", pady=(0, 6))

        # 1. Interactive Key Recorder (User Request: 交互检测按键录制)
        rec_row = tk.Frame(card, bg=self.CARD)
        rec_row.pack(fill="x", pady=(0, 8))
        self.btn_record_hotkey = tk.Button(
            rec_row, text="⌨️ 点击交互录制热键 (按下键盘即可自动捕获)",
            command=self._start_interactive_hotkey_recording,
            bg=self.SURFACE, fg=self.TEXT, bd=0, font=(self.FONT, 9, "bold"),
            padx=12, pady=6, cursor="hand2", highlightbackground=self.BORDER, highlightthickness=1
        )
        self.btn_record_hotkey.pack(side="left")

        self.lbl_record_status = tk.Label(rec_row, text="支持单键(Shift/F5/F8)或组合键(Ctrl+Alt/Alt+Space)",
                                          bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9))
        self.lbl_record_status.pack(side="left", padx=(10, 0))

        # 2. Presets
        p_row = tk.Frame(card, bg=self.CARD)
        p_row.pack(fill="x", pady=(0, 8))
        tk.Label(p_row, text="常用预设: ", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9)).pack(side="left")
        for label, mods, k in HOTKEY_PRESETS:
            cmd = (lambda m=mods, key=k: self._apply_preset(m, key))
            b = tk.Button(p_row, text=label, command=cmd, bg=self.SURFACE, fg=self.TEXT, bd=0, font=(self.FONT, 9),
                          padx=8, pady=3, cursor="hand2", highlightbackground=self.BORDER, highlightthickness=1)
            b.pack(side="left", padx=3)

        # 3. Fine-tuning checkboxes
        c_row = tk.Frame(card, bg=self.CARD)
        c_row.pack(fill="x", pady=(0, 8))
        tk.Label(c_row, text="手动微调: ", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9)).pack(side="left", padx=(0, 4))
        self.v_ctrl = tk.BooleanVar()
        self.v_alt = tk.BooleanVar()
        self.v_shift = tk.BooleanVar()
        self.v_win = tk.BooleanVar()
        self.v_key = tk.StringVar()

        for label, var in [("Ctrl", self.v_ctrl), ("Alt", self.v_alt), ("Shift", self.v_shift), ("Win", self.v_win)]:
            cb = ttk.Checkbutton(c_row, text=label, variable=var, command=self._update_hotkey_preview)
            cb.pack(side="left", padx=4)

        tk.Label(c_row, text="字母/功能键:", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9)).pack(side="left", padx=(8, 4))
        self.entry_key = ttk.Entry(c_row, textvariable=self.v_key, width=6)
        self.entry_key.pack(side="left")
        self.v_key.trace_add("write", lambda *a: self._update_hotkey_preview())

        # 4. Trigger Mode (User Request: 长按、短按、松手等标准做法)
        # C1: stacked vertically — the long option labels used to overflow the card's
        # right edge (third radio clipped) in a single horizontal row.
        m_box = tk.Frame(card, bg=self.CARD)
        m_box.pack(fill="x", pady=(2, 6))
        tk.Label(m_box, text="触发模式: ", bg=self.CARD, fg=self.TEXT, font=(self.FONT, 9, "bold")).pack(anchor="w")
        self.v_mode = tk.StringVar(value="adaptive")
        _MODE_OPTS = [
            ("自适应双模：长按口述松手出字 / 单击轻按免手持录音（推荐）", "adaptive"),
            ("仅长按说话：按住说话，松手立即出字", "hold_only"),
            ("仅单击开关：按一下录音 / 再按结束", "toggle_only"),
        ]
        for _txt, _val in _MODE_OPTS:
            ttk.Radiobutton(m_box, text=_txt, variable=self.v_mode,
                           value=_val).pack(anchor="w", padx=(10, 0))

        # 5. Hold Duration Threshold Slider
        t_row = tk.Frame(card, bg=self.CARD)
        t_row.pack(fill="x", pady=(2, 0))
        tk.Label(t_row, text="长按判定阈值: ", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9)).pack(side="left")
        self.v_hold_thresh = tk.IntVar(value=350)
        self.lbl_thresh_val = tk.Label(t_row, text="350 毫秒 (0.35秒)", bg=self.CARD, fg=self.ACCENT, font=(self.FONT, 9, "bold"))
        self.lbl_thresh_val.pack(side="right")
        self.scale_thresh = ttk.Scale(
            t_row, from_=150, to=600, orient="horizontal",
            variable=self.v_hold_thresh,
            command=lambda v: self.lbl_thresh_val.config(text=f"{int(float(v))} 毫秒 ({float(v)/1000:.2f}秒)")
        )
        self.scale_thresh.pack(side="left", fill="x", expand=True, padx=12)

    def _start_interactive_hotkey_recording(self):
        if not _KB_OK:
            messagebox.showerror("错误", "当前环境未加载 keyboard 键盘监听库")
            return
        if self._recording_hotkey:
            return
        self._recording_hotkey = True
        self.btn_record_hotkey.config(
            text="🔴 正在监听键盘... 请按下任意单键或组合键 (Esc 取消)",
            bg=self.REC, fg="#ffffff", state="disabled"
        )
        self.lbl_record_status.config(text="请在键盘上直接按下您期望的热键...", fg=self.REC)

        def rec_worker():
            keys_down = set()
            cancel = False
            done_event = threading.Event()

            def on_event(e):
                nonlocal cancel
                name = (e.name or "").lower().strip()
                if name in ("left ctrl", "right ctrl", "control"):
                    name = "ctrl"
                elif name in ("left alt", "right alt"):
                    name = "alt"
                elif name in ("left shift", "right shift"):
                    name = "shift"
                elif name in ("left windows", "right windows", "windows"):
                    name = "win"

                if e.event_type == "down":
                    if name == "esc":
                        cancel = True
                        done_event.set()
                        return
                    keys_down.add(name)
                elif e.event_type == "up":
                    if keys_down:
                        done_event.set()

            try:
                hook = keyboard.hook(on_event)
                done_event.wait(timeout=12.0)
                try:
                    keyboard.unhook(hook)
                except Exception:
                    pass
            except Exception as err:
                log(f"热键监听失败: {err}", "WARN")

            if cancel or not keys_down:
                self.after(0, lambda: self._on_record_hotkey_done(None))
                return

            ordered = []
            for m in ("ctrl", "alt", "shift", "win"):
                if m in keys_down:
                    ordered.append(m)
            for k in sorted(keys_down):
                if k not in ("ctrl", "alt", "shift", "win"):
                    ordered.append(k)

            res = "+".join(ordered)
            self.after(0, lambda: self._on_record_hotkey_done(res))

        threading.Thread(target=rec_worker, daemon=True).start()

    def _on_record_hotkey_done(self, res):
        self._recording_hotkey = False
        self.btn_record_hotkey.config(
            text="⌨️ 点击交互录制热键 (按下键盘即可自动捕获)",
            bg=self.SURFACE, fg=self.TEXT, state="normal"
        )
        if not res:
            self.lbl_record_status.config(text="已取消录制 (无按键或已按 Esc 撤销)", fg=self.MUTED)
            return

        self.lbl_record_status.config(text=f"✅ 成功录制按键: [{res.upper()}]", fg=self.SUCCESS)
        self._apply_combo_string(res)

    def _apply_combo_string(self, combo_str):
        tokens = [t.lower().strip() for t in combo_str.split("+") if t.strip()]
        self.v_ctrl.set("ctrl" in tokens or "control" in tokens)
        self.v_alt.set("alt" in tokens)
        self.v_shift.set("shift" in tokens)
        self.v_win.set("win" in tokens)
        reg_keys = [t for t in tokens if t not in ("ctrl", "control", "alt", "shift", "win")]
        self.v_key.set(reg_keys[0] if reg_keys else "")
        self._update_hotkey_preview()

    def _build_prompt_card(self, col):
        card = self._card(col, fill="x", pady=(0, 12))

        hdr_row = tk.Frame(card, bg=self.CARD)
        hdr_row.pack(fill="x", pady=(0, 6))
        tk.Label(hdr_row, text="AI 与软件开发专属提示词 (Whisper Prompt)", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9, "bold")).pack(side="left")

        btn_reset = tk.Button(
            hdr_row, text="↺ 恢复推荐开发预设", command=self._reset_dev_prompt,
            bg=self.SURFACE, fg=self.ACCENT, bd=0, font=(self.FONT, 9), padx=10, pady=3,
            cursor="hand2", highlightbackground=self.BORDER, highlightthickness=1
        )
        btn_reset.pack(side="right")

        self.txt_prompt = tk.Text(card, height=3, bg=self.SURFACE, fg=self.TEXT, bd=0, wrap="word",
                                  font=(self.FONT, 9), highlightthickness=1, highlightbackground=self.BORDER)
        self.txt_prompt.pack(fill="x", pady=(0, 6))

        hint_text = (
            "💡 提示: Whisper 会根据上述提示词引导识别上下文。输入代码术语、公司名词、常用英汉词汇，"
            "可显著提升中英文专业术语转写精准度，并有效避免无声时产生韩文或日文幻觉。"
        )
        tk.Label(card, text=hint_text, bg=self.CARD, fg=self.MUTED, font=(self.FONT, 8),
                 anchor="w", justify="left", wraplength=680).pack(fill="x")

    def _reset_dev_prompt(self):
        self.txt_prompt.delete("1.0", "end")
        self.txt_prompt.insert("1.0", DEFAULT_DEV_PROMPT)

    def _build_fidelity_card(self, col):
        card = self._card(col, fill="x", pady=(0, 12))
        self._title(card, "口语保真度与语气风格控制 (真实口语 vs 书面润色)")

        # Mode Selection
        f_row = tk.Frame(card, bg=self.CARD)
        f_row.pack(fill="x", pady=(0, 6))
        self.v_fidelity_mode = tk.StringVar(value="verbatim")
        rb1 = ttk.Radiobutton(
            f_row, text="🟢 原生口语直录 (强烈推荐 - 100% 真实还原口述语气，保留大白话及语气词，零过度干预)",
            variable=self.v_fidelity_mode, value="verbatim"
        )
        rb1.pack(anchor="w", pady=2)
        rb2 = ttk.Radiobutton(
            f_row, text="🔵 书面语自动润色 (自动将大白话梳理为书面报告文风，适合正式邮件撰写)",
            variable=self.v_fidelity_mode, value="polished"
        )
        rb2.pack(anchor="w", pady=2)

        # Auto period toggle
        p_row = tk.Frame(card, bg=self.CARD)
        p_row.pack(fill="x", pady=(4, 6))
        self.v_auto_period = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            p_row, text="强制句尾追加句号 (取消勾选可原汁原味输出短句/语音片段，不强行补标点)",
            variable=self.v_auto_period
        ).pack(anchor="w")

        # Architectural explanation
        hint_box = tk.Frame(card, bg=self.SURFACE, padx=10, pady=8, highlightbackground=self.BORDER, highlightthickness=1)
        hint_box.pack(fill="x", pady=(4, 0))
        hint_txt = (
            "💡 为什么默认模型会出现语气重写？\n"
            "Whisper 自回归模型在识别带有引导词（如「以下是关于...的口述内容：」）的提示词时，容易产生续写偏差，"
            "把口语俚语和短句重写为书面语。在「原生口语直录」模式下，Vox 会自动净化引导词（仅保留纯专业名词作为词典），"
            "并锁定贪婪直录解码（beam_size=1, condition_on_previous_text=False），确保一字不漏、丝毫不改地输出您的真实口述！"
        )
        tk.Label(hint_box, text=hint_txt, bg=self.SURFACE, fg=self.MUTED, font=(self.FONT, 8),
                 justify="left", anchor="w", wraplength=680).pack(fill="x")

    def _build_perf_card(self, col):
        card = self._card(col, fill="x", pady=(0, 12))

        hdr_row = tk.Frame(card, bg=self.CARD)
        hdr_row.pack(fill="x", pady=(0, 8))
        tk.Label(hdr_row, text="⚡ 性能监控与全方案测速中心", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9, "bold")).pack(side="left")

        self.btn_run_bench = tk.Button(
            hdr_row, text="⚡ 一键全方案性能跑分 (5090 vs Groq vs CPU)",
            command=self._action_run_benchmark,
            bg=self.SURFACE, fg=self.ACCENT, bd=0, font=(self.FONT, 9, "bold"),
            padx=12, pady=4, cursor="hand2", highlightbackground=self.BORDER, highlightthickness=1
        )
        self.btn_run_bench.pack(side="right")

        # Real-time Status Chips
        chips_frame = tk.Frame(card, bg=self.SURFACE, padx=10, pady=8, highlightbackground=self.BORDER, highlightthickness=1)
        chips_frame.pack(fill="x", pady=(0, 8))

        self.lbl_chip_engine = tk.Label(chips_frame, text="主调引擎: 加载中...", bg=self.SURFACE, fg=self.TEXT, font=(self.FONT, 9, "bold"))
        self.lbl_chip_engine.pack(side="left", padx=(0, 16))

        self.lbl_chip_latency = tk.Label(chips_frame, text="最新转写耗时: 19.3 ms", bg=self.SURFACE, fg="#047857", font=(self.FONT, 9, "bold"))
        self.lbl_chip_latency.pack(side="left", padx=(0, 16))

        self.lbl_chip_speedup = tk.Label(chips_frame, text="实时吞吐加速: 51.8x 极速", bg=self.SURFACE, fg=self.ACCENT, font=(self.FONT, 9, "bold"))
        self.lbl_chip_speedup.pack(side="left")

        # Benchmark Comparison Table Frame
        self.bench_display_frame = tk.Frame(card, bg=self.CARD)
        self.bench_display_frame.pack(fill="x", pady=(0, 6))

        # Initial benchmark placeholder table (refreshed by the run button).
        # NOTE: the local CUDA rows were removed with the faster-whisper engine; only
        # the three cloud slots and the CPU SenseVoice slot remain.
        self._render_benchmark_table({
            "groq_l3": {"engine": "Groq · whisper-large-v3 (Cloud)", "latency_ms": 0.0, "speedup": 0.0, "status": "🔵 最准 · 默认主力"},
            "groq_turbo": {"engine": "Groq · large-v3-turbo (Cloud)", "latency_ms": 0.0, "speedup": 0.0, "status": "⚡ 最快 · 云端"},
            "mimo": {"engine": "MiMo-V2.5-ASR (Cloud)", "latency_ms": 0.0, "speedup": 0.0, "status": "🔵 免费额度 · 兜底"},
            "sensevoice_cpu": {"engine": "SenseVoice (CPU)", "latency_ms": 0.0, "speedup": 0.0, "status": "⚪ 离线兜底 · RTF≈0.04"},
        })

        # History log frame
        # C1: wrap="word" — mono log lines used to clip at the right edge with wrap="none".
        tk.Label(card, text="最近转写性能记录 (最近 5 次):", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 8, "bold")).pack(anchor="w", pady=(6, 2))
        self.txt_perf_history = tk.Text(card, height=4, bg=self.SURFACE, fg=self.TEXT, bd=0, wrap="word",
                                        font=("Consolas", 8), highlightthickness=1, highlightbackground=self.BORDER)
        self.txt_perf_history.pack(fill="both", expand=True)

        # B1: engine health. The log above shows ONE take at a time; this aggregates by
        # engine slot so silent degradation — "mimo has been failing with 429 for three
        # days" — is finally visible instead of living only in vox.log.
        tk.Label(card, text="引擎健康 (按槽位聚合 · 含失败):", bg=self.CARD, fg=self.MUTED,
                 font=(self.FONT, 8, "bold")).pack(anchor="w", pady=(10, 2))
        self.txt_engine_health = tk.Text(card, height=5, bg=self.SURFACE, fg=self.TEXT, bd=0,
                                         wrap="none", font=("Consolas", 8),
                                         highlightthickness=1, highlightbackground=self.BORDER)
        self.txt_engine_health.pack(fill="both", expand=True)

    def _action_run_benchmark(self):
        self.btn_run_bench.config(text="⏳ 正在对各方案进行实时推演跑分中...", state="disabled")

        def worker():
            try:
                results = core.benchmark_all_engines()
            except Exception:
                results = {}

            def finish():
                self.btn_run_bench.config(text="⚡ 一键全方案性能跑分 (5090 vs Groq vs CPU)", state="normal")
                if results:
                    self._render_benchmark_table(results)
                    self._refresh_perf_display()

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _render_benchmark_table(self, results):
        for widget in self.bench_display_frame.winfo_children():
            widget.destroy()

        h_row = tk.Frame(self.bench_display_frame, bg=self.NEUTRAL, padx=6, pady=4)
        h_row.pack(fill="x", pady=(0, 2))
        tk.Label(h_row, text="方案 / 架构", width=24, anchor="w", bg=self.NEUTRAL, font=(self.FONT, 8, "bold")).pack(side="left")
        tk.Label(h_row, text="推理延迟", width=12, anchor="center", bg=self.NEUTRAL, font=(self.FONT, 8, "bold")).pack(side="left")
        tk.Label(h_row, text="实时加速比", width=12, anchor="center", bg=self.NEUTRAL, font=(self.FONT, 8, "bold")).pack(side="left")
        tk.Label(h_row, text="状态与评估建议", anchor="w", bg=self.NEUTRAL, font=(self.FONT, 8, "bold")).pack(side="left", fill="x", expand=True, padx=(6, 0))

        for k, v in results.items():
            r = tk.Frame(self.bench_display_frame, bg=self.CARD, padx=6, pady=3, highlightbackground=self.BORDER, highlightthickness=1)
            r.pack(fill="x", pady=1)

            lat = f"{v['latency_ms']:.1f} ms" if v.get("latency_ms") else "不可用"
            sp = f"{v['speedup']:.1f}x" if v.get("speedup") else "-"
            st = v.get("status", "")

            eng_name = v.get("engine", k)
            fg_color = "#047857" if "5090" in eng_name or "cuda" in k else self.TEXT

            tk.Label(r, text=eng_name, width=24, anchor="w", bg=self.CARD, fg=fg_color, font=(self.FONT, 8, "bold")).pack(side="left")
            tk.Label(r, text=lat, width=12, anchor="center", bg=self.CARD, fg=fg_color, font=(self.FONT, 8)).pack(side="left")
            tk.Label(r, text=sp, width=12, anchor="center", bg=self.CARD, fg=fg_color, font=(self.FONT, 8)).pack(side="left")
            # C1: DPI-aware wraplength keeps long status text from clipping at the right edge.
            tk.Label(r, text=st, bg=self.CARD, fg=self.MUTED, font=(self.FONT, 8),
                     anchor="w", justify="left",
                     wraplength=int(150 * self.dpi_scale)).pack(side="left", padx=(6, 0))

    def _refresh_perf_display(self):
        # Skip the rebuild when the perf file has not changed. This ran every 2s and
        # unconditionally deleted + reinserted the whole Text widget, which forced a
        # full re-layout of the panel even when the content was byte-identical.
        stamp = None
        try:
            stamp = os.path.getmtime(core.PERF_HISTORY_FILE)
        except Exception:
            pass
        if stamp is not None and stamp == getattr(self, "_perf_stamp", None):
            return
        self._perf_stamp = stamp

        records = core.safe_read_json(core.PERF_HISTORY_FILE, [])
        if records:
            last = records[-1]
            eng = last.get("engine", "未知")
            lat_ms = last.get("latency_ms", 0.0)
            sp = last.get("speedup", 1.0)
            self.lbl_chip_engine.config(text=f"主调引擎: {eng}")
            self.lbl_chip_latency.config(text=f"最新转写耗时: {lat_ms:.1f} ms")
            self.lbl_chip_speedup.config(text=f"实时吞吐加速: {sp:.1f}x 极速")

            self.txt_perf_history.delete("1.0", "end")
            lines = []
            for item in reversed(records[-5:]):
                t_str = item.get("time", "")[-8:]
                eng_name = item.get("engine", "")
                # F9/G5: per-model tracking — show the concrete model + chars + confidence.
                model = item.get("model", "")
                chars = item.get("chars", 0)
                conf = item.get("confidence")
                conf_s = f" | 置信:{conf:.2f}" if isinstance(conf, (int, float)) else ""
                lat = item.get("latency_ms", 0)
                dur = item.get("audio_len_s", 0)
                speed = item.get("speedup", 0)
                prev = item.get("text_preview", "")
                lines.append(
                    f"[{t_str}] {eng_name:<10} {model:<22} | {chars:>4}字 | "
                    f"耗时:{lat:>5.1f}ms | 语长:{dur:>4.1f}s | 加速:{speed:>4.1f}x{conf_s} | 预览:{prev}")
            self.txt_perf_history.insert("1.0", "\n".join(lines))

        self._refresh_engine_health()

    def _refresh_engine_health(self):
        """Render the B1 aggregate: per-slot success rate, P50/P95 and dominant error.

        Deliberately derived from the same file as _refresh_perf_display, so the
        existing "skip when the file has not changed" optimisation covers both.
        """
        if not hasattr(self, "txt_engine_health"):
            return
        try:
            health = core.engine_health(limit=200)
            lines = []
            for slot, s in sorted(health.items(),
                                  key=lambda kv: (kv[1].get("ok_rate", 0.0),
                                                  -kv[1].get("n", 0))):
                p50 = f"{s['p50_ms']:.0f}" if s.get("p50_ms") is not None else "-"
                p95 = f"{s['p95_ms']:.0f}" if s.get("p95_ms") is not None else "-"
                lines.append(
                    f"{slot[:16]:<16} {s.get('ok_rate', 0.0) * 100:5.1f}%  "
                    f"{p50:>6} {p95:>6}  {s.get('ok', 0):>4}/{s.get('n', 0):<4} "
                    f"{s.get('top_error') or '-'}")
            header = (f"{'引擎槽':<16} {'成功':>6}  {'P50ms':>6} {'P95ms':>6}  "
                      f"{'OK/N':>9} 主要错误")
            body = "\n".join(lines) if lines else "(暂无记录 — 说一句话后这里会出现)"
            self.txt_engine_health.delete("1.0", "end")
            self.txt_engine_health.insert("1.0", header + "\n" + body)
        except Exception:
            pass

    def _reset_overlay_pos(self):
        ov = self.cfg.setdefault("overlay", {})
        ov["custom_x"] = None
        ov["custom_y"] = None
        core.ConfigManager.save(self.cfg)
        messagebox.showinfo("悬浮窗复位", "已清除自定义拖动位置！\n灵动岛药丸窗已复位至屏幕正中下方（任务栏正上方）。\n（双击药丸窗亦可随时一秒复位居中）")

    # ── G5: boot autostart (user Startup folder, no admin) ───────────
    @staticmethod
    def _startup_bat_path():
        startup = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        return os.path.join(startup, "Vox Daemon.bat")

    def _apply_auto_start(self, enabled):
        try:
            if enabled:
                os.makedirs(os.path.dirname(self._startup_bat_path()), exist_ok=True)
                python = sys.executable
                daemon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vox_daemon.py")
                with open(self._startup_bat_path(), "w", encoding="utf-8") as f:
                    f.write("@echo off\r\n")
                    f.write(f'start "" "{python}" "{daemon}"\r\n')
            else:
                if os.path.exists(self._startup_bat_path()):
                    os.remove(self._startup_bat_path())
        except Exception as e:
            messagebox.showwarning("开机自启", f"写入启动项失败: {e}")

    def _toggle_auto_start(self):
        enabled = self.v_auto_start.get()
        self._apply_auto_start(enabled)
        try:
            self.cfg.setdefault("system", {})["auto_start"] = enabled
            core.ConfigManager.save(self.cfg)
        except Exception:
            pass

    # ── G5: config backup/restore ──────────────────────────────────
    def _export_config(self):
        try:
            import filedialog
            path = filedialog.asksaveasfilename(
                title="导出 Vox 配置", defaultextension=".json",
                initialfile="vox_config_backup.json",
                filetypes=[("JSON", "*.json")])
            if not path:
                return
            core.ConfigManager.save(self.cfg)
            with open(core.CONFIG_PATH, "r", encoding="utf-8") as src, \
                 open(path, "w", encoding="utf-8") as dst:
                dst.write(src.read())
            messagebox.showinfo("配置已导出", f"已导出到:\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _import_config(self):
        try:
            import filedialog
            path = filedialog.askopenfilename(
                title="导入 Vox 配置", filetypes=[("JSON", "*.json")])
            if not path:
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("配置文件格式不正确")
            if not messagebox.askyesno("确认导入", "导入将覆盖当前全部设置（含 API Key 与引擎链）。\n继续?"):
                return
            core.ConfigManager.save(data)
            self.cfg = core.ConfigManager.load()
            self._load_config_to_ui()
            self.save_and_apply_all()
            messagebox.showinfo("配置已导入", "已导入并实时应用。")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def _build_appearance_card(self, col):
        # E1: user-adjustable bubble appearance. All values live under cfg["overlay"]
        # (ui.* sub-keys + base_width/base_height); the standalone overlay hot-applies
        # them on the next config.json change (no restart, layout math stays consistent
        # because font/line sizes both derive from the same design units).
        card = self._card(col, fill="x", pady=(0, 12))
        self._title(card, "气泡外观 · 字号 / 行距 / 大小 / 透明度")

        ui = self.cfg.setdefault("overlay", {}).setdefault("ui", {})
        self.v_ui_font_scale = tk.DoubleVar(value=ui.get("font_scale", 1.0))
        self.v_ui_line_scale = tk.DoubleVar(value=ui.get("line_scale", 1.0))
        self.v_ui_opacity = tk.DoubleVar(value=ui.get("opacity", 85))
        ov = self.cfg.setdefault("overlay", {})
        self.v_ui_base_w = tk.IntVar(value=ov.get("base_width", 540))
        self.v_ui_base_h = tk.IntVar(value=ov.get("base_height", 66))

        # F1: two-line rows — label on its own line above the slider, so long labels
        # ("不透明度…"/"气泡宽度…") no longer truncate or crowd the slider.
        def _slider_row(label, var, lo, hi, step, fmt, tip):
            block = tk.Frame(card, bg=self.CARD)
            block.pack(fill="x", pady=(0, 10))
            top = tk.Frame(block, bg=self.CARD)
            top.pack(fill="x")
            tk.Label(top, text=label, bg=self.CARD, fg=self.TEXT, font=(self.FONT, 9, "bold")).pack(side="left")
            lbl = tk.Label(top, text="", bg=self.CARD, fg=self.ACCENT, font=(self.FONT, 9, "bold"))
            lbl.pack(side="right")
            def _upd(*_):
                try:
                    lbl.config(text=(tip % fmt) % float(var.get()))
                except Exception:
                    pass
            tk.Scale(block, from_=lo, to=hi, orient="horizontal", resolution=step,
                     variable=var, command=_upd, showvalue=False, bg=self.CARD,
                     highlightthickness=0, length=420).pack(anchor="w", pady=(4, 0))
            _upd()

        _slider_row("气泡字号", self.v_ui_font_scale, 0.6, 2.0, 0.05, "%.2fx", "字号 %s")
        _slider_row("行距", self.v_ui_line_scale, 0.7, 2.0, 0.05, "%.2fx", "行距 %s")
        _slider_row("不透明度（越高越不透明）", self.v_ui_opacity, 40, 100, 5, "%.0f%%", "不透明度 %s")
        _slider_row("气泡宽度（逻辑像素）", self.v_ui_base_w, 420, 760, 10, "%dpx", "宽度 %s")
        _slider_row("气泡高度（逻辑像素）", self.v_ui_base_h, 44, 90, 2, "%dpx", "高度 %s")

        # F6: done-state display durations (气泡停留多久消失)
        self.v_ui_done_short = tk.IntVar(value=ui.get("done_short_ms", 2000))
        self.v_ui_done_long = tk.IntVar(value=ui.get("done_long_ms", 5000))
        _slider_row("短文本停留时长（毫秒）", self.v_ui_done_short, 500, 10000, 500, "%dms", "短文本 %s")
        _slider_row("长文本停留时长（毫秒）", self.v_ui_done_long, 2000, 30000, 500, "%dms", "长文本 %s")

        # F5: live state preview — open a real overlay window in the chosen state with
        # the CURRENT settings so the user sees exactly what they are tuning.
        pv_row = tk.Frame(card, bg=self.CARD)
        pv_row.pack(fill="x", pady=(6, 4))
        tk.Label(pv_row, text="实时预览:", bg=self.CARD, fg=self.TEXT, font=(self.FONT, 9, "bold")).pack(side="left")
        for text, state in (("预览录音态", "recording"), ("预览完成态", "done"), ("预览长文态", "done_long")):
            tk.Button(pv_row, text=text, command=lambda s=state: self._preview_overlay_state(s),
                      bg=self.SURFACE, fg=self.ACCENT, bd=0, font=(self.FONT, 8, "bold"),
                      padx=10, pady=3, cursor="hand2",
                      highlightbackground=self.BORDER, highlightthickness=1).pack(side="left", padx=(10, 0))
        tk.Label(card, text="💡 保存后即时生效（悬浮窗自动热更新，无需重启）。预览窗可拖动，按 Esc 关闭；长文态可无限增高，不再截断。",
                 bg=self.CARD, fg=self.MUTED, font=(self.FONT, 8)).pack(anchor="w", pady=(4, 0))

    def _preview_overlay_state(self, state):
        """F5: open a throwaway overlay window in the requested state with the CURRENT
        appearance settings, so the user can visually tune the bubble (Esc to close)."""
        try:
            import vox_overlay as ov
            # Save first so the preview reflects the slider values as typed? No — the
            # preview uses the config on disk; the user clicks after saving. To make the
            # preview feel live, apply the in-memory vars to a temp cfg overlay dict.
            o = ov.VoxOverlay()
            o.ui_font_scale = max(0.6, min(2.0, float(self.v_ui_font_scale.get())))
            o.ui_line_scale = max(0.7, min(2.0, float(self.v_ui_line_scale.get())))
            o.ui_opacity = max(40, min(100, int(self.v_ui_opacity.get())))
            o.base_w = int(self.v_ui_base_w.get())
            o.base_h = int(self.v_ui_base_h.get())
            o.width = int(round(o.base_w * o.scale))
            o.height = int(round(o.base_h * o.scale))
            o.max_content_h = max(o.height * 3, int(core.get_screen_workarea()[3] * 0.62))
            o._resize_to(o.height)
            o.engine = "groq (whisper-large-v3-turbo)"
            if state == "recording":
                o.start_time = time.time()
                o.state = "recording"
                o.show_recording(hint="口述中（预览）", start_time=o.start_time,
                                 preview="边说边出字：这是录音态的实时预览效果")
                o.level = 0.55
                o._draw_pill()
            elif state == "done":
                o.text = "转写成功，完成态预览：这是最终输出的文字"
                o.state = "done"
                o._fit_content()
            else:  # done_long
                o.text = ("这是一段比较长的完成态预览文本，用来观察气泡无限增高的效果。"
                          "长段落不会再被截断，气泡会一直长到能完整显示为止，"
                          "同时圆角也会随高度保持平滑，不会出现内圈直角边。" * 3)
                o.state = "done"
                o._fit_content()
            o.show()
            o.root.lift()
            o.root.bind("<Escape>", lambda e: o.root.destroy())
            # Auto-close after 60s so a forgotten preview doesn't linger.
            o.root.after(60000, lambda: o.root.destroy() if o.root.winfo_exists() else None)
        except Exception as e:
            messagebox.showerror("预览失败", str(e))

    def _test_groq_models(self):
        """F8: transcribe a built-in sample with each available Groq model and report
        latency + result, so the user can pick based on measured speed/accuracy."""
        import threading
        key = self.v_groq_key.get().strip() or self.cfg.get("engines", {}).get("groq", {}).get("api_key", "")
        if not key:
            messagebox.showwarning("缺少密钥", "请先填写 Groq API Key 再测试。")
            return
        sample = core.SCRIPT_DIR and os.path.join(core.SCRIPT_DIR, "tts_test.wav.flac")
        if not os.path.exists(sample):
            sample = os.path.join(core.SCRIPT_DIR, "tts_test.wav")
        if not os.path.exists(sample):
            messagebox.showwarning("缺少样音", "未找到测试样音 tts_test.wav(.flac)。")
            return

        def _run():
            rows = []
            for model in ("whisper-large-v3-turbo", "whisper-large-v3"):
                try:
                    eng = core.GroqEngine(key, model, cfg=self.cfg)
                    text, lat, _cost = eng.transcribe(sample, language="zh")
                    rows.append(f"{model}\n  耗时: {lat:.2f}s · {len(text)}字\n  结果: {text[:48]}")
                except Exception as e:
                    rows.append(f"{model}\n  失败: {str(e)[:60]}")
            def _show():
                messagebox.showinfo("Groq 模型对比测试", "\n\n".join(rows))
            try:
                self._root.after(0, _show) if hasattr(self, "_root") else messagebox.showinfo("Groq 模型对比测试", "\n\n".join(rows))
            except Exception:
                messagebox.showinfo("Groq 模型对比测试", "\n\n".join(rows))
        threading.Thread(target=_run, daemon=True).start()

    def _build_engine_card(self, col):
        card = self._card(col, fill="x", pady=(0, 12))
        self._title(card, "硬件加速引擎与流式预览设置")

        # Hardware Info Banner (User Request: 5090 加速检测)
        is_cuda, cuda_info = core.get_cuda_device_info()
        hw_frame = tk.Frame(card, bg=self.SURFACE, padx=12, pady=10, highlightbackground=self.BORDER, highlightthickness=1)
        hw_frame.pack(fill="x", pady=(0, 10))

        hw_left = tk.Frame(hw_frame, bg=self.SURFACE)
        hw_left.pack(side="left", fill="both", expand=True)

        hw_badge_fg = "#047857" if is_cuda else "#334155"
        self.lbl_hw_info = tk.Label(hw_left, text=f"🚀 硬件就绪: {cuda_info}", bg=self.SURFACE, fg=hw_badge_fg, font=(self.FONT, 9, "bold"))
        self.lbl_hw_info.pack(anchor="w")

        self.lbl_vram_info = tk.Label(hw_left, text="显存检测中...", bg=self.SURFACE, fg=self.MUTED, font=(self.FONT, 8))
        self.lbl_vram_info.pack(anchor="w", pady=(3, 0))

        self.btn_offload_gpu = tk.Button(
            hw_frame, text="🧹 退出本地 GPU 引擎 (释放显存)", command=self._action_offload_gpu,
            bg=self.WARN_BG, fg="#92400e", bd=0, font=(self.FONT, 9, "bold"),
            padx=12, pady=6, cursor="hand2", highlightbackground="#f59e0b", highlightthickness=1
        )
        self.btn_offload_gpu.pack(side="right", padx=(8, 0))

        # BUG-4: the faster-whisper "local" engine was removed from the codebase.
        # Its model-size picker was dead UI (the choice was written back to a key
        # that ConfigManager pruned on every load). Local ASR is configured only
        # through the SenseVoice fallback slots below.

        # G5: boot autostart — user Startup folder shortcut (no admin needed),
        # so Vox is alive before the first dictation of the day.
        sa_row = tk.Frame(card, bg=self.CARD)
        sa_row.pack(fill="x", pady=(0, 8))
        self.v_auto_start = tk.BooleanVar(
            value=bool(self.cfg.get("system", {}).get("auto_start", False)))
        ttk.Checkbutton(sa_row, text="开机自动启动守护进程 (写入用户启动文件夹, 无需管理员)",
                        variable=self.v_auto_start, command=self._toggle_auto_start).pack(side="left")

        # G1: explicit ordered fallback chain — 3 slots, each an engine the user
        # picks from: cloud models (2) + local GPU + local CPU. Fallback 1 is tried
        # first; on failure the chain walks 2, 3.
        self.SLOT_TO_SPEC = {
            "": None,
            "Groq · L3 Turbo (快速/省)": "groq-turbo",
            "Groq · L3 (最准)": "groq-l3",
            "本地 · SenseVoice · 5090 (中文更强·自带标点)": "sensevoice-cuda",
            "本地 · SenseVoice · CPU": "sensevoice-cpu",
            "MiMo 云端 · mimo-v2.5-asr (中英混·方言·抗噪·30M/小时额度)": "mimo",
        }
        self.SPEC_TO_SLOT = {v: k for k, v in self.SLOT_TO_SPEC.items() if v}
        p_frame = tk.Frame(card, bg=self.CARD)
        p_frame.pack(fill="x", pady=(0, 8))
        tk.Label(p_frame, text="Fallback 引擎链 (按顺序尝试, 前一个失败才轮到下一个):",
                 bg=self.CARD, fg=self.TEXT, font=(self.FONT, 9, "bold")).pack(anchor="w", pady=(0, 4))
        for i in (1, 2, 3):
            row = tk.Frame(p_frame, bg=self.CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"Fallback {i}:", bg=self.CARD, fg=self.MUTED,
                     font=(self.FONT, 9)).pack(side="left")
            var = tk.StringVar(value="Groq · L3 Turbo (快速/省)" if i == 1 else "")
            setattr(self, f"v_fb{i}", var)
            ttk.Combobox(row, textvariable=var, state="readonly", width=30,
                         values=list(self.SLOT_TO_SPEC.keys())).pack(side="left", padx=(8, 0))

        # Groq Key
        g_row = tk.Frame(card, bg=self.CARD)
        g_row.pack(fill="x", pady=(4, 8))
        tk.Label(g_row, text="Groq API Key (云端引擎密钥):", bg=self.CARD, fg=self.TEXT, font=(self.FONT, 9, "bold")).pack(anchor="w", pady=(0, 4))
        self.v_groq_key = tk.StringVar()
        self.entry_groq = ttk.Entry(g_row, textvariable=self.v_groq_key, show="*")
        self.entry_groq.pack(fill="x")

        # MiMo Key (Xiaomi coding-plan 免费 mimo-v2.5-asr 额度)
        mm_row = tk.Frame(card, bg=self.CARD)
        mm_row.pack(fill="x", pady=(0, 8))
        tk.Label(mm_row, text="MiMo API Key (小米 MiMo 平台密钥, coding-plan 免费额度):",
                 bg=self.CARD, fg=self.TEXT, font=(self.FONT, 9, "bold")).pack(anchor="w", pady=(0, 4))
        self.v_mimo_key = tk.StringVar()
        self.entry_mimo = ttk.Entry(mm_row, textvariable=self.v_mimo_key, show="*")
        self.entry_mimo.pack(fill="x")
        tk.Label(mm_row, text="Token Plan 专属端点已内置 · ASR 额度 30M/小时 · 谷价 8 折 (北京 00:00-08:00) · TTS 三模型限时免费",
                 bg=self.CARD, fg=self.MUTED, font=(self.FONT, 8), anchor="w").pack(fill="x", pady=(2, 0))

        # F8/G1: Groq model compare — the slots above pick the live model; this button
        # benchmarks both on the built-in sample so the user can decide which to put in
        # Fallback 1.
        m_row = tk.Frame(card, bg=self.CARD)
        m_row.pack(fill="x", pady=(0, 8))
        tk.Button(m_row, text="⚡ 测试对比云端模型 (L3 Turbo vs L3)", command=self._test_groq_models,
                  bg=self.SURFACE, fg=self.ACCENT, bd=0, font=(self.FONT, 8, "bold"),
                  padx=10, pady=3, cursor="hand2",
                  highlightbackground=self.BORDER, highlightthickness=1).pack(side="left")
        tk.Label(m_row, text="用内置样音实测两个 Groq 模型的耗时与转写结果, 帮你决定 Fallback 槽位放哪个",
                 bg=self.CARD, fg=self.MUTED, font=(self.FONT, 8)).pack(side="left", padx=(10, 0))

        # Streaming Interim Preview (User Request: 流式传输显字)
        st_frame = tk.Frame(card, bg=self.CARD)
        st_frame.pack(fill="x", pady=(0, 8))
        self.v_streaming_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(st_frame, text="开启胶囊悬浮窗流式显字 (边录音边实时出字预览)", variable=self.v_streaming_enabled).pack(side="left")
        tk.Label(st_frame, text="刷新间隔:", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9)).pack(side="left", padx=(16, 4))
        self.v_streaming_interval = tk.StringVar(value="500")
        ttk.Combobox(st_frame, textvariable=self.v_streaming_interval, values=["350", "500", "800", "1200", "1800"], state="readonly", width=7).pack(side="left")
        tk.Label(st_frame, text="毫秒 (推荐 500ms)", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 8)).pack(side="left", padx=(4, 0))

        # Draggable Dynamic Island Overlay Controls (User Request: 悬浮窗可拖动与中下部复位)
        ov_frame = tk.Frame(card, bg=self.CARD)
        ov_frame.pack(fill="x", pady=(0, 8))
        tk.Label(ov_frame, text="灵动岛药丸悬浮窗: ", bg=self.CARD, fg=self.TEXT, font=(self.FONT, 9, "bold")).pack(side="left")
        tk.Label(ov_frame, text="已设为大屏自适应 · 默认吸附屏幕正中下方 · 支持鼠标任意拖拽", bg=self.CARD, fg=self.MUTED, font=(self.FONT, 9)).pack(side="left", padx=(4, 10))
        btn_reset_pos = tk.Button(
            ov_frame, text="↺ 复位药丸窗至屏幕中下方", command=self._reset_overlay_pos,
            bg=self.SURFACE, fg=self.ACCENT, bd=0, font=(self.FONT, 8, "bold"), padx=10, pady=3,
            cursor="hand2", highlightbackground=self.BORDER, highlightthickness=1
        )
        btn_reset_pos.pack(side="left")

        # Language Dropdown (User Request: 中英文混合输入)
        l_row = tk.Frame(card, bg=self.CARD)
        l_row.pack(fill="x", pady=(0, 8))
        tk.Label(l_row, text="口述语言: ", bg=self.CARD, fg=self.TEXT, font=(self.FONT, 9, "bold")).pack(side="left")
        self.v_lang = tk.StringVar(value="zh (中文 + 英文专业术语混排 - 推荐)")
        self.cbo_lang = ttk.Combobox(l_row, textvariable=self.v_lang,
                                     values=["zh (中文 + 英文专业术语混排 - 推荐)", "auto (多国语言自动识别)"],
                                     state="readonly", width=34)
        self.cbo_lang.pack(side="left", padx=6)

        # Output method & Switches
        s_row = tk.Frame(card, bg=self.CARD)
        s_row.pack(fill="x", pady=(0, 8))

        self.v_sound = tk.BooleanVar(value=True)
        ttk.Checkbutton(s_row, text="开启操作提示音 (微秒级水滴/升调音)", variable=self.v_sound).pack(side="left", padx=(0, 16))

        self.v_restore_clip = tk.BooleanVar(value=True)
        ttk.Checkbutton(s_row, text="保护剪贴板 (粘贴后自动恢复原内容)", variable=self.v_restore_clip).pack(side="left")

        # Output Method Dropdown
        o_row = tk.Frame(card, bg=self.CARD)
        o_row.pack(fill="x")
        tk.Label(o_row, text="输出方式: ", bg=self.CARD, fg=self.TEXT, font=(self.FONT, 9, "bold")).pack(side="left")
        self.v_output_method = tk.StringVar(value="smart_paste")
        ttk.Combobox(o_row, textvariable=self.v_output_method, values=["smart_paste", "clipboard_only", "auto"], state="readonly", width=18).pack(side="left", padx=6)

    # ── Config Binding ────────────────────────────────────────────
    def _load_config_to_ui(self):
        c = self.cfg
        # Hotkey
        hk = c.get("hotkey", {})
        mods = [m.lower() for m in hk.get("modifiers", ["shift"])]
        self.v_ctrl.set("control" in mods or "ctrl" in mods)
        self.v_alt.set("alt" in mods)
        self.v_shift.set("shift" in mods)
        self.v_win.set("win" in mods)
        self.v_key.set(hk.get("key", ""))
        self.v_mode.set(hk.get("mode", "adaptive"))
        thresh = int(hk.get("hold_threshold_ms", 350))
        self.v_hold_thresh.set(thresh)
        self.lbl_thresh_val.config(text=f"{thresh} 毫秒 ({thresh/1000:.2f}秒)")
        self._update_hotkey_preview()

        # G1: engine fallback slots (max 3; legacy bare specs resolve to a slot label)
        ep = c.get("engine_priority", ["groq-turbo", "groq-l3"])
        for i in (1, 2, 3):
            var = getattr(self, f"v_fb{i}")
            slot = self.SPEC_TO_SLOT.get(ep[i - 1], "") if i - 1 < len(ep) else ""
            var.set(slot)
        # The removed faster-whisper "local" engine has no UI state any more.

        # Streaming
        st = c.get("streaming", {})
        self.v_streaming_enabled.set(st.get("enabled", True))
        self.v_streaming_interval.set(str(st.get("interval_ms", 500)))

        # Fidelity & Tone Style (User Request: 语气保留与避免过度介入)
        fid = c.get("fidelity", {})
        self.v_fidelity_mode.set(fid.get("mode", "verbatim"))
        self.v_auto_period.set(fid.get("auto_period", False))

        # Groq key
        self.v_groq_key.set(c.get("engines", {}).get("groq", {}).get("api_key", ""))
        # MiMo key (Xiaomi MiMo-V2.5-ASR, coding-plan free quota)
        mimo_key = c.get("engines", {}).get("mimo", {}).get("api_key", "") or os.environ.get("VOX_MIMO_KEY", "") or os.environ.get("MIMO_API_KEY", "")
        self.v_mimo_key.set(mimo_key)

        # Prompt
        p = c.get("whisper_prompt")
        if p is None or p == "":
            p = DEFAULT_DEV_PROMPT
        self.txt_prompt.delete("1.0", "end")
        self.txt_prompt.insert("1.0", p)

        # Language
        lang = str(c.get("language") or c.get("engines", {}).get("groq", {}).get("language", "zh")).lower()
        if "auto" in lang:
            self.v_lang.set("auto (多国语言自动识别)")
        else:
            self.v_lang.set("zh (中文 + 英文专业术语混排 - 推荐)")

        # Switches
        self.v_sound.set(c.get("sound_effects", True))
        self.v_restore_clip.set(c.get("restore_clipboard", True))
        self.v_output_method.set(c.get("output_method", "smart_paste"))

        # Populate Mics
        self._refresh_mic_list()

    def _refresh_mic_list(self):
        devs = self.pipeline.list_input_devices(force_refresh=True)
        options = ["(自动 — 智能匹配有效麦克风)"]
        for d in devs:
            options.append(f"[{d['index']}] {d['friendly_name']}")
        self.cbo_mic["values"] = options

        pref = self.cfg.get("audio", {}).get("preferred_device")
        chosen = options[0]
        if pref is not None:
            pref_str = str(pref).lower()
            for opt in options:
                if pref_str in opt.lower():
                    chosen = opt
                    break
        self.cbo_mic_var.set(chosen)

    def _apply_preset(self, mods, key):
        self.v_ctrl.set("control" in mods or "ctrl" in mods)
        self.v_alt.set("alt" in mods)
        self.v_shift.set("shift" in mods)
        self.v_win.set("win" in mods)
        self.v_key.set(key)
        self._update_hotkey_preview()
        self.lbl_record_status.config(text="已应用推荐快捷键预设", fg=self.MUTED)

    def _update_hotkey_preview(self):
        tokens = []
        if self.v_ctrl.get(): tokens.append("Ctrl")
        if self.v_alt.get(): tokens.append("Alt")
        if self.v_shift.get(): tokens.append("Shift")
        if self.v_win.get(): tokens.append("Win")
        k = self.v_key.get().strip().upper()
        if k: tokens.append(k)
        text = "+".join(tokens) if tokens else "未设置"
        self.lbl_hotkey_preview.config(text=f"当前热键: {text}")

    # ── Actions ───────────────────────────────────────────────────
    def save_and_apply_all(self):
        tokens = []
        mods = []
        if self.v_ctrl.get(): mods.append("control"); tokens.append("Ctrl")
        if self.v_alt.get(): mods.append("alt"); tokens.append("Alt")
        if self.v_shift.get(): mods.append("shift"); tokens.append("Shift")
        if self.v_win.get(): mods.append("win"); tokens.append("Win")
        key = self.v_key.get().strip().lower()
        if key: tokens.append(key)
        if not mods and not key:
            messagebox.showwarning("提示", "热键至少需要选中一个修饰键或按键")
            return

        combo_str = "+".join(mods)
        if key:
            combo_str = f"{combo_str}+{key}" if combo_str else key

        self.cfg["hotkey"] = {
            "combo": combo_str,
            "modifiers": mods,
            "key": key,
            "mode": self.v_mode.get(),
            "hold_threshold_ms": int(self.v_hold_thresh.get())
        }

        # G1: engine fallback chain from the 3 explicit slots (drop empties).
        specs = []
        for i in (1, 2, 3):
            spec = self.SLOT_TO_SPEC.get(getattr(self, f"v_fb{i}").get())
            if spec:
                specs.append(spec)
        self.cfg["engine_priority"] = specs or ["groq-turbo", "groq-l3"]

        # E1/F6: bubble appearance (气泡外观 card) incl. done-state display durations
        try:
            ui = self.cfg.setdefault("overlay", {}).setdefault("ui", {})
            ui["font_scale"] = round(float(self.v_ui_font_scale.get()), 2)
            ui["line_scale"] = round(float(self.v_ui_line_scale.get()), 2)
            ui["opacity"] = int(self.v_ui_opacity.get())
            ui["done_short_ms"] = int(self.v_ui_done_short.get())
            ui["done_long_ms"] = int(self.v_ui_done_long.get())
            self.cfg["overlay"]["base_width"] = int(self.v_ui_base_w.get())
            self.cfg["overlay"]["base_height"] = int(self.v_ui_base_h.get())
        except Exception:
            pass

        # BUG-4 fix: the removed faster-whisper "local" engine used to be
        # re-created here on every save (ConfigManager pruned it on load, but
        # the dead key kept coming back in config.json). Do not write it.

        # Streaming Interim Preview — mutate ONLY the keys this UI owns.
        # Replacing the whole dict silently dropped `interim_engine`, so the
        # user's streaming-preview setting reverted to the default on every save.
        streaming = self.cfg.setdefault("streaming", {})
        streaming["enabled"] = self.v_streaming_enabled.get()
        streaming["interval_ms"] = int(self.v_streaming_interval.get())

        # Fidelity & Tone Style Control (User Request: 语气保留与避免过度干预)
        self.cfg["fidelity"] = {
            "mode": self.v_fidelity_mode.get(),
            "auto_period": self.v_auto_period.get()
        }

        self.cfg["sound_effects"] = self.v_sound.get()
        self.cfg["restore_clipboard"] = self.v_restore_clip.get()
        self.cfg["output_method"] = self.v_output_method.get()
        self.cfg["engines"]["groq"]["api_key"] = self.v_groq_key.get().strip()
        # MiMo key — setdefault so saving before a daemon reload keeps a full block
        # (model/base_url defaults come from DEFAULT_CONFIG merge on next load).
        _mimo = self.cfg.setdefault("engines", {}).setdefault("mimo", {})
        _mimo["api_key"] = self.v_mimo_key.get().strip()
        # G1: keep the configured groq model in sync with the first groq slot, so the
        # bare 'groq' legacy spec and the badge default resolve to what the user picked.
        try:
            first_groq = next(
                (self.SLOT_TO_SPEC.get(getattr(self, f"v_fb{i}").get())
                 for i in (1, 2, 3) if self.SLOT_TO_SPEC.get(getattr(self, f"v_fb{i}").get())
                 and self.SLOT_TO_SPEC.get(getattr(self, f"v_fb{i}").get()).startswith("groq")),
                None)
            if first_groq:
                self.cfg["engines"]["groq"]["model"] = {
                    "groq-turbo": "whisper-large-v3-turbo",
                    "groq-l3": "whisper-large-v3",
                }[first_groq]
        except Exception:
            pass

        # Language
        lang_val = "auto" if "auto" in self.v_lang.get().lower() else "zh"
        self.cfg["language"] = lang_val
        if "groq" in self.cfg.get("engines", {}):
            self.cfg["engines"]["groq"]["language"] = lang_val
        if "mimo" in self.cfg.get("engines", {}):
            # MiMo asr_options.language only accepts auto/zh/en — lang_val is auto|zh.
            self.cfg["engines"]["mimo"]["language"] = lang_val
        # (removed local engine — no language key to sync)

        # Prompt
        prompt_val = self.txt_prompt.get("1.0", "end").strip()
        self.cfg["whisper_prompt"] = prompt_val

        # Mic
        mic_val = self.cbo_mic_var.get()
        if mic_val.startswith("("):
            self.cfg["audio"]["preferred_device"] = None
        else:
            friendly = mic_val.split("]", 1)[-1].strip() if "]" in mic_val else mic_val
            self.cfg["audio"]["preferred_device"] = friendly

        core.ConfigManager.save(self.cfg)
        messagebox.showinfo("已保存", "配置已成功保存！\n守护进程会在约 1 秒内自动热重载并生效。")
        self._periodic_refresh()

    def quick_calibrate_mic(self):
        if self._calibrating:
            return
        self._calibrating = True
        self.btn_calib.config(text="⏳ 校准探测中(请说话)...", state="disabled")

        def calib_thread():
            devs = self.pipeline.list_input_devices(force_refresh=True)
            best_dev = None
            best_rms = -1.0

            # Test top candidates (WASAPI / DirectSound / USB)
            for d in devs[:8]:
                idx = d["index"]
                sr = d.get("native_sr", 16000)
                try:
                    import sounddevice as sd
                    data = sd.rec(int(sr * 0.4), samplerate=sr, channels=1, dtype="float32", device=idx, blocking=True)
                    rms = float(np.sqrt(np.mean(data ** 2)))
                    if rms > best_rms:
                        best_rms = rms
                        best_dev = d
                except Exception:
                    pass

            def finish():
                self._calibrating = False
                self.btn_calib.config(text="⚡ 一键校准最强麦克风", state="normal")
                if best_dev and best_rms > 0.0001:
                    match_str = f"[{best_dev['index']}] {best_dev['friendly_name']}"
                    self.cbo_mic_var.set(match_str)
                    self.lbl_mic_hint.config(text=f"✅ 自动锁定: {best_dev['friendly_name']} (RMS={best_rms:.5f})", fg=self.SUCCESS)
                else:
                    self.lbl_mic_hint.config(text="⚠️ 未检测到有效声音输入，请大声说话并重试", fg=self.REC)

            self.after(0, finish)

        threading.Thread(target=calib_thread, daemon=True).start()

    # ── Manual Record & Meter ─────────────────────────────────────
    def toggle_manual_rec(self):
        if not self._recording_manual:
            self._start_manual_rec()
        else:
            self._stop_manual_rec()

    def _start_manual_rec(self):
        self._recording_manual = True
        self.btn_rec.config(text="⏹️ 停止录音并转写", bg=self.REC_DARK)
        self.lbl_result_meta.config(text="正在录音中... 请说话")
        try:
            mic_val = self.cbo_mic_var.get()
            dev_idx = None
            if mic_val.startswith("["):
                dev_idx = int(mic_val.split("]")[0][1:])
            self.pipeline.start_recording(dev_idx)
        except Exception as e:
            self._recording_manual = False
            self.btn_rec.config(text="●  点击测试录音", bg=self.REC)
            messagebox.showerror("录音错误", f"无法打开麦克风: {e}")

    def _stop_manual_rec(self):
        self._recording_manual = False
        self.btn_rec.config(text="●  点击测试录音", bg=self.REC)
        self.lbl_result_meta.config(text="⏳ 正在转写中...")

        def transcribe_thread():
            audio = self.pipeline.stop_recording()
            if audio.size < 16000 * 0.1:
                self.after(0, lambda: self.lbl_result_meta.config(text="录音过短或未收到声音"))
                return

            wav = os.path.join(SCRIPT_DIR, "manual_test.wav")
            try:
                core.write_wav(wav, audio, 16000)
                flac_path, _ = core.to_flac_or_wav(wav, self.cfg["audio"].get("upload_format", "flac"))
                chain = core.EngineChain(self.cfg)
                # Use the configured language (B11), not a hardcoded "auto".
                lang = self.cfg.get("language") or self.cfg.get("engines", {}).get("groq", {}).get("language", "zh")
                res = chain.transcribe(flac_path, lang)
                clean = core.PostProcessor.process(res["text"], self.cfg)

                def show_res():
                    self.txt_result.delete("1.0", "end")
                    self.txt_result.insert("1.0", clean)
                    self.lbl_result_meta.config(text=f"[{res['engine']}] 延迟: {res['latency_s']}s · {len(clean)}字")

                self.after(0, show_res)
            except Exception as e:
                self.after(0, lambda err=e: self.lbl_result_meta.config(text=f"转写失败: {err}"))
            finally:
                if os.path.exists(wav):
                    try: os.unlink(wav)
                    except Exception: pass

        threading.Thread(target=transcribe_thread, daemon=True).start()

    def _tick_meter(self):
        if self._recording_manual:
            peak = 0.0
            with self.pipeline._lock:
                if self.pipeline.frames:
                    last_chunk = self.pipeline.frames[-1]
                    peak = float(np.max(np.abs(last_chunk)))
            val = min(1.0, peak * 3.5)
            self.meter_bar["value"] = val
            lvl_desc = "🔇微弱" if val < 0.1 else "🟢良好" if val < 0.8 else "🔴过强"
            self.meter_label.config(text=f"电平: {lvl_desc} ({peak:.3f})")
            self._meter_idle_written = False
            delay = self.METER_TICK_ACTIVE_MS
        else:
            # Idle: write the placeholder at most ONCE, then back off to 1 Hz.
            #
            # This used to reschedule itself every 80 ms forever and rewrite both
            # widgets on every tick — 12.5 wakeups/s and 12.5 widget writes/s while the
            # window just sat there. Measured on this machine: 24.9 s of CPU over 19
            # idle minutes for one settings window. Neither the value nor the label can
            # change while idle, so the writes were pure waste; 80 ms is kept for the
            # live level meter, where the cadence is actually earned.
            if not self._meter_idle_written:
                self.meter_bar["value"] = 0.0
                self.meter_label.config(text="电平: —")
                self._meter_idle_written = True
            delay = self.METER_TICK_IDLE_MS
        self.after(delay, self._tick_meter)

    # ── Daemon Controls & Refresh ─────────────────────────────────
    def _action_start(self):
        start_daemon()
        self.after(600, self._periodic_refresh)

    def _action_stop(self):
        stop_daemon()
        self.after(400, self._periodic_refresh)

    def _action_restart(self):
        stop_daemon()
        self.after(600, lambda: (start_daemon(), self.after(500, self._periodic_refresh)))

    def _periodic_refresh(self):
        # Surface the window if a launcher/tray asked for it (port-free wake).
        self._check_restore_request()
        pid = get_daemon_pid()
        if pid:
            self.status_badge.config(text=f"● 守护进程运行中 (PID: {pid})", bg="#dcfce7", fg="#15803d")
        else:
            self.status_badge.config(text="○ 守护进程未运行", bg="#fee2e2", fg="#b91c1c")

        # Read latest meta
        if os.path.exists(core.LATEST_META_FILE):
            meta = core.safe_read_json(core.LATEST_META_FILE, {})
            if meta.get("text") and not self._recording_manual:
                cur = self.txt_result.get("1.0", "end").strip()
                if not cur or cur != meta.get("text"):
                    self.txt_result.delete("1.0", "end")
                    self.txt_result.insert("1.0", meta.get("text", ""))
                    self.lbl_result_meta.config(text=f"[{meta.get('engine', '?')}] 延迟: {meta.get('latency_s', '?')}s · {meta.get('chars', 0)}字 · 粘贴: {'✓' if meta.get('pasted') else '✗'}")

        # Refresh performance metrics chips
        self._refresh_perf_display()
        # nvidia-smi is a heavyweight driver round-trip (~50-150ms of full process
        # spawn). Re-running it every 2 seconds was a classic source of visible
        # stutter while this window is open; every 30s is ample for a VRAM readout.
        self._vram_tick = getattr(self, "_vram_tick", 0) + 1
        if self._vram_tick % 15 == 0:
            self._update_vram_display()

        self.after(2000, self._periodic_refresh)

    def _update_vram_display(self):
        try:
            ok, used_mb, total_mb = core.get_gpu_vram_info()
            if hasattr(self, "lbl_vram_info"):
                if ok:
                    gb_used = round(used_mb / 1024, 2)
                    gb_total = round(total_mb / 1024, 1)
                    color = "#b45309" if used_mb > 4000 else self.MUTED
                    self.lbl_vram_info.config(
                        text=f"当前显卡已用显存: {used_mb} MB ({gb_used} GB) / {gb_total} GB",
                        fg=color
                    )
                else:
                    self.lbl_vram_info.config(text="显存状态: 正常")
        except Exception:
            pass

    def _action_offload_gpu(self):
        """Offload local ASR models from GPU to free VRAM, and offer CPU fallback switch."""
        def _offload_worker():
            core.request_gpu_offload()
            time.sleep(0.6)

            ok, used_mb, total_mb = core.get_gpu_vram_info()
            vram_str = f"{used_mb} MB / {total_mb} MB" if ok else "显存已释放"

            gpu_slots = []
            for i in (1, 2, 3):
                var = getattr(self, f"v_fb{i}", None)
                if var:
                    val = self.SLOT_TO_SPEC.get(var.get(), "")
                    if val and "cuda" in val:
                        gpu_slots.append(f"Fallback {i} ({var.get()})")

            def _ui_callback():
                self._update_vram_display()
                msg = f"✅ 已成功向守护进程发送卸载指令，本地模型已移出 GPU！\n\n当前显卡显存占用: {vram_str}\n"
                if gpu_slots:
                    prompt = (
                        f"{msg}\n⚠️ 检测到控制面板当前 Fallback 槽位仍选用了 GPU 引擎:\n"
                        f" • " + "\n • ".join(gpu_slots) + "\n\n"
                        "是否将这些槽位一键切换为对应的【纯 CPU 模式】？\n\n"
                        "【是】: 自动切换为 CPU 引擎并保存应用，避免下次语音输入重新唤醒 GPU；\n"
                        "【否】: 仅立即释放显存，保留当前配置（下次触发时再按需加载）。"
                    )
                    switch_cpu = messagebox.askyesno("GPU 显存已释放", prompt)
                    if switch_cpu:
                        self._switch_gpu_slots_to_cpu()
                else:
                    messagebox.showinfo("GPU 显存已释放", f"{msg}\n目前没有配置任何本地 GPU 引擎，显存将保持空闲。")

            self.after(0, _ui_callback)

        threading.Thread(target=_offload_worker, daemon=True).start()

    def _switch_gpu_slots_to_cpu(self):
        """Helper to switch any configured -cuda fallback slots to -cpu and save."""
        cuda_to_cpu = {
            "sensevoice-cuda": "sensevoice-cpu",
        }
        for i in (1, 2, 3):
            var = getattr(self, f"v_fb{i}", None)
            if var:
                spec = self.SLOT_TO_SPEC.get(var.get(), "")
                if spec in cuda_to_cpu:
                    target_spec = cuda_to_cpu[spec]
                    target_slot = self.SPEC_TO_SLOT.get(target_spec)
                    if target_slot:
                        var.set(target_slot)
        self.save_and_apply_all()
        self._update_vram_display()
        messagebox.showinfo("切换完成", "已成功将本地引擎切换为 CPU 模式并实时应用生效！")


if __name__ == "__main__":
    # Single-instance guard: if a manager window is already open, the existing
    # one is asked to surface and this process exits WITHOUT creating a window.
    # This makes every launch path (manager.bat, check_env, tray, overlay)
    # self-deduplicating — no more stacked duplicate manager windows.
    if not _claim_gui_instance():
        sys.exit(0)
    app = VoxManagerApp()
    app.mainloop()
