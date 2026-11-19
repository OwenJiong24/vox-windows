#!/usr/bin/env python3
"""
Vox — Windows Voice Input Daemon.

Hold hotkey to record, release to transcribe and auto-paste.
Or tap hotkey to toggle hands-free recording, tap again or press Enter to finish.
Press Esc at any time to cancel recording.

Engines: Groq (Whisper Large V3 Turbo, ~300ms) -> MiMo cloud -> SenseVoice (local, on demand).
Smart Output: Smart paste via CF_UNICODETEXT + Ctrl+V, with automatic clipboard history restore.
"""

import sys
import os

# MUST come before numpy / torch: OMP_NUM_THREADS and friends are read when the
# BLAS/OpenMP runtime initialises, i.e. on the first numpy or torch import. Capping
# threads makes the local model load ~2.5x faster (22.6s -> 8.9s measured) and is no
# slower at inference; see vox_tuning.py for the measurements.
import vox_tuning  # noqa: F401  (import for its side effect; order is load-bearing)

import json
import struct
import time
import traceback
import shutil
import threading
import subprocess
import tempfile
import socket
from datetime import datetime
import numpy as np

import vox_core as core
import vox_win32 as _win32
import vox_vad
from vox_core import log

# Platform modules
try:
    import keyboard
    _KB_OK = True
except Exception:
    keyboard = None
    _KB_OK = False

try:
    import pystray
    from PIL import Image, ImageDraw
    _TRAY_OK = True
except Exception:
    pystray = None
    Image = ImageDraw = None
    _TRAY_OK = False


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STOP_FILE = os.path.join(SCRIPT_DIR, "vox.stop")
# G7: drop this file to offload all local ASR models from the GPU (frees VRAM)
# without stopping the daemon. Handled by the main watch loop.
OFFLOAD_FILE = os.path.join(SCRIPT_DIR, "vox.offload")
OVERLAY_SCRIPT = os.path.join(SCRIPT_DIR, "vox_overlay.py")
OVERLAY_STATE_FILE = os.path.join(SCRIPT_DIR, "overlay_state.json")
# F7: recording hot-backup — a live snapshot of the in-progress recording is flushed to
# disk every few seconds so a crash/interrupt mid-recording does not lose the audio.
REC_BACKUP_FILE = os.path.join(SCRIPT_DIR, "vox_rec_backup.wav")
REC_BACKUP_FLUSH_S = 5.0
GUI_SCRIPT = os.path.join(SCRIPT_DIR, "vox_gui.py")
SINGLE_INSTANCE_PORT = 48123


def acquire_single_instance_lock(name="Global\\Vox_Windows_Daemon_SingleInstance_Mutex", port=38123):
    """
    Acquires a single-instance lock via Win32 Named Mutex / Socket fallback.
    Returns the lock instance on success, or None if already running.
    This guarantees strictly ONLY ONE daemon instance can ever run,
    preventing duplicate hotkey triggering and double-paste bugs.
    """
    lock = core.SingleInstanceLock(name=name, port=port)
    if lock.acquire():
        return lock
    return None


# Process-liveness helpers live in vox_core (shared by daemon + GUI); re-export here.
_process_alive = core._process_alive
_scan_cmdline_procs = core._scan_cmdline_procs
find_existing_daemon_pids = core.find_existing_daemon_pids
kill_all_vox_overlays = core.kill_all_vox_overlays
kill_all_vox_daemons = core.kill_all_vox_daemons


# ── Backward Compatibility Aliases for Legacy Scripts ──────────────
load_config = core.ConfigManager.load
save_config = core.ConfigManager.save
post_process = core.PostProcessor.process
AudioCapture = core.AudioPipeline
GroqEngine = core.GroqEngine
EngineChain = core.EngineChain
write_wav = core.write_wav
to_flac_or_wav = core.to_flac_or_wav
output_text = lambda text, cfg, recording_id, result=None: core.SmartOutput.output(text, method=cfg.get("output_method", "smart_paste"))
get_api_key = lambda cfg: core.EngineChain(cfg)._resolve_api_key()
CONFIG_PATH = core.CONFIG_PATH


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _create_tray_icon_image(state="idle"):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if state == "recording":
        bg = (239, 68, 68, 255)    # Bright Red
    elif state == "transcribing":
        bg = (245, 158, 11, 255)   # Amber / Orange
    else:
        bg = (16, 185, 129, 255)   # Emerald Green

    # Circular badge with clean white outline
    d.ellipse([2, 2, size - 3, size - 3], fill=bg, outline=(255, 255, 255, 230), width=3)

    # Center white glyph
    if state == "recording":
        # Solid recording indicator
        d.rounded_rectangle([20, 20, 44, 44], radius=4, fill=(255, 255, 255, 255))
    elif state == "transcribing":
        # Hourglass / transcribing indicator
        d.polygon([(22, 18), (42, 18), (32, 32)], fill=(255, 255, 255, 255))
        d.polygon([(22, 46), (42, 46), (32, 32)], fill=(255, 255, 255, 255))
    else:
        # Crisp microphone
        d.rounded_rectangle([26, 14, 38, 32], radius=6, fill=(255, 255, 255, 255))
        d.arc([20, 22, 44, 40], start=0, end=180, fill=(255, 255, 255, 255), width=3)
        d.line([32, 40, 32, 48], fill=(255, 255, 255, 255), width=3)
        d.line([24, 48, 40, 48], fill=(255, 255, 255, 255), width=3)

    return img


def _wav_begin(path, sr=16000, channels=1, bits=16):
    """Create a 16-bit PCM WAV with placeholder sizes; return the open handle.

    Part of the append-only hot backup. `wave`'s own writer only patches the header on
    close, which is useless for a crash-recovery file — so the sizes are patched after
    every append instead, keeping the file valid at all times.
    """
    f = open(path, "wb")
    block_align = channels * bits // 8
    byte_rate = sr * block_align
    f.write(b"RIFF" + struct.pack("<I", 36) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sr,
                                    byte_rate, block_align, bits)
            + b"data" + struct.pack("<I", 0))
    return f


def _wav_patch(f, data_bytes):
    """Rewrite RIFF and data size fields so the file is playable RIGHT NOW."""
    f.seek(4)
    f.write(struct.pack("<I", 36 + data_bytes))
    f.seek(40)
    f.write(struct.pack("<I", data_bytes))
    f.flush()


def _wav_append(f, pcm_bytes, written):
    """Append PCM bytes, patch the sizes, and return the new running total."""
    f.seek(0, os.SEEK_END)
    f.write(pcm_bytes)
    written += len(pcm_bytes)
    _wav_patch(f, written)
    return written


# The `keyboard` library reports left/right variants and several spellings for the same
# physical key; config tokens use yet another spelling ("control" vs the library's
# "ctrl", "win" vs "windows"). Everything is normalised through here before the held-key
# set is compared against the configured combo.
_KB_NAME_ALIASES = {
    "left ctrl": "ctrl", "right ctrl": "ctrl", "control": "ctrl", "ctrl": "ctrl",
    "left alt": "alt", "right alt": "alt", "alt gr": "alt",
    "left shift": "shift", "right shift": "shift",
    "left windows": "win", "right windows": "win", "windows": "win",
    "cmd": "win", "super": "win", "win": "win",
    "escape": "esc", "return": "enter",
}


def _norm_kb_name(name):
    n = str(name or "").lower().strip()
    return _KB_NAME_ALIASES.get(n, n)


class VoxDaemon:
    # Minimum interval between overlay_state.json writes (s). The PortAudio callback
    # fires ~10-100ms per block and used to rewrite the whole file on the audio thread
    # every time, starving the audio thread and causing level spikes / bubble jitter
    # (audit B7). The overlay polls the file itself every ~40ms, so coalescing rapid
    # level-only refreshes below this rate is invisible but removes the IO pressure.
    MIN_OVERLAY_WRITE_INTERVAL = 0.040

    def __init__(self):
        self.cfg = core.ConfigManager.load()
        self.pipeline = core.AudioPipeline()
        self.chain = core.EngineChain(self.cfg)
        self.tray = None
        self._stop_event = threading.Event()
        self._overlay_proc = None
        # Active input-device binding for hot-plug detection (B5 / complaint #5).
        self._bound_device = None
        # Overlay-state write debounce bookkeeping (B7).
        self._overlay_last_write_ts = 0.0
        self._last_overlay_state = None
        self._last_overlay_text = ""
        self._last_overlay_hint = ""

        # State machine
        # States: IDLE, CHECKING_TAP_HOLD, RECORDING_HOLD, RECORDING_TOGGLE, TRANSCRIBING
        self.state = "IDLE"
        self._state_lock = threading.Lock()
        # BUG-6 fix: overlay_state.json / vox_health.json are written from
        # several threads (watch_loop, _level_publisher, config poller, tray,
        # transcription worker). Serialize the debounce bookkeeping + writes.
        self._io_lock = threading.Lock()
        self._press_start_time = 0.0
        # BUG-18: becomes True only after the hotkey is released in TOGGLE
        # mode, so the press that STARTED the take cannot also finish it.
        self._toggle_armed = False
        self._record_start_time = 0.0
        self._last_level = 0.0
        self._active_combo = "shift"
        self._is_single_modifier = True
        self._combo_tokens = ["shift"]
        self._other_key_pressed_during_combo = False
        self._hook = None
        self._hook_ready = False
        self._hook_combo_down = False
        self._lock_socket = None
        self._last_preview_text = ""
        # Per-take generation id. Background workers (hot-backup, streaming) capture
        # the value at take start and retire as soon as it changes, so finishing one
        # take can never leave a worker alive against the *next* take's state.
        self._rec_session = 0
        # VK groups for the active hotkey, resolved once so the 25 Hz poll loop
        # never has to re-parse the hotkey string.
        self._combo_vks = []
        # Keys currently held, maintained by the global keyboard hook. This is the
        # focus-independent source for hotkey state (see _is_hotkey_pressed).
        self._kb_down = set()
        self._combo_norm = set()
        self._ctrl_tick = 0

        self._parse_hotkey()
        core.ConfigManager.register_change_callback(self.on_config_changed)

    def _parse_hotkey(self):
        hk = self.cfg.get("hotkey", {})
        if "combo" in hk and hk["combo"]:
            combo_str = str(hk["combo"]).lower().strip()
            self._active_combo = combo_str
            self._combo_tokens = [t.strip() for t in combo_str.split("+") if t.strip()]
        else:
            mods = [str(m).lower().strip() for m in hk.get("modifiers", ["shift"]) if m]
            key = str(hk.get("key") or "").lower().strip()
            tokens = mods + ([key] if key else [])
            if not tokens:
                tokens = ["shift"]
            self._combo_tokens = tokens
            self._active_combo = "+".join(tokens)
        self._is_single_modifier = len(self._combo_tokens) == 1 and self._combo_tokens[0] in ("shift", "control", "ctrl", "alt", "win", "right shift", "left shift")
        # Resolve the combo to virtual-key groups ONCE. The main loop polls the
        # hotkey 25x/s; asking the `keyboard` library instead made it re-parse the
        # combo string and copy the entire pressed-key set ~120x/s.
        self._combo_vks = _win32.resolve_hotkey_vks(self._combo_tokens)
        # Same combo in the `keyboard` library's naming, for the hook-based check.
        self._combo_norm = {_norm_kb_name(t) for t in self._combo_tokens if t}
        self._hotkey_mode = hk.get("mode", "adaptive")
        self._hold_thresh = max(0.15, float(hk.get("hold_threshold_ms", 350)) / 1000.0)

    def on_config_changed(self, new_cfg):
        log("检测到配置更新，正在热重载参数...", "INFO")
        self.cfg = new_cfg
        self.chain = core.EngineChain(new_cfg)
        self._parse_hotkey()
        self.update_health()
        log(f"热重载完成: 热键={self._active_combo}, 模式={self._hotkey_mode}, 引擎优先级={self.cfg.get('engine_priority')}", "INFO")

    def _config_poller(self):
        """Hot-reload guard (fixes B2). The daemon used to read config only once at
        __init__, so every GUI edit was silently ignored until a manual restart.
        ConfigManager.load() already reloads from disk on mtime; here we poll it and
        apply any diff immediately (hotkey re-parse, engine chain, streaming interval...)."""
        while not self._stop_event.is_set():
            try:
                fresh = core.ConfigManager.load()
                if fresh is not self.cfg:
                    self.on_config_changed(fresh)
            except Exception as e:
                log(f"热重载轮询异常: {e}", "DEBUG")
            self._stop_event.wait(1.0)

    def _device_watch(self):
        """Hot-plug guard (complaint #5 / audit B5). Periodically re-resolves the input
        device and, when it changes (plug/unplug headphones, Windows default-switch),
        notifies and records the binding so the active mic is visible in health/GUI.
        Recording itself already re-resolves the device on every start (_start_recording),
        so the new mic is used for the next dictation automatically; we deliberately do
        NOT restart an in-progress stream (that would glitch audio)."""
        # 15 s instead of 2 s: list_input_devices caches for 5 s, so the old cadence
        # ran a FULL PortAudio enumeration (sd.query_devices + query_hostapis) every
        # other tick, forever. Enumerating devices can disturb an active stream, so
        # we also skip entirely while audio is being captured.
        interval = 15.0
        while not self._stop_event.is_set():
            try:
                if self.state != "IDLE":
                    # Recording/transcribing: _start_recording already re-resolves the
                    # device per take, so polling now buys nothing but risk.
                    self._stop_event.wait(interval)
                    continue
                cur_idx, cur_name = self.pipeline.resolve_device(
                    self.cfg.get("audio", {}).get("preferred_device")
                )
                cur = {"index": cur_idx, "friendly_name": cur_name}
                prev = self._bound_device
                if prev is not None and core.input_device_changed(prev, cur):
                    log(f"🎚️ 检测到麦克风更换: [{prev['index']}] {prev['friendly_name']} -> [{cur['index']}] {cur['friendly_name']}", "INFO")
                    self._bound_device = cur
                    try:
                        h = core.safe_read_json(core.HEALTH_FILE, {})
                        h["last_device_index"] = cur_idx
                        h["last_device_name"] = cur_name
                        h["device_changed_at"] = datetime.now().isoformat()
                        core.safe_write_json(core.HEALTH_FILE, h)
                    except Exception:
                        pass
            except Exception as e:
                log(f"麦克风热插拔轮询异常: {e}", "DEBUG")
            self._stop_event.wait(interval)

    # ── Overlay Bridge ───────────────────────────────────────────
    def _update_overlay(self, state, text="", hint="", level=0.0, preview=None, engine=None,
                        latency=None):
        if preview is not None:
            self._last_preview_text = preview
        if engine is None:
            # Default: the engine the EngineChain would try first (primary of priority list).
            # G1: qualify groq slots with their concrete model so the bubble badge
            # shows exactly what will serve (e.g. "groq (whisper-large-v3)" not bare "groq").
            prio = self.cfg.get("engine_priority", ["groq-turbo"])
            engine = prio[0] if prio else "groq"
            fam, variant = (engine.split("-", 1) + [""])[:2] if engine else ("groq", "")
            if fam == "groq":
                model = {"turbo": "whisper-large-v3-turbo", "l3": "whisper-large-v3"}.get(
                    variant, self.cfg.get("engines", {}).get("groq", {}).get("model", "whisper-large-v3-turbo"))
                engine = f"groq ({model})"
            elif fam == "sensevoice":
                engine = f"sensevoice ({'cuda' if variant == 'cuda' else 'cpu' if variant == 'cpu' else 'auto'})"
        data = {
            "state": state,
            "text": text,
            "hint": hint,
            "preview": self._last_preview_text,
            "level": round(float(level), 4),
            "start_time": self._record_start_time if "RECORDING" in self.state else 0.0,
            "engine": engine,
            # G6.4: transcription latency for the done-state timer (bubble shows how
            # long the last transcription took, mirroring the recording timer).
            "latency": latency,
            "updated_at": datetime.now().isoformat(),
        }
        # B7 debounce: only coalesce rapid PURE-level refreshes (same state/text/hint and
        # no preview change) that fall inside MIN_OVERLAY_WRITE_INTERVAL. Every state
        # transition or content change writes through immediately so UI behavior is exact.
        with self._io_lock:
            if self._overlay_throttle(
                self._last_overlay_state, self._last_overlay_text, self._last_overlay_hint,
                self._overlay_last_write_ts, state, text, hint, preview,
                now=time.monotonic(), interval=self.MIN_OVERLAY_WRITE_INTERVAL,
            ):
                return
            # Record this as the last write so subsequent same-state refreshes can coalesce.
            self._overlay_last_write_ts = time.monotonic()
            self._last_overlay_state = state
            self._last_overlay_text = text
            self._last_overlay_hint = hint
            core.safe_write_json(OVERLAY_STATE_FILE, data)

    @staticmethod
    def _overlay_throttle(prev_state, prev_text, prev_hint, prev_ts,
                          state, text, hint, preview, now, interval):
        """Return True when this overlay-state write should be skipped (coalesced).
        Pure/testable: a level-only refresh within `interval` of the last write is
        dropped; any state transition or text/hint/preview change passes through."""
        try:
            return (
                state == prev_state
                and text == prev_text
                and hint == prev_hint
                and preview is None
                and (now - prev_ts) < interval
            )
        except Exception:
            return False

    def spawn_overlay(self):
        if not self.cfg.get("overlay", {}).get("enabled", True):
            return
        if self._overlay_proc and self._overlay_proc.poll() is None:
            return
        try:
            self._overlay_proc = subprocess.Popen(
                # Tell the overlay which daemon owns it. Without this it has no way
                # to notice the daemon died, and an orphaned overlay leaves its pill
                # on screen forever with no way for the user to dismiss it.
                [sys.executable, OVERLAY_SCRIPT, "--parent-pid", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log(f"启动悬浮窗进程失败: {e}", "WARN")

    def kill_overlay(self):
        if self._overlay_proc and self._overlay_proc.poll() is None:
            try:
                self._overlay_proc.terminate()
            except Exception:
                pass
        self._overlay_proc = None
        # Clear the child's self-registered pid marker (set via _self_register_pid).
        try:
            opf = os.path.join(SCRIPT_DIR, "overlay.pid")
            if os.path.exists(opf):
                os.unlink(opf)
        except Exception:
            pass
        # Sweep orphaned ghost pill windows left behind by a prior crash/exit (B1).
        try:
            killed = kill_all_vox_overlays()
            if killed:
                log(f"清理孤儿悬浮窗进程 PID={killed}", "DEBUG")
        except Exception:
            pass

    # ── Health & Stats ───────────────────────────────────────────
    def update_health(self):
        data = {
            "pid": os.getpid(),
            "state": self.state.lower(),
            "audio_device": self._dev_name() or "自动选择",
            # Report the engines that actually exist. This used to list "local"
            # (faster-whisper), which was deleted, and omitted the real retained local
            # engine — so vox_health.json advertised a capability that was gone.
            "engines": {k: bool(self.cfg.get("engines", {}).get(k, {}).get("enabled", True))
                        for k in ("groq", "mimo", "sensevoice")},
            "has_groq_key": bool(self.chain._resolve_api_key()),
            "hotkey_combo": self._active_combo.upper(),
            "hotkey_hook_ready": bool(self._hook_ready),
            "updated_at": datetime.now().isoformat(),
        }
        with self._io_lock:
            core.safe_write_json(core.HEALTH_FILE, data)

    # ── Tray Management ───────────────────────────────────────────
    def _setup_tray(self):
        if not _TRAY_OK:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem(lambda text: f"Vox: {self._tray_status_text()}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("🎙️ 开始/停止录音", self._tray_toggle_recording),
                pystray.MenuItem("⚙️ 打开设置中心 (Manager)", self._open_gui, default=True),
                pystray.MenuItem("⚡ 重新校准麦克风", self._recalibrate_mic),
                pystray.MenuItem("🧹 释放 GPU 显存 (卸载本地模型)", lambda: threading.Thread(target=self._offload_worker, daemon=True).start()),
                pystray.MenuItem("🔁 重载配置", lambda: self.on_config_changed(core.ConfigManager.load())),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("❌ 退出 Vox", self.stop),
            )
            tooltip = f"Vox 语音输入: {self._tray_status_text()}"

            def _make_icon():
                return pystray.Icon("Vox", _create_tray_icon_image("idle"), tooltip, menu)

            def _tray_setup(icon):
                """Show the icon, then report its REAL state a few seconds later.

                CRITICAL: passing a custom `setup` REPLACES pystray's default one, and
                pystray's `run` docstring is explicit about the consequence —
                "If not specified, a simple setup function setting `visible` to True is
                used. If you specify a custom setup function, you must explicitly set
                this attribute." An earlier version of this diagnostic omitted the line
                below and therefore GUARANTEED no tray icon: a blocking run() with
                visible=False looks exactly like a healthy one from the outside, so the
                diagnostic silently broke the feature it was measuring.
                """
                try:
                    icon.visible = True
                except Exception as e:
                    log(f"⚠️ 托盘图标显示失败: {e}", "WARN")

                def _report():
                    try:
                        time.sleep(3.0)
                        if icon.visible:
                            log("✅ 托盘图标已显示 (visible=True)", "INFO")
                        else:
                            log("⚠️ 托盘图标已注册但 visible=False —— 图标未显示。"
                                "Windows 11 常把新图标收进任务栏的 ^ 溢出区，先点 ^ 找它。"
                                "若溢出区里也没有，请用 run.bat 打开设置中心（已不再锁死）。",
                                "WARN")
                    except Exception:
                        pass
                threading.Thread(target=_report, daemon=True, name="vox-tray-check").start()

            def _tray_run():
                """Run the tray icon, SURFACING failures instead of losing them.

                Two problems with the original one-liner:

                1. `pystray.Icon.run` raises from inside its own thread, and the
                   daemon is launched with pythonw.exe — so the traceback went to a
                   discarded stderr. The tray icon simply never appeared and nothing
                   anywhere said why: the user silently lost the whole right-click
                   menu (start/stop, settings, calibrate, quit).

                2. Windows can deny `ChangeWindowMessageFilterEx` with WinError 5
                   when the process runs under a restricted token. pystray treats
                   that as fatal, but the call only buys resilience to an Explorer
                   restart (re-adding the icon on TaskbarCreated). Losing the entire
                   menu over that is a bad trade, so we retry once with the call
                   neutralised.
                """
                try:
                    self.tray.run(setup=_tray_setup)
                    return
                except Exception as e:
                    log(f"⚠️ 托盘图标启动失败，尝试绕过受限调用重试: {e}", "WARN")

                try:
                    from pystray._util import win32 as _pystray_win32
                    _pystray_win32.ChangeWindowMessageFilterEx = lambda *a, **k: True
                    self.tray = _make_icon()
                    # Log BEFORE run(): a successful run() blocks in the tray message
                    # loop for the lifetime of the icon, so anything after it is
                    # unreachable while the tray is actually up.
                    log("托盘: 已绕过 ChangeWindowMessageFilterEx, 正在重试创建图标", "INFO")
                    self.tray.run(setup=_tray_setup)
                except Exception as e2:
                    log(f"⚠️ 托盘图标重试仍失败，右键菜单不可用: {e2}", "WARN")
                    self.tray = None

            self.tray = _make_icon()
            threading.Thread(target=_tray_run, daemon=True, name="vox-tray").start()

            # Windows 11 may bury the icon under the '^' overflow chevron. Promote it
            # ONCE. The old code retried at 0.3/1.0/3.0s, but the write to
            # HKCU\Control Panel\NotifyIconSettings is denied (WinError 5) on this
            # machine every single time — the 12 historical log lines are all that
            # retry loop achieving nothing. One attempt, then stop.
            def _promote_worker():
                time.sleep(1.0)
                try:
                    core.promote_tray_icon_in_windows()
                except Exception:
                    pass
            threading.Thread(target=_promote_worker, daemon=True, name="vox-tray-promote").start()

            # Display a brief native Windows balloon notification confirming it's active
            time.sleep(0.5)
            try:
                if self.tray is not None:
                    self.tray.notify(
                        f"全局热键: [{self._active_combo.upper()}] (按住说话，松手出字；轻按免手持)",
                        "Vox 语音输入已就绪"
                    )
            except Exception:
                pass
        except Exception as e:
            log(f"托盘图标启动失败: {e}", "WARN")

    def _recalibrate_mic(self):
        log("开始自动校准麦克风...", "INFO")
        threading.Thread(target=lambda: subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "calibrate.py")]), daemon=True).start()

    def _tray_status_text(self):
        if "RECORDING" in self.state:
            return "🔴 正在录音中..."
        if self.state == "TRANSCRIBING":
            return "⏳ 正在识别中..."
        return f"🟢 就绪 (按 {self._active_combo.upper()} 说话)"

    def _update_tray_icon(self, state="idle"):
        if self.tray and _TRAY_OK:
            try:
                self.tray.icon = _create_tray_icon_image(state)
                self.tray.title = f"Vox — {self._tray_status_text()}"
            except Exception:
                pass

    def _tray_toggle_recording(self):
        with self._state_lock:
            if self.state == "IDLE":
                self._start_recording(mode="RECORDING_TOGGLE", hint="免手持录音中 (点击托盘/热键结束)")
            elif "RECORDING" in self.state:
                self._finish_recording()

    def _open_gui(self):
        # 1. If GUI is already running (it holds a named mutex), wake it via
        #    the restore flag file — the GUI's ~2s refresh loop surfaces it.
        #    Port-free: the old 127.0.0.1:48124 RESTORE socket could never
        #    bind on this machine (that port range is reserved), so it always
        #    fell through to spawning a duplicate window.
        if core.gui_is_running():
            core.gui_request_restore()
            return

        # 2. Otherwise launch new GUI process (vox_gui.py self-deduplicates
        #    via its own named mutex, so this can never double a window).
        try:
            subprocess.Popen([sys.executable, GUI_SCRIPT])
        except Exception as e:
            log(f"启动管理界面失败: {e}", "WARN")

    # ── Audio Pipeline Bridge ─────────────────────────────────────
    def _on_audio_level(self, peak):
        """Level callback — invoked from PortAudio's REALTIME callback thread.

        It must never touch the disk. This used to call `_update_overlay`, which
        rewrites overlay_state.json (json.dumps + open/truncate/write, with up to 5
        retry sleeps inside safe_write_json) — as often as 25 whole-file writes per
        second *from inside the audio callback*. That stalls the capture thread,
        which is exactly how you get dropped blocks and choppy audio. Now the
        callback only stores the value and `_level_publisher` does the I/O on its
        own thread.
        """
        self._last_level = peak

    def _level_publisher(self):
        """Publish the mic level to the overlay off the audio thread (~25 Hz)."""
        while not self._stop_event.is_set():
            self._stop_event.wait(0.04)
            # BUG-7 fix: a single attribute read is atomic under CPython's GIL.
            # Taking _state_lock here made this 25 Hz publisher block whenever
            # watch_loop held the lock across PortAudio start/stop (hundreds of
            # ms), freezing the live waveform at every take boundary.
            active = self.state in ("CHECKING_TAP_HOLD", "RECORDING_HOLD", "RECORDING_TOGGLE")
            if not active:
                continue
            try:
                self._update_overlay("recording", level=self._last_level)
            except Exception:
                pass

    # ── Pipeline attribute access (defensive) ─────────────────────
    # B1/B2 attribution reads three pipeline attributes. Any of them missing must
    # never turn a metadata lookup into a FAILED TRANSCRIPTION: the real AudioPipeline
    # always has them, but a test stub or a partially-constructed pipeline does not.
    # (Caught by test_suite #26: "'_P' object has no attribute 'device_name'".)
    def _dev_name(self):
        """Name of the microphone that actually captured this take, or ""."""
        return str(getattr(self.pipeline, "device_name", "") or "")

    def _dev_idx(self):
        """Index of that microphone, or "-" when unknown."""
        idx = getattr(self.pipeline, "device_idx", None)
        return "-" if idx is None else idx

    def _session_peak(self):
        """Highest PRE-AGC sample of this take as a float (0.0 when unavailable)."""
        try:
            return float(getattr(self.pipeline, "session_peak", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _peak_verdict(self):
        """silent / quiet / ok from the pipeline, or "ok" when it cannot say."""
        try:
            return self.pipeline.peak_verdict()
        except Exception:
            return "ok"

    # ── Core Actions ──────────────────────────────────────────────
    def _start_recording(self, mode="CHECKING_TAP_HOLD", hint="松手完成 · 单击锁定"):
        # New take => new generation. Any worker still running for a previous take
        # sees the changed id and retires instead of following this one's state.
        self._rec_session += 1
        try:
            pref_dev = self.cfg.get("audio", {}).get("preferred_device")
            dev_idx, dev_name = self.pipeline.resolve_device(pref_dev)
            self.pipeline.live_level_cb = self._on_audio_level
            # Honour audio.max_recording_seconds (previously defined but never read).
            try:
                self.pipeline.max_seconds = float(
                    self.cfg.get("audio", {}).get("max_recording_seconds", 300) or 300
                )
            except Exception:
                self.pipeline.max_seconds = 300.0
            self.pipeline.start_recording(dev_idx)
        except Exception as e:
            log(f"启动录音失败: {e}", "ERROR")
            self._update_overlay("error", text=f"麦克风错误: {e}")
            self.state = "IDLE"
            return

        self.state = mode
        # BUG-18: entering TOGGLE must not be armed yet — the hotkey may still
        # be physically held (toggle_only starts on the press itself). Only a
        # later release-from-press arms the "press again to finish" condition.
        self._toggle_armed = False
        # BUG-8: monotonic clock. time.time() can jump on NTP/manual clock
        # changes, which made the recording timer reset/jump mid-take.
        # Windows' monotonic source is system-wide, so the overlay process can
        # use the value written to overlay_state.json directly.
        self._record_start_time = time.monotonic()
        self._other_key_pressed_during_combo = False
        self._last_preview_text = ""

        # Sound cue & Overlay & Tray
        core.SoundFeedback.play("start", enabled=self.cfg.get("sound_effects", True))
        self._update_overlay("recording", hint=hint, level=0.0, preview="")
        self._update_tray_icon("recording")
        self.update_health()
        log(f"▶️ 开始录音 [设备: {dev_name}] (模式: {mode})", "INFO")

        # Streaming interim preview worker (local GPU / cloud live transcription)
        if self.cfg.get("streaming", {}).get("enabled", True):
            threading.Thread(target=self._streaming_worker, args=(self._rec_session,),
                             daemon=True).start()

        # F7: hot-backup worker — flush the growing recording to disk every few seconds
        # so a crash/interrupt mid-recording never loses the whole take. The session id
        # makes the worker retire with THIS take instead of lingering into the next one.
        threading.Thread(target=self._rec_backup_worker, args=(self._rec_session,),
                         daemon=True).start()

    def _rec_backup_worker(self, session):
        """F7: periodically snapshot the in-progress recording to REC_BACKUP_FILE.

        Bound to a per-take `session` id. The old loop tested only `self.state`,
        which is a *shared* predicate: finishing take A and starting take B within
        one flush interval left worker A running forever, because B's state also
        looked like "recording". Each leaked worker then deep-copied every frame
        while holding `pipeline._lock` — the same lock the PortAudio callback needs
        to append a block — so overlapping takes caused real capture glitches and
        duplicate writers racing on the same file.

        IO (PERF): the snapshot is now APPEND-ONLY. The old version called write_wav()
        on the WHOLE recording on every flush, which is quadratic disk IO — a 300 s
        take at 16 kHz rewrote 160 KB, then 320 KB, ... then 9.6 MB every 5 s, about
        288 MB of writes to protect 9.6 MB of audio. Now the file is opened once, only
        the new samples are appended, and the two WAV size fields are patched after
        each append (so the file is a valid, playable WAV at every instant — which is
        the entire point of a crash-recovery file, and why `wave`'s own writer, which
        patches the header only on close, was unusable here).
        """
        first_flush = True
        fh = None            # append-only handle; opened on the first real audio
        written = 0          # PCM bytes written so far
        cursor = 0           # samples already flushed
        try:
            while not self._stop_event.is_set():
                if session != self._rec_session:
                    break
                # BUG-7: state read is atomic; do not block on _state_lock here.
                if self.state not in ("CHECKING_TAP_HOLD", "RECORDING_HOLD", "RECORDING_TOGGLE"):
                    break
                try:
                    # Shallow-copy the frame list under the lock (cheap: references only)
                    # and do the concatenate/copy OUTSIDE it. The old version converted
                    # every frame to float32 while holding the lock, stalling capture.
                    with self.pipeline._lock:
                        frames = list(self.pipeline.frames)
                    if frames:
                        audio = np.concatenate(frames).ravel()
                        sr = self.pipeline.device_sr
                        if fh is None and audio.size < sr * 0.8:
                            # Still leading silence — nothing worth backing up yet.
                            self._stop_event.wait(REC_BACKUP_FLUSH_S)
                            continue
                        if audio.size > cursor:
                            if fh is None:
                                fh = _wav_begin(REC_BACKUP_FILE, sr)
                            # Append ONLY the samples added since the last flush, so
                            # the disk IO is linear in the recording length instead of
                            # quadratic. See the docstring for the measured cost.
                            pcm = np.clip(audio[cursor:], -1.0, 1.0)
                            written = _wav_append(
                                fh, (pcm * 32767).astype("<i2").tobytes(), written)
                            cursor = int(audio.size)
                            if first_flush:
                                log(f"🛡️ 录音热备份已启用 ({REC_BACKUP_FLUSH_S:.0f}s 刷新)", "DEBUG")
                                first_flush = False
                except Exception as e:
                    log(f"录音热备份写入异常: {e}", "DEBUG")
                self._stop_event.wait(REC_BACKUP_FLUSH_S)
        finally:
            # The header is already correct after the last append; just close.
            if fh is not None:
                try:
                    _wav_patch(fh, written)
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass

    def _cleanup_rec_backup(self, keep=False):
        """F7: remove the hot-backup after a successful take; keep it on cancel/failure."""
        try:
            if os.path.exists(REC_BACKUP_FILE):
                if keep:
                    log(f"🛡️ 已保留录音热备份: {REC_BACKUP_FILE} (可手动重转)", "INFO")
                else:
                    os.unlink(REC_BACKUP_FILE)
        except Exception:
            pass

    def _streaming_worker(self, session):
        """Incremental streaming preview: transcribe each SETTLED speech segment once.

        PERF — this is the fix for the preview's quadratic cost. The old loop
        re-transcribed the ENTIRE accumulated recording on every tick. A 20 s dictation
        at a 500 ms tick pushed about 410 s of audio through the model; at the measured
        CPU RTF of 0.042 that is ~17 s of CPU inside a 20 s recording — an entire core,
        continuously, just to draw a preview. Worse, it grew without bound with length.

        Now the take is kept in ONE 16 kHz accumulator and `vox_vad` finds the pauses.
        Every speech run closed by a real pause is transcribed exactly once and its text
        remembered; each tick only transcribes the short unsettled tail. Total work
        becomes O(recording length) rather than O(ticks x length), and only the NEW
        tail is resampled, which was also per-tick O(n).

        Frames are still read as float32 @ device_sr and never reinterpreted as int16
        (that was the old garbled-preview root cause).
        """
        # The interim engine is opt-in (streaming.interim_engine). With no slot
        # configured there is nothing cheap to run every tick, so bail before touching
        # the audio buffer at all — no resample, no transcription, no local model.
        if not (self.cfg.get("streaming", {}) or {}).get("interim_engine"):
            return
        interval = max(0.35, float(self.cfg.get("streaming", {}).get("interval_ms", 500)) / 1000.0)
        time.sleep(0.35)  # quick buffer for the first syllables

        SR = self.pipeline.TARGET_SR
        # Per-take state. The worker is bound to one take via `session`, so all of this
        # dies with it and can never leak into the next recording.
        frames_done = 0            # how many pipeline frames we have already consumed
        pcm = None                 # 16 kHz accumulator for this take
        final_end = 0              # 16 kHz index already transcribed and settled
        final_text = ""            # text of everything before final_end
        last_preview = ""
        last_tail_size = 0         # 16 kHz length of the tail at its last transcription
        tail_text = ""             # cached transcription of the current tail
        MAX_FINAL_CHARS = 2000     # bound the published/accumulated text
        FORCE_TAIL_S = 12.0        # continuous speech with no pause: settle anyway
        # Only re-read the tail once this much NEW audio exists. This MUST be greater
        # than the tick interval to do anything: at 0.5 s with a 0.5 s tick the gate is
        # satisfied every tick and the cost is unchanged (measured, not assumed).
        # 1.0 s halves the tail re-reads while the preview still advances about once a
        # second of speech — and a half-spoken sentence's partial text is unstable
        # anyway, so nothing legible is lost.
        TAIL_STEP_S = 1.0

        while not self._stop_event.is_set():
            if session != self._rec_session:
                return  # this take is over; do not follow the next one
            tick_start = time.time()
            # BUG-7: atomic state read; no _state_lock acquisition in the hot loop.
            if self.state not in ("CHECKING_TAP_HOLD", "RECORDING_HOLD", "RECORDING_TOGGLE"):
                break
            try:
                # 1. Consume only the frames we have not seen yet. The slice is taken
                #    under the lock (references only); concatenate/resample happen
                #    outside it so the PortAudio callback is never blocked.
                with self.pipeline._lock:
                    new_frames = list(self.pipeline.frames[frames_done:])
                frames_done += len(new_frames)
                if new_frames:
                    raw = np.concatenate([np.asarray(f, dtype=np.float32).ravel()
                                          for f in new_frames])
                    if raw.size:
                        chunk = self.pipeline.resample(raw, self.pipeline.device_sr, SR)
                        pcm = chunk if pcm is None else np.concatenate([pcm, chunk])

                if pcm is not None and pcm.size >= SR * 0.35:
                    # 2. Settle every speech run that a real pause has closed.
                    settled = vox_vad.last_confirmed_segment_end(pcm[final_end:], SR)
                    if settled:
                        seg_text = self.chain.transcribe_preview(
                            pcm[final_end: final_end + settled])
                        if seg_text:
                            final_text = (final_text + seg_text).strip()[-MAX_FINAL_CHARS:]
                        final_end += settled
                        # The tail starts fresh after a settle.
                        last_tail_size = 0
                        tail_text = ""

                    tail = pcm[final_end:]
                    # 3. Someone talking without pausing would otherwise leave the whole
                    #    take unsettled and the preview would lag far behind. Settle it
                    #    forcibly past the threshold.
                    if tail.size >= SR * FORCE_TAIL_S:
                        forced = self.chain.transcribe_preview(tail)
                        if forced:
                            final_text = (final_text + forced).strip()[-MAX_FINAL_CHARS:]
                        final_end = pcm.size
                        tail = pcm[final_end:]
                        last_tail_size = 0
                        tail_text = ""

                    # 4. Publish settled text plus the unsettled tail. The tail is only
                    #    re-transcribed once TAIL_STEP_S of NEW audio has arrived, so a
                    #    long sentence is not re-read on every tick while it is being
                    #    spoken; the preview still advances every ~0.5 s of speech.
                    preview = final_text
                    if tail.size >= SR * 0.35:
                        if (not tail_text) or (tail.size - last_tail_size >= SR * TAIL_STEP_S):
                            tail_text = self.chain.transcribe_preview(tail) or ""
                            last_tail_size = tail.size
                        if tail_text:
                            preview = (final_text + tail_text).strip()
                    if preview and preview != last_preview:
                        last_preview = preview
                        # _update_overlay serializes its own writes with _io_lock;
                        # no need to hold the FSM lock for an atomic state read.
                        if self.state in ("CHECKING_TAP_HOLD", "RECORDING_HOLD", "RECORDING_TOGGLE"):
                            self._update_overlay("recording", preview=preview)
            except Exception as e:
                log(f"流式预览处理异常: {e}", "DEBUG")

            # Adaptive pacing: never let a slow tick push the period past ~2x.
            elapsed = time.time() - tick_start
            time.sleep(max(0.0, min(interval, 2.0) - elapsed))

    def _cancel_recording(self):
        log("🚫 录音已撤销 (Esc / 异常触发)", "INFO")
        self.state = "IDLE"
        try:
            self.pipeline.stop_recording()  # discard audio
        except Exception:
            pass

        core.SoundFeedback.play("cancel", enabled=self.cfg.get("sound_effects", True))
        self._update_overlay("cancelled")
        self._update_tray_icon("idle")
        self.update_health()
        # F7: keep the hot-backup so a cancelled take can be re-transcribed manually.
        self._cleanup_rec_backup(keep=True)

    def _finish_recording(self):
        self.state = "TRANSCRIBING"
        self._update_overlay("transcribing")
        self._update_tray_icon("transcribing")
        self.update_health()

        # Stop audio capture
        try:
            audio = self.pipeline.stop_recording()
        except Exception as e:
            log(f"音频采集结束失败: {e}", "ERROR")
            self._update_overlay("error", text="音频采集失败")
            self.state = "IDLE"
            self._update_tray_icon("idle")
            return

        duration = time.monotonic() - self._record_start_time
        log(f"⏹️ 录音结束, 长度: {duration:.2f}s, 帧数: {audio.size}", "INFO")

        # Silence / ultra-short guard: stop Whisper's classic silence-hallucination
        # ("Thank you", "Q.") from EVER reaching paste. These phantom recordings are
        # ~0.4-0.7s; requiring >=0.8s of real speech blocks them at the source instead
        # of trusting post-processing to guess words. Peak was unreliable (a burst of
        # noise spikes peak), so this uses RMS energy as the secondary signal.
        MIN_SPEECH_S = 0.8
        _rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) if audio.size > 0 else 0.0
        if duration < MIN_SPEECH_S or _rms < 0.004:
            too_short = duration < MIN_SPEECH_S
            reason = "too_short" if too_short else "silent"
            # B2: attribute the skip. The pre-AGC peak plus the actual device name is
            # what separates "I tapped too briefly" from "the bound microphone is a
            # dead input" — previously both just said nothing.
            verdict = self._peak_verdict()
            dev = self._dev_name() or "(未知)"
            peak = self._session_peak()
            log(f"跳过转写 ({reason}): 时长={duration:.2f}s rms={_rms:.5f} "
                f"峰值={peak:.5f} 设备=[{self._dev_idx()}] {dev}", "WARN")
            core.record_perf(
                engine="(skipped)", model="", duration_s=duration, latency_s=0.0,
                text="", chars=0, ok=False, error_class=reason,
                error=f"{reason}: rms={_rms:.5f} peak={peak:.5f}",
                used_engine="", attempts=[],
                device=dev, session_peak=peak, reason=reason,
            )
            # Only SPEAK when it looks like a real misconfiguration. A quick tap is
            # normal user behaviour and must not raise a bubble every time, but a take
            # long enough to hold speech that captured nothing deserves to be visible.
            if not too_short and verdict == "silent":
                self._update_overlay(
                    "error",
                    text=f"输入电平≈0（可能选错麦克风）· 当前: {dev[:26]}",
                )
                core.SoundFeedback.play("cancel", enabled=self.cfg.get("sound_effects", True))
            else:
                self._update_overlay("idle")
            self.state = "IDLE"
            self._update_tray_icon("idle")
            self.update_health()
            self._cleanup_rec_backup(keep=False)
            return

        # Start async transcription job
        threading.Thread(target=self._transcription_worker, args=(audio, duration), daemon=True).start()

    def _transcription_worker(self, audio, duration):
        recording_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_path = None
        try:
            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            core.write_wav(wav_path, audio, self.pipeline.TARGET_SR)

            pref_fmt = self.cfg.get("audio", {}).get("upload_format", "flac")
            upload_path, fmt = core.to_flac_or_wav(wav_path, pref_fmt)
            lang = self.cfg.get("language") or self.cfg.get("engines", {}).get("groq", {}).get("language", "zh")
            # A1: pass None so EngineChain builds the prompt itself (static
            # whisper_prompt + bounded context terms). Passing the raw config value
            # here would bypass the lexicon entirely.
            prompt = None

            # Execute transcription via EngineChain
            result = self.chain.transcribe(upload_path, language=lang, prompt=prompt)
            raw_text = result.get("text", "").strip()

            # Post-processing pipeline
            clean_text = core.PostProcessor.process(raw_text, self.cfg)

            if not clean_text or len(clean_text) < 1:
                # B2: separate "we captured silence" from "the model did not understand"
                # and name the microphone that was actually bound.
                verdict = self._peak_verdict()
                dev = self._dev_name() or "(未知)"
                peak = self._session_peak()
                if verdict == "silent":
                    hint = f"输入电平≈0 · 麦克风: {dev[:26]}"
                    err_class = "silent"
                elif verdict == "quiet":
                    hint = f"输入电平偏低，未识别到语音 · 麦克风: {dev[:26]}"
                    err_class = "quiet"
                else:
                    hint = "未识别到语音"
                    err_class = "empty"
                log(f"转写结果为空 (原始={raw_text!r}) 归因={err_class} "
                    f"峰值={peak:.5f} 设备=[{self._dev_idx()}] {dev}", "WARN")
                self._update_overlay("error", text=hint)
                core.record_perf(
                    engine=result.get("engine", "?"), model=result.get("model", ""),
                    duration_s=duration, latency_s=result.get("latency_s", 0.0),
                    text="", chars=0, ok=False, error_class=err_class, error=hint,
                    used_engine=result.get("engine", ""),
                    attempts=result.get("attempts") or [],
                    device=dev, session_peak=peak, reason=err_class,
                )
                return

            # Smart paste & clipboard output (context-aware; complaint #9)
            restore = self.cfg.get("restore_clipboard", True)
            fg = core.foreground_window_title()
            out_res = core.SmartOutput.output(
                clean_text, restore_clipboard=restore,
                foreground_title=fg, cfg=self.cfg,
            )
            if out_res.get("hint"):
                # A failed clipboard write used to pass silently here, which is exactly
                # how the "nothing pastes" bug stayed invisible. Log real failures loudly.
                level = "INFO" if out_res.get("copied") else "ERROR"
                log(f"粘贴结果: method={out_res.get('method')} copied={out_res.get('copied')} "
                    f"pasted={out_res.get('pasted')} · {out_res['hint']}", level)

            # Audio cue & overlay done
            core.SoundFeedback.play("done", enabled=self.cfg.get("sound_effects", True))
            self._update_overlay("done", text=clean_text, hint=out_res.get("hint", ""),
                                 engine=result["engine"], latency=result.get("latency_s"))
            # E6: after the review window elapses, return the overlay state to idle so
            # the bubble disappears even if its own timer raced (daemon is the source of
            # truth). Guarded: only fires if a new recording did not start in between.
            # F6: durations are user-configurable and mirror the overlay's values.
            _ui = self.cfg.get("overlay", {}).get("ui", {})
            _short_ms = max(500, int(_ui.get("done_short_ms", 2000)))
            _long_ms = max(2000, int(_ui.get("done_long_ms", 5000)))
            dur_ms = _short_ms if len(clean_text) <= 40 else _long_ms
            def _push_idle_if_still_done():
                if self.state == "IDLE":
                    try:
                        with open(OVERLAY_STATE_FILE, "r", encoding="utf-8") as f:
                            cur = json.load(f)
                        if cur.get("state") == "done" and cur.get("text") == clean_text:
                            self._update_overlay("idle")
                    except Exception:
                        pass
            _idle_timer = threading.Timer(dur_ms / 1000.0, _push_idle_if_still_done)
            _idle_timer.daemon = True
            _idle_timer.start()
            # F9: log the model id (not just engine family) for per-model tracking.
            log(f"✅ [{result['engine']}] {result.get('model', '')} {result['latency_s']}s · {len(clean_text)}字 · \"{clean_text}\"", "INFO")

            # Persist latest meta
            meta = {
                "recording_id": recording_id,
                "engine": result["engine"],
                "latency_s": result["latency_s"],
                "chars": len(clean_text),
                "text": clean_text,
                "method": out_res.get("method"),
                "copied": out_res["copied"],
                "pasted": out_res["pasted"],
                "duration_s": duration,
                # Confidence proxy (complaint #7): 0..1, low => likely noise/hallucination.
                "confidence": round(core.compute_confidence(clean_text), 3),
                # B1/B2: attribution travels with the record, so the panel and the
                # history archive can EXPLAIN a take instead of only storing its text.
                "slot": result.get("slot", ""),
                "attempts": result.get("attempts") or [],
                "device": self._dev_name(),
                "session_peak": round(self._session_peak(), 5),
                "updated_at": datetime.now().isoformat(),
            }
            core.safe_write_json(core.LATEST_META_FILE, meta)

            # Append to the durable history archive (complaint #6)
            try:
                core.record_transcription(meta)
            except Exception as e:
                log(f"历史存档调用失败: {e}", "DEBUG")

            # Record performance metric (F9 per-model + B1 attribution)
            core.record_perf(
                engine=result["engine"],
                model=result.get("model", ""),
                duration_s=duration,
                latency_s=result["latency_s"],
                text=clean_text,
                chars=len(clean_text),
                confidence=meta.get("confidence"),
                # B1: which slot actually served, and what the chain tried first —
                # this is what makes silent degradation visible.
                ok=True,
                used_engine=result.get("slot", "") or result.get("engine", ""),
                attempts=result.get("attempts") or [],
                # B2: the microphone that captured it and its pre-AGC level.
                device=self._dev_name(),
                session_peak=self._session_peak(),
                reason="ok",
            )

            # Record usage
            self._add_usage(duration)

            # F7: take succeeded — the hot-backup is no longer needed.
            self._cleanup_rec_backup(keep=False)

        except Exception as e:
            # B1: a total chain failure must be RECORDED, with the per-slot detail that
            # says which engine failed and why. Previously this only reached vox.log,
            # so "mimo has been 429 for three days" was invisible in the panel.
            attempts = list(getattr(e, "attempts", []) or [])
            err_class = core.classify_engine_error(e)
            dev = self._dev_name()
            peak = self._session_peak()
            log(f"❌ 转写失败: {e}", "ERROR")
            self._update_overlay("error", text=f"转写失败: {str(e)[:30]}")
            core.record_perf(
                engine="(failed)", model="", duration_s=duration, latency_s=0.0,
                text="", chars=0, ok=False, error_class=err_class, error=str(e),
                used_engine="", attempts=attempts,
                device=dev, session_peak=peak, reason="chain_failed",
            )
            # F7: transcription failed — keep the hot-backup so the take can be retried.
            self._cleanup_rec_backup(keep=True)
        finally:
            self.state = "IDLE"
            self._update_tray_icon("idle")
            self.update_health()
            if wav_path and os.path.exists(wav_path):
                try:
                    os.unlink(wav_path)
                except Exception:
                    pass

    def _add_usage(self, duration_s):
        try:
            u = core.safe_read_json(core.USAGE_FILE, {"total_audio_s": 0.0})
            u["total_audio_s"] = round(u.get("total_audio_s", 0.0) + duration_s, 1)
            u["updated_at"] = datetime.now().isoformat()
            core.safe_write_json(core.USAGE_FILE, u)
        except Exception:
            pass

    # ── Main Event & Watch Loop ───────────────────────────────────
    def _is_hotkey_pressed(self):
        """True while the whole hotkey chord is physically held.

        Two independent sources, because neither alone is sufficient here:

        * `GetAsyncKeyState` (one syscall, no allocation) is the fast path — but
          MEASURED on this machine it reports nothing while another process's window is
          in the foreground, which is what made the hotkey appear to work only when the
          Vox window was focused.
        * the `keyboard` library's global WH_KEYBOARD_LL hook is focus-independent, so
          its held-key set is consulted whenever the fast path says "not pressed".
        """
        groups = self._combo_vks
        if groups and all(groups):
            if _win32.keys_all_down(groups):
                return True
            # The low-level hook is the focus-independent source. Keep the
            # keyboard package's own pressed-state as a second read of the same
            # global listener so a missed/custom event-name normalization cannot
            # make a valid hotkey appear permanently released.
            if self._combo_down_via_hook():
                return True
            if _KB_OK:
                try:
                    return bool(keyboard.is_pressed(self._active_combo))
                except Exception as e:
                    log(f"全局热键状态读取失败: {e}", "DEBUG")
            return False
        if not _KB_OK:
            return False
        try:
            return keyboard.is_pressed(self._active_combo)
        except Exception:
            return False

    def _is_esc_pressed(self):
        return _win32.is_key_down(_win32.VK_ESCAPE)

    def _is_enter_pressed(self):
        return _win32.is_key_down(_win32.VK_RETURN)

    def _on_kb_event(self, event):
        """Track the physically held key set from the GLOBAL hook.

        This hook was already installed but only used to notice "the user typed another
        key during a single-modifier combo". It is the reliable source for hotkey state:
        `WH_KEYBOARD_LL` is delivered system-wide regardless of which window has focus,
        whereas `GetAsyncKeyState` — measured on this machine — reported nothing at all
        while a Chrome window was in the foreground:

            05:29:14  松开  Ctrl=False LWin=False  前台=Chrome        <- 没有记录到按下
            05:30:58  按下  Ctrl=True  LWin=True   前台=Vox 管理控制中心
            05:31:21  按下  Ctrl=True  LWin=True   前台=Vox 管理控制中心

        That is exactly the reported symptom: the hotkey only worked while the Vox window
        itself was in front.
        """
        was_combo_down = self._hook_combo_down
        try:
            name = _norm_kb_name(event.name)
            if event.event_type == "down":
                self._kb_down.add(name)
            else:
                self._kb_down.discard(name)
            self._hook_combo_down = self._combo_down_via_hook()
            if self._hook_combo_down != was_combo_down:
                log(
                    f"全局键盘 hook 捕获热键组合 {'按下' if self._hook_combo_down else '松开'}: "
                    f"{self._active_combo.upper()} (事件={event.event_type}:{event.name})",
                    "DEBUG",
                )
        except Exception:
            pass
        # Detect if another key is typed when hotkey is a single modifier (e.g. user typed Shift+A)
        if self._is_single_modifier and event.event_type == "down":
            try:
                name = (event.name or "").lower()
                if name not in self._combo_tokens and len(name) == 1:
                    self._other_key_pressed_during_combo = True
            except Exception:
                pass

    def _combo_down_via_hook(self):
        """True while every hotkey token is held, according to the global hook.

        Both sides are normalised here rather than relying on `_on_kb_event` having done
        it: the first version compared normalised tokens against a raw held-key set and
        silently returned False for "left ctrl" + "left windows". Normalising inside
        makes the function correct on its own terms; the set is tiny and this runs at
        25 Hz, so the extra comprehension costs nothing measurable.
        """
        try:
            need = self._combo_norm
            if not need:
                return False
            return need.issubset({_norm_kb_name(k) for k in self._kb_down})
        except Exception:
            return False

    def _left_mouse_pressed(self):
        """Poll whether the left mouse button is currently down. Used to suppress the
        classic false trigger where Shift+Click (text selection) starts a recording.

        This previously went through `keyboard.mouse.is_pressed("left")`, whose first
        call permanently installs a global WH_MOUSE_LL hook. That hook allocates a
        Python MoveEvent for EVERY mouse movement on the machine and drains them one
        at a time on a worker thread — i.e. it added Python round-trips to the whole
        system's cursor movement for the daemon's entire lifetime, just to answer one
        boolean. GetAsyncKeyState answers the same question in a single syscall.
        """
        return _win32.is_key_down(_win32.VK_LBUTTON)

    def _warmup_local_engines(self):
        """Optionally preload the local engine — OFF unless explicitly enabled.

        DEFAULT IS OFF, and the reason is measured rather than a matter of taste. A warm
        SenseVoice holds ~3.2 GB of RESIDENT RAM (WorkingSet 21 MB -> 3190 MB measured),
        and unloading it in-process returns almost none of that: del + gc.collect() +
        3x gc + msvcrt._heapmin gave back 16 MB of 3168 MB, because CPython's allocator
        and torch's native pools keep freed memory inside the process. So preloading
        cannot be undone later — the only way to return that RAM is for the process to
        exit. Gated by `engines.sensevoice.warmup`.

        When it does run: the device RESPECTS the config — configured for CPU, warmup
        happens on CPU and never touches the GPU."""
        if not (self.cfg.get("engines", {}).get("sensevoice", {}) or {}).get("warmup", False):
            return
        # Warm ONLY when the local engine is genuinely reachable. Reading
        # core._FASR_OK resolves the lazy import and drags in funasr -> torch, and
        # the warmup then loads ~1GB of weights into RAM.
        #
        # Reachability is defined by what can actually run: the families listed in
        # engine_priority (that is the only list EngineChain walks) plus the
        # streaming interim engine. Gating on gpu_backed_engines() alone was wrong
        # for a CPU slot: 'sensevoice-cpu' as a fallback or as the live-preview
        # engine is very much in use, yet it reported as "not GPU-backed" and so
        # was never warmed — costing 12.5s in the middle of the user's first take.
        try:
            reachable = set()
            for slot in self.cfg.get("engine_priority", []) or []:
                fam, _ = core.split_slot(slot)
                reachable.add(fam)
            interim = (self.cfg.get("streaming", {}) or {}).get("interim_engine") or ""
            if interim:
                fam, _ = core.split_slot(interim)
                reachable.add(fam)
            if "sensevoice" not in reachable:
                return
        except Exception:
            return
        try:
            prio = self.cfg.get("engine_priority", [])
            warmed = []
            for key in prio:
                fam, variant = core.EngineChain(self.cfg).resolve_spec(key)
                if fam == "sensevoice" and core._FASR_OK:
                    try:
                        if variant in ("cuda", "cpu"):
                            dev = variant
                        else:
                            sv = self.cfg.get("engines", {}).get("sensevoice", {})
                            dev = str(sv.get("device", "auto")).lower()
                            if dev not in ("cuda", "cpu"):
                                # Probe for a GPU only NOW. This must not be an
                                # eagerly-evaluated dict default: doing so made even a
                                # pure CPU slot import ctranslate2 and touch the driver.
                                dev = "cuda" if core.get_cuda_device_info()[0] else "cpu"
                        eng = core.SenseVoiceEngine(device=dev)
                        eng._get_model()
                        warmed.append(f"sensevoice({dev})")
                    except Exception as e:
                        log(f"SenseVoice 预热失败: {e}", "WARN")
            if warmed:
                log(f"本地引擎预热完成: {', '.join(warmed)}")
        except Exception as e:
            log(f"引擎预热异常: {e}", "WARN")

    def _offload_worker(self):
        """G7: drop all local ASR models off the GPU to free VRAM (daemon keeps
        running; models reload lazily on the next transcription per config)."""
        try:
            what = core.offload_gpu_models()
            log(f"🧹 GPU offload 完成: {what} — 显存已释放", "INFO")
            self.update_health()
            if getattr(self, "tray", None) and _TRAY_OK:
                try:
                    self.tray.notify(f"本地模型已移出 GPU: {what}，显存已释放", "Vox 显存释放")
                except Exception:
                    pass
        except Exception as e:
            log(f"GPU offload 失败: {e}", "ERROR")

    def watch_loop(self):
        log(f"Vox 守护进程主循环已启动. 全局热键: [{self._active_combo.upper()}]")
        # G6: background warmup so first dictation is fast, not blocked on model load.
        import threading as _th
        _th.Thread(target=self._warmup_local_engines, name="vox-warmup", daemon=True).start()
        log("交互说明: 长按松开即出字；短按单击免手持录音；按 Esc 随时撤销。")
        # F7: a leftover hot-backup means the previous session crashed mid-recording —
        # tell the user the audio is safe and can be re-transcribed.
        if os.path.exists(REC_BACKUP_FILE):
            log(f"🛡️ 检测到上次会话遗留的录音热备份: {REC_BACKUP_FILE} (上次录音中断，音频已保留)", "WARN")

        if _KB_OK:
            try:
                self._hook = keyboard.hook(self._on_kb_event)
                self._hook_ready = self._hook is not None
                if self._hook_ready:
                    log("✅ 全局键盘 hook 已安装；正在等待按键事件", "INFO")
                else:
                    log("⚠️ 全局键盘 hook 返回空句柄，前台外热键可能不可用", "WARN")
            except Exception as e:
                self._hook = None
                self._hook_ready = False
                log(f"安装低级键盘钩子失败: {e}", "WARN")
        else:
            self._hook_ready = False
            log("⚠️ keyboard 模块不可用；前台外热键缺少全局 hook 兜底", "WARN")
        self.update_health()

        while not self._stop_event.is_set():
            # Control-file sentinels are polled on a slower cadence. They used to cost
            # two stat syscalls every 25 ms (~80/s) for the daemon's whole lifetime;
            # 500 ms of extra latency on stop/offload is imperceptible.
            self._ctrl_tick += 1
            if self._ctrl_tick >= 20:
                self._ctrl_tick = 0
                if os.path.exists(STOP_FILE):
                    log("检测到停止信号，正在退出...", "INFO")
                    try:
                        os.unlink(STOP_FILE)
                    except Exception:
                        pass
                    break

                # G7: offload control file — free VRAM without stopping the daemon.
                if os.path.exists(OFFLOAD_FILE):
                    try:
                        os.unlink(OFFLOAD_FILE)
                    except Exception:
                        pass
                    log("🧹 收到 offload 指令，正在卸载 GPU 上的本地音频模型...", "INFO")
                    threading.Thread(target=self._offload_worker, name="vox-offload", daemon=True).start()

            # Enforce audio.max_recording_seconds. The key existed in config.json but
            # NOTHING ever read it, so a forgotten hands-free take grew the audio
            # buffer without bound — the single most likely source of the memory
            # growth this tool was seeing. The pipeline also hard-caps the buffer;
            # this is the graceful end-of-take path.
            if self.state in ("CHECKING_TAP_HOLD", "RECORDING_HOLD", "RECORDING_TOGGLE"):
                elapsed = (time.monotonic() - self._record_start_time) if self._record_start_time else 0.0
                if self.pipeline.limit_reached or elapsed > self.pipeline.max_seconds:
                    log(f"⏱️ 录音达到上限 {self.pipeline.max_seconds:.0f}s，自动结束本轮转写", "WARN")
                    self._finish_recording()

            pressed = self._is_hotkey_pressed()
            # B6: reset each iteration. Set to True when a toggle-mode finish must wait for
            # hotkey release; that wait is performed AFTER the state lock is released below.
            release_wait = False

            # State Machine Dispatch
            with self._state_lock:
                if self.state == "IDLE":
                    if pressed:
                        # If the left mouse button is already held when the modifier goes
                        # down, this is a selection gesture (Shift+Click), not dictation —
                        # ignore it so no recording starts.
                        if not self._left_mouse_pressed():
                            self._press_start_time = time.monotonic()
                            if self._hotkey_mode == "hold_only":
                                self._start_recording(mode="RECORDING_HOLD", hint="口述中 (松手完成)")
                            elif self._hotkey_mode == "toggle_only":
                                self._start_recording(mode="RECORDING_TOGGLE", hint="免手持录音中 (按热键/回车完成)")
                            else:  # adaptive
                                self._start_recording(mode="CHECKING_TAP_HOLD", hint="松手完成 · 单击锁定")

                elif self.state == "CHECKING_TAP_HOLD":
                    # Esc cancels immediately
                    if self._is_esc_pressed():
                        self._cancel_recording()
                    # If normal typing OR a left-click detected during single modifier,
                    # cancel (text selection / typing is not dictation).
                    elif self._is_single_modifier and (self._other_key_pressed_during_combo or self._left_mouse_pressed()):
                        log("检测到鼠标点击/正常按键输入，取消单修饰键录音触发", "DEBUG")
                        self._cancel_recording()
                    # Held longer than hold_thresh -> firmly in HOLD mode
                    elif time.monotonic() - self._press_start_time >= self._hold_thresh:
                        self.state = "RECORDING_HOLD"
                        self._update_overlay("recording", hint="口述中 (松手完成)")
                    # Released before hold_thresh -> entered TOGGLE (hands-free) mode
                    elif not pressed:
                        self.state = "RECORDING_TOGGLE"
                        # BUG-18: this release is what arms "press again to
                        # finish". toggle_only arms on its own release below.
                        self._toggle_armed = True
                        self._update_overlay("recording", hint="免手持录音中 (按热键/回车完成)")
                        log("💡 进入免手持录音模式 (按热键/回车结束)", "DEBUG")

                elif self.state == "RECORDING_HOLD":
                    if self._is_esc_pressed():
                        self._cancel_recording()
                    elif not pressed:
                        # User released hotkey -> finish
                        self._finish_recording()

                elif self.state == "RECORDING_TOGGLE":
                    if self._is_esc_pressed():
                        self._cancel_recording()
                    elif not pressed:
                        # BUG-18: the press that entered TOGGLE must be fully
                        # released before another press can finish the take.
                        self._toggle_armed = True
                    elif self._is_enter_pressed() or (self._toggle_armed and pressed):
                        # User pressed hotkey again (after release) or hit Enter -> finish
                        self._finish_recording()
                        # B6: defer release-detection until AFTER the state lock is released,
                        # else this busy-wait (up to several seconds during a key hold) blocks
                        # _streaming_worker (frozen preview) and tray start/stop (hung buttons).
                        release_wait = True

            # B6: wait for hotkey release OUTSIDE the state lock so streaming preview and
            # tray start/stop are never blocked by a multi-second key hold.
            if release_wait:
                self._wait_for_hotkey_release()

            time.sleep(0.025)

    def _wait_for_hotkey_release(self):
        """B6: block until the active hotkey is released (or stop requested). Deliberately
        runs WITHOUT holding _state_lock, so _streaming_worker (live preview) and the tray
        start/stop controls are never frozen by a multi-second key hold."""
        while self._is_hotkey_pressed() and not self._stop_event.is_set():
            time.sleep(0.02)

    def run(self):
        # 0. Single-Instance lock (Strictly one daemon permitted). Normally it was
        #    acquired up front in __main__ BEFORE the heavy VoxDaemon construction;
        #    acquire here as a fallback for callers that construct directly.
        if getattr(self, "_instance_lock", None) is None:
            self._instance_lock = acquire_single_instance_lock()
        if not self._instance_lock:
            log("⚠️ 检测到已有另一个 Vox 守护进程正在运行中！本实例自动退出，杜绝重复监听与双重粘贴。", "WARN")
            return

        # Belt-and-suspenders: even if the mutex/port somehow passed twice, refuse to
        # start when another vox_daemon is actually alive (defends against races / a
        # pre-patch shadow instance).
        others = find_existing_daemon_pids()
        if others:
            log(f"⚠️ 发现残留 Vox 守护进程 PID={others}；拒绝启动以避免双开。", "WARN")
            try:
                self._instance_lock.release()
            except Exception:
                pass
            self._instance_lock = None
            return

        # Write PID
        try:
            with open(core.PID_FILE, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

        # Clear stale stop file
        if os.path.exists(STOP_FILE):
            try:
                os.unlink(STOP_FILE)
            except Exception:
                pass

        # Preflight audio device
        dev_idx, dev_name = self.pipeline.resolve_device(self.cfg.get("audio", {}).get("preferred_device"))
        log(f"🎙️ 麦克风就绪: [{dev_idx}] {dev_name}")
        # Seed the hot-plug baseline so _device_watch only fires on actual changes.
        self._bound_device = {"index": dev_idx, "friendly_name": dev_name}

        self.update_health()
        self.spawn_overlay()
        self._setup_tray()

        # Start config hot-reload poller (B2 fix) — applies GUI changes within ~1s
        threading.Thread(target=self._config_poller, daemon=True).start()

        # Start input-device hot-plug watcher (complaint #5 / audit B5)
        threading.Thread(target=self._device_watch, daemon=True).start()

        # Publish mic levels to the overlay from here, NOT from the PortAudio callback.
        threading.Thread(target=self._level_publisher, name="vox-level-pub", daemon=True).start()

        try:
            self.watch_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        log("守护进程正在关闭...", "INFO")
        self._stop_event.set()
        if self._hook and _KB_OK:
            try:
                keyboard.unhook(self._hook)
            except Exception:
                pass
        self.kill_overlay()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        if os.path.exists(core.PID_FILE):
            try:
                os.unlink(core.PID_FILE)
            except Exception:
                pass
        if hasattr(self, "_instance_lock") and self._instance_lock:
            try:
                self._instance_lock.release()
            except Exception:
                pass
            self._instance_lock = None
        self._update_overlay("idle")


def install_exception_logging():
    """Route uncaught exceptions — thread ones included — into vox.log.

    A pythonw process has no console and the daemon's stdout/stderr were previously sent
    to DEVNULL, so an exception raised inside a worker thread left NO trace anywhere.
    The `keyboard` hook runs on its own thread, so "the hotkey stopped responding" was
    exactly the kind of failure this hid: the log simply ended, and nothing said why.

    threading.excepthook requires 3.8+, which this project already targets.
    """
    def _thread_hook(args):
        try:
            name = getattr(getattr(args, "thread", None), "name", "?")
            log(f"未捕获异常 (线程 {name}): "
                + "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                     args.exc_traceback)), "ERROR")
        except Exception:
            pass

    def _main_hook(exc_type, exc_value, exc_tb):
        try:
            log("未捕获异常 (主线程): "
                + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
                "ERROR")
        except Exception:
            pass

    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass
    try:
        sys.excepthook = _main_hook
    except Exception:
        pass


if __name__ == "__main__":
    install_exception_logging()
    if len(sys.argv) > 1 and sys.argv[1] == "benchmark":
        wav = sys.argv[2] if len(sys.argv) > 2 else "jfk.wav"
        cfg = core.ConfigManager.load()
        chain = core.EngineChain(cfg)
        print(f"Benchmarking STT engines on: {wav}")
        for eng_name in cfg.get("engine_priority", ["groq", "local"]):
            try:
                t0 = time.time()
                chain.priority = [eng_name]
                res = chain.transcribe(wav, "auto")
                print(f"[{eng_name:7s}] {res['latency_s']:5.3f}s: {res['text'][:60]}")
            except Exception as e:
                print(f"[{eng_name:7s}] 失败: {e}")
    else:
        # Acquire the single-instance lock BEFORE constructing VoxDaemon. The
        # constructor builds AudioPipeline / EngineChain, which can block for
        # several seconds (PortAudio enumeration, model warmup). If the lock
        # were only taken inside run(), a losing racer from a double-launch
        # could get stuck in the constructor and linger forever as a
        # single-threaded zombie that never exits (observed in the field).
        early_lock = acquire_single_instance_lock()
        if not early_lock:
            log("⚠️ 检测到已有另一个 Vox 守护进程正在运行中！本实例自动退出，杜绝重复监听与双重粘贴。", "WARN")
            sys.exit(0)
        daemon = VoxDaemon()
        daemon._instance_lock = early_lock
        daemon.run()
