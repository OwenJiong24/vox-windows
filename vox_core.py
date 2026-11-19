#!/usr/bin/env python3
"""
Vox Core — Core audio pipeline, STT engines, post-processing, and smart output.
Engineered for Windows with high reliability, low latency, and zero focus-stealing.
"""

import os
import sys
import io
import json
import re
import time
import math
import struct
import wave
import shutil
import base64
import threading
import subprocess
import tempfile
import copy
import importlib
from datetime import datetime

# Credential-at-rest helpers for the Groq API key (audit B12).
from crypto_utils import maybe_encrypt, maybe_decrypt
import vox_win32 as _win32

# numpy is deliberately NOT imported at module scope — see _np(). Importing it here
# cost ~60 ms and 192 modules in EVERY process, including the always-on overlay, which
# uses numpy exactly zero times. All 11 uses live in AudioPipeline / write_wav, so it
# is resolved on first use instead.

# ─── CUDA & High-DPI Environment Setup ─────────────────────────────
def _setup_cuda_env():
    site_pkg = os.path.join(sys.prefix, "Lib", "site-packages")
    for sub in ["nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"]:
        p = os.path.join(site_pkg, *sub.split("/"))
        if os.path.exists(p):
            try:
                os.add_dll_directory(p)
            except Exception:
                pass
            if p not in os.environ.get("PATH", ""):
                os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")

_setup_cuda_env()

def init_dpi_awareness():
    """Initializes per-monitor DPI awareness V2 early before any GUI toolkit initializes."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    except Exception:
        pass

init_dpi_awareness()

def get_dpi_scale():
    """Returns the Windows DPI scaling factor (e.g. 1.0 for 100%, 2.25 for 4K 225%)."""
    init_dpi_awareness()
    try:
        import ctypes
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return max(1.0, round(float(dpi) / 96.0, 2))
    except Exception:
        return 1.0

def get_screen_workarea():
    """Returns (left, top, width, height) of the primary monitor work area excluding taskbar."""
    init_dpi_awareness()
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    try:
        import ctypes
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)
        return 0, 0, w, h
    except Exception:
        return 0, 0, 1920, 1080

def get_cuda_device_info():
    """Checks if an NVIDIA CUDA GPU is available for local ASR (SenseVoice) acceleration.

    NOTE: this probes through ctranslate2 on purpose — it is a cheap CUDA runtime
    query, whereas importing torch here would drag the whole 6.5 GB stack onto the
    cloud-only startup path that `gpu_backed_engines` exists to keep clean."""
    try:
        import ctranslate2
        count = ctranslate2.get_cuda_device_count()
        if count > 0:
            return True, "NVIDIA GPU (CUDA)"
    except Exception:
        pass
    return False, "CPU 运算"

# ─── Optional / Platform Dependencies ──────────────────────────────
# PERF (2026-10): `sounddevice` (PortAudio, 120ms / 109 modules), the `groq` SDK
# (244ms / 475), `requests` (116ms / 324) and `zhconv` (163ms / 229) used to be
# imported eagerly right here. Each one is needed by exactly ONE feature
# (recording / Groq ASR / MiMo ASR / 繁简转换), so they are registered as LAZY
# deps below (see _LAZY_SYMBOLS / _LAZY_ATTRS) and resolved on first real use.
# The old module-level flags `_SD_OK` / `_GROQ_OK` / `_REQ_OK` / `_HAS_ZHCONV`
# keep their exact semantics — reading one imports the dep once (that is the
# point) and returns the same bool it always returned.

try:
    import win32clipboard, win32con, win32api, win32gui
    _WIN_OK = True
except Exception:
    win32clipboard = win32con = win32api = win32gui = None
    _WIN_OK = False
# _WIN_OK is now only a leftover compatibility flag (test_suite skips a Win32 test
# on it). NOTHING functional depends on pywin32 any more: clipboard access, Ctrl+V
# injection, foreground-window lookup and tray promotion all use either ctypes
# (vox_win32) or the stdlib. Do not reintroduce a hard dependency on it.

try:
    import winsound
    _WINSOUND_OK = True
except Exception:
    winsound = None
    _WINSOUND_OK = False

# ─── Heavy optional dependencies: LAZY (perf) ─────────────────────
# PERF (2026-09): these pull torch + transformers + funasr + nagisa and
# cost ~7.0s of import time TOGETHER. They used to be imported eagerly here at
# module scope, but Vox starts FOUR python processes (launcher check_env,
# daemon, overlay, GUI) and every one of them imports vox_core — so a single
# 7s cost was paid four times, making a double-click-to-usable launch take
# ~16s before it even opened a microphone.
#
# PERF (2026-10): the four "medium" deps joined them for the same reason —
# groq (244ms/475 modules), zhconv (163ms/229), sounddevice (120ms/109) and
# requests (116ms/324) were each paid by every process, including the overlay
# (a Tk canvas that needs none of them) and the launcher. sqlite3 (3ms/82) is
# lazy as well; it is only needed by the history archive.
#
# They are now resolved on FIRST USE instead of at import time:
#   * internal call sites go through _dep("funasr") / _dep("sounddevice") / ...
#   * every external `core._FASR_OK` / `core._SD_OK` / `core._GROQ_OK` /
#     `core._REQ_OK` / `core._HAS_ZHCONV` / `core.AutoModel` / `core.sd` /
#     `core.groq_sdk` reference keeps working unchanged thanks to the
#     module-level __getattr__ (PEP 562) below.
_LAZY_DEPS = {}
_LAZY_ERRORS = {}


def _dep(name):
    """Import one optional dependency once; return (symbol, ok).

    name is the module name; the returned symbol is the one object the callers
    actually use — the module itself unless _LAZY_SYMBOLS names a member (e.g.
    `funasr` -> AutoModel). A missing package is not fatal: ok=False and the
    caller degrades exactly as it did when the import was eager (the engine
    falls to the next priority, AudioPipeline raises its RuntimeError, ...).
    """
    if name in _LAZY_DEPS:
        return _LAZY_DEPS[name]
    attr = _LAZY_SYMBOLS.get(name)
    try:
        mod = importlib.import_module(name)
        val = getattr(mod, attr) if attr else mod
        ok = True
    except Exception as _e:
        val, ok = None, False
        _LAZY_ERRORS[name] = _e
    _LAZY_DEPS[name] = (val, ok)
    return val, ok


_LAZY_SYMBOLS = {
    "funasr": "AutoModel",              # G2 SenseVoice (FunASR) — the single local engine
    "torch": None,                      # pulled in by funasr
    "sounddevice": None,                # PortAudio recording (AudioPipeline)
    "groq": None,                       # Groq cloud ASR SDK
    "requests": None,                   # MiMo cloud ASR HTTP client
    "zhconv": None,                     # 繁简转换 post-processing
    "sqlite3": None,                    # transcription history archive (stdlib)
    "numpy": None,                      # audio DSP only — see _np()
}


def _dep_ok(name):
    """True when the optional dependency is importable (loads it once)."""
    return _dep(name)[1]


def _dep_err(name):
    """The exception raised while importing `name` (imports it once first)."""
    _dep(name)
    return _LAZY_ERRORS.get(name)


def _np():
    """Resolve numpy on first use.

    numpy was the last eager heavy import: ~60 ms and 192 modules, and for the
    always-on overlay it also meant ~16 MB resident plus a BLAS thread pool that the
    overlay never touches (it uses numpy zero times). Only AudioPipeline and write_wav
    need it, so it is resolved per call — a dict hit after the first — instead of at
    module import.
    """
    np_mod, ok = _dep("numpy")
    if not ok:
        raise RuntimeError("numpy 未安装 (pip install numpy)")
    return np_mod

# attribute name -> (dep module, is_flag)
#   is_flag True  -> the old bool flag (via _dep_ok)
#   is_flag False -> the module object itself (the old eager alias)
_LAZY_ATTRS = {
    "AutoModel": ("funasr", False),
    "_FASR_OK": ("funasr", True),
    "torch": ("torch", False),
    "sd": ("sounddevice", False),
    "_SD_OK": ("sounddevice", True),
    "groq_sdk": ("groq", False),
    "_GROQ_OK": ("groq", True),
    "requests": ("requests", False),
    "_REQ_OK": ("requests", True),
    "zhconv": ("zhconv", False),
    "_HAS_ZHCONV": ("zhconv", True),
    "sqlite3": ("sqlite3", False),
    "np": ("numpy", False),
    "_NP_OK": ("numpy", True),
}

# attribute name -> dep module whose import exception should be exposed
_LAZY_ERR_ATTRS = {"_SD_ERR": "sounddevice"}


def __getattr__(name):
    """PEP 562 lazy attribute hook — resolves the optional deps on first touch."""
    err_dep = _LAZY_ERR_ATTRS.get(name)
    if err_dep is not None:
        out = _dep_err(err_dep)
        globals()[name] = out          # cache: next lookup is a plain global hit
        return out
    spec = _LAZY_ATTRS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    dep, is_flag = spec
    val, ok = _dep(dep)
    out = ok if is_flag else val
    globals()[name] = out          # cache: next lookup is a plain global hit
    return out


# ─── Directory Paths ──────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "vox.log")
PID_FILE = os.path.join(SCRIPT_DIR, "vox.pid")
HEALTH_FILE = os.path.join(SCRIPT_DIR, "vox_health.json")
LATEST_META_FILE = os.path.join(SCRIPT_DIR, "latest.json")
USAGE_FILE = os.path.join(SCRIPT_DIR, "vox_usage.json")
REPLACEMENTS_PATH = os.path.join(SCRIPT_DIR, "replacements.json")
PERF_HISTORY_FILE = os.path.join(SCRIPT_DIR, "vox_perf_history.json")

# ─── Transcription History Archive (complaint #6) ─────────────────
HISTORY_DIR = os.path.join(SCRIPT_DIR, "history")
HISTORY_FILE = os.path.join(HISTORY_DIR, "transcriptions.jsonl")
HISTORY_DB = os.path.join(HISTORY_DIR, "transcriptions.db")


def _looks_garbage(text):
    """Heuristic error/confidence proxy (lightweight stand-in for an ASR error-rate
    metric — see complaint #7). High ratio of non-alphanumeric characters vs. words
    tends to indicate noise/hallucination rather than real speech."""
    text = str(text or "").strip()
    if not text:
        return True
    alnum = sum(1 for c in text if c.isalnum())
    ratio = alnum / len(text)
    # Whitespace/punctuation-only runs are suspicious; also flag pure kana-ish bursts.
    return ratio < 0.45


def compute_confidence(text):
    """Lightweight ASR error-rate / confidence proxy (complaint #7). Returns a value in
    [0, 1]: the fraction of alphanumeric characters. Real speech (Chinese, English,
    mixed) is mostly alphanumeric (~1.0); silence-hallucinations and noise tend to be
    punctuation/symbol/whitespace runs (~0.0). Reused by _looks_garbage so the two stay
    consistent. Not a true WER, but a cheap signal good enough to flag low-confidence
    transcriptions in the bubble and history table."""
    s = str(text or "").strip()
    if not s:
        return 0.0
    alnum = sum(1 for c in s if c.isalnum())
    return min(1.0, max(0.0, alnum / len(s)))


# Columns shared by record/read/search/get/export. Order matters for the positional SQL.
_HISTORY_COLS = (
    "recording_id", "timestamp", "engine", "text", "chars", "latency_s",
    "duration_s", "method", "copied", "pasted", "garbage_flag", "confidence",
)
_HISTORY_PLACEHOLDERS = ", ".join(["?"] * len(_HISTORY_COLS))


def _history_conn():
    """Open a history SQLite connection with a busy timeout so concurrent readers/writers
    (transcription worker vs GUI) don't fail with 'database is locked'."""
    try:
        sqlite3, sql_ok = _dep("sqlite3")   # LAZY: stdlib, but only history needs it
        if not sql_ok:
            return None
        os.makedirs(HISTORY_DIR, exist_ok=True)
        con = sqlite3.connect(HISTORY_DB, timeout=5.0)
        con.execute("PRAGMA busy_timeout = 5000")
        return con
    except Exception:
        return None


def _init_history_db(con):
    if con is None:
        return False
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS transcriptions ("
            "recording_id TEXT, timestamp TEXT, engine TEXT, text TEXT, chars INTEGER, "
            "latency_s REAL, duration_s REAL, method TEXT, copied INTEGER, pasted INTEGER, "
            "garbage_flag INTEGER, confidence REAL)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_hts ON transcriptions(timestamp)")
        con.commit()
        return True
    except Exception:
        return False


def _row_to_values(row):
    return (
        row.get("recording_id"), row.get("timestamp"), row.get("engine"), row.get("text"),
        int(row.get("chars") or 0), float(row.get("latency_s") or 0.0),
        float(row.get("duration_s") or 0.0), row.get("method"),
        1 if row.get("copied") else 0, 1 if row.get("pasted") else 0,
        1 if row.get("garbage_flag") else 0,
        float(row.get("confidence") or 0.0),
    )


def record_transcription(meta):
    """Persist a finalized transcription to the SQLite history archive (complaint #6).
    Best-effort; never raises so it can't interrupt the paste flow."""
    try:
        row = {
            "recording_id": meta.get("recording_id"),
            "timestamp": datetime.now().isoformat(),
            "engine": meta.get("engine"),
            "text": meta.get("text", ""),
            "chars": meta.get("chars", 0),
            "latency_s": meta.get("latency_s"),
            "duration_s": meta.get("duration_s"),
            "method": meta.get("method"),
            "copied": bool(meta.get("copied")),
            "pasted": bool(meta.get("pasted")),
            "garbage_flag": _looks_garbage(meta.get("text", "")),
            "confidence": round(float(meta.get("confidence") or compute_confidence(meta.get("text", ""))), 3),
        }
        con = _history_conn()
        if con is not None:
            try:
                if _init_history_db(con):
                    con.execute(
                        f"INSERT INTO transcriptions ({', '.join(_HISTORY_COLS)}) VALUES ({_HISTORY_PLACEHOLDERS})",
                        _row_to_values(row),
                    )
                    con.commit()
            finally:
                con.close()
        return row
    except Exception as e:
        log(f"历史存档写入失败: {e}", "DEBUG")
        return None


def _migrate_jsonl_into_db(con):
    """Backfill the SQLite table from legacy JSONL records (one-time, best-effort)."""
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        prepared = []
        for ln in lines:
            try:
                prepared.append(_row_to_values(json.loads(ln)))
            except Exception:
                continue
        if prepared:
            con.executemany(
                f"INSERT INTO transcriptions ({', '.join(_HISTORY_COLS)}) VALUES ({_HISTORY_PLACEHOLDERS})",
                prepared,
            )
            con.commit()
    except Exception:
        pass


def read_history(limit=200):
    """Read archived transcriptions newest-first (most recent at index 0). Backfills any
    legacy JSONL rows into the DB on first read. Returns a list of dicts; [] on error."""
    out = []
    con = _history_conn()
    if con is None:
        return out
    try:
        if not _init_history_db(con):
            return out
        # Backfill legacy JSONL once, only when the table is still empty.
        if con.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0] == 0:
            _migrate_jsonl_into_db(con)
        cur = con.execute(
            f"SELECT {', '.join(_HISTORY_COLS)} FROM transcriptions ORDER BY timestamp DESC LIMIT ?",
            (int(limit),),
        )
        for rec in cur.fetchall():
            d = dict(zip(_HISTORY_COLS, rec))
            d["copied"] = bool(d["copied"])
            d["pasted"] = bool(d["pasted"])
            d["garbage_flag"] = bool(d["garbage_flag"])
            out.append(d)
    except Exception:
        out = []
    finally:
        con.close()
    return out


def search_history(query, limit=100):
    """Case-insensitive substring search over stored text/engine (newest-first)."""
    out = []
    con = _history_conn()
    if con is None:
        return out
    try:
        if not _init_history_db(con):
            return out
        like = f"%{str(query or '').strip()}%"
        cur = con.execute(
            f"SELECT {', '.join(_HISTORY_COLS)} FROM transcriptions "
            f"WHERE text LIKE ? OR engine LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (like, like, int(limit)),
        )
        for rec in cur.fetchall():
            d = dict(zip(_HISTORY_COLS, rec))
            d["copied"] = bool(d["copied"])
            d["pasted"] = bool(d["pasted"])
            d["garbage_flag"] = bool(d["garbage_flag"])
            out.append(d)
    except Exception:
        out = []
    finally:
        con.close()
    return out


def get_history(recording_id):
    """Return a single archived transcription by recording_id, or None."""
    con = _history_conn()
    if con is None:
        return None
    try:
        if not _init_history_db(con):
            return None
        row = con.execute(
            f"SELECT {', '.join(_HISTORY_COLS)} FROM transcriptions WHERE recording_id = ?",
            (recording_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(zip(_HISTORY_COLS, row))
        d["copied"] = bool(d["copied"])
        d["pasted"] = bool(d["pasted"])
        d["garbage_flag"] = bool(d["garbage_flag"])
        return d
    except Exception:
        return None
    finally:
        con.close()


def export_history_csv(path):
    """Write all archived transcriptions to a CSV file (UTF-8 with BOM for Excel).
    Returns the path written, or None on failure."""
    import csv
    rows = read_history(limit=10000)
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(_HISTORY_COLS))
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return path
    except Exception as e:
        log(f"历史导出失败: {e}", "DEBUG")
        return None


def clear_history():
    """Delete the whole history archive (SQLite table + legacy JSONL file)."""
    con = _history_conn()
    if con is not None:
        try:
            _init_history_db(con)
            con.execute("DELETE FROM transcriptions")
            con.commit()
        except Exception:
            pass
        finally:
            con.close()
    try:
        if os.path.exists(HISTORY_FILE):
            os.remove(HISTORY_FILE)
    except Exception as e:
        log(f"清空历史失败: {e}", "DEBUG")


# ─── Process Liveness (single-instance & orphan cleanup) ──────────
def _process_alive(pid):
    """True if `pid` maps to a RUNNING process.

    Delegates to vox_win32.process_alive, which uses GetExitCodeProcess. The previous
    implementation called OpenProcess alone, which keeps succeeding after a process has
    exited because Windows retains the process object while any handle to it is open
    (Python's subprocess holds one). That made killed processes look alive forever.
    """
    try:
        return _win32.process_alive(pid)
    except Exception:
        return False


def _scan_cmdline_procs(target_substr):
    """ALIVE PIDs whose process command line contains `target_substr`, excluding self.

    Queries Win32_Process.CommandLine, which is reliably populated in this environment.
    The previous scan matched against the raw `tasklist /fo csv` ROW TEXT -- that output
    carries no command-line column, so the match could never succeed and
    find_existing_daemon_pids() / kill_all_vox_*() were effectively inert. We filter to
    python/pythonw images and substring-match the command line. The script is passed as a
    single argv element so the outer shell never interpolates ``$_`` before PowerShell sees
    it (typing those tokens inline corrupts them).
    """
    mine = os.getpid()
    found = []
    needle = str(target_substr).replace("'", "''")
    try:
        ps_cmd = (
            "$n='" + needle + "'; "
            "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Name -like 'python*.exe' -and "
            "$_.CommandLine -ne $null -and "
            "$_.CommandLine.ToLower().Contains($n.ToLower()) } | "
            "ForEach-Object { $_.Id }"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            text=True, encoding="utf-8", errors="replace",
            timeout=10,  # WMI/CIM can stall for minutes on a busy machine;
                         # an unbounded call once hung the launcher 3+ min.
        )
    except Exception:
        return found
    for tok in out.split():
        tok = tok.strip().strip('"')
        if tok.isdigit():
            pid = int(tok)
            if pid != mine:
                found.append(pid)
    return found


def _self_daemon_pid():
    """Live, non-self pid the daemon self-registered in PID_FILE, else None. OpenProcess
    liveness here is reliable even where command-line enumeration is not."""
    mine = os.getpid()
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pf = int(f.read().strip())
    except Exception:
        return None
    return pf if (pf != mine and _process_alive(pf)) else None


def find_existing_daemon_pids():
    """ALIVE PIDs running vox_daemon.py (excluding self). Single-instance guard used by
    both the daemon startup check and the GUI stop/restart path.

    Combines two independent signals so the guard holds even when command-line
    enumeration is unavailable: Win32_Process.CommandLine substring match, plus the
    daemon's self-registered PID_FILE liveness probe. The pid-file signal is only
    trusted when its command line really mentions vox_daemon.py (or when enumeration is
    unavailable) — otherwise a recycled pid (reused by an unrelated process after a
    crash) would block startup forever with a stale PID_FILE."""
    found = {p for p in _scan_cmdline_procs("vox_daemon.py") if _process_alive(p)}
    pf = _self_daemon_pid()
    if pf is not None:
        # BUG-21: distinguish "this pid does not exist" from "enumeration is
        # unavailable". The old code only looked at whether the CommandLine string
        # was empty, so a STALE pid file left behind by a killed daemon (very common
        # — the process object can look alive while no such cmdline exists) was
        # treated as "enumeration unavailable -> trust the pid file". run.bat then
        # concluded "a daemon is already running", showed its menu instead of
        # starting one, and the user was left with NO daemon at all.
        trust_pf = True
        try:
            import subprocess as _sp
            probe = ("$p = Get-CimInstance Win32_Process -Filter \"ProcessId={0}\" "
                     "-ErrorAction SilentlyContinue; if ($null -eq $p) {{ "
                     "'__VOX_NO_SUCH_PID__' }} else {{ $p.CommandLine }}").format(pf)
            cl = _sp.check_output(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", probe],
                text=True, encoding="utf-8", errors="replace", timeout=8).strip()
            if "__VOX_NO_SUCH_PID__" in cl:
                trust_pf = False           # pid really is gone -> ignore stale file
            elif cl:                        # enumeration available -> require a match
                trust_pf = "vox_daemon" in cl.lower()
            # empty output here would mean the shell produced nothing at all, which
            # is indistinguishable from "unavailable" -> keep the fallback.
        except Exception:
            trust_pf = True  # enumeration unavailable -> keep the pid-file fallback
        if trust_pf:
            found.add(pf)
    return sorted(found)


def clear_stale_pid_file():
    """Delete vox.pid when it no longer points at a live daemon.

    Start-up hygiene (BUG-21): a stale pid file makes the launcher refuse to start
    a new daemon, which presents as "Vox is running" while nothing is listening.
    Returns True when a stale file was removed.
    """
    if not os.path.exists(PID_FILE):
        return False
    try:
        if _self_daemon_pid() is not None:
            return False
        os.unlink(PID_FILE)
        return True
    except Exception:
        return False


def kill_all_vox_overlays():
    """Kill orphaned vox_overlay.py processes (ghost pill windows left behind on exit)."""
    killed = []
    for pid in _scan_cmdline_procs("vox_overlay.py"):
        if _process_alive(pid):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
                killed.append(pid)
            except Exception:
                pass
    return killed


def kill_all_vox_daemons():
    """Kill every alive vox_daemon.py process (GUI/check_env stop & restart use this so no
    shadow instance survives — fixes B1 double-instances). Uses the same dual-signal
    detection as find_existing_daemon_pids."""
    candidates = {_p for _p in _scan_cmdline_procs("vox_daemon.py") if _process_alive(_p)}
    pf = _self_daemon_pid()
    if pf is not None:
        candidates.add(pf)
    killed = []
    for pid in candidates:
        if _process_alive(pid):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
                killed.append(pid)
            except Exception:
                pass
    return killed


# MiMo: cost 0 — the Token Plan subscription includes a mimo-v2.5-asr quota of
# ~30M credits/hour (off-peak 9AM-5PM PDT ≈ Beijing 00:00-08:00 gets 20% off).
# Pay-as-you-go reference price is 0.5 CNY/hour if the quota is ever exhausted.
ENGINE_COST_PER_S = {"groq": 0.1333 / 3600, "local": 0.0, "mimo": 0.0}


def sanitize_prompt_for_fidelity(prompt_text, fidelity_mode="verbatim"):
    """
    In verbatim mode, filters out formal preamble directives like '以下是关于...的内容：'
    and keeps pure technical terms/nouns. This prevents Whisper from hallucinating
    formal essay prose or rewriting conversational fillers.
    """
    if not prompt_text:
        return ""
    if fidelity_mode != "verbatim":
        return str(prompt_text).strip()
    text = str(prompt_text).strip()
    if "：" in text:
        parts = text.split("：", 1)
        if len(parts) > 1 and parts[1].strip():
            text = parts[1].strip()
    elif ":" in text:
        parts = text.split(":", 1)
        if len(parts) > 1 and parts[1].strip():
            text = parts[1].strip()
    return text


# B1: 30 records could not support a P95 and only ever saw successes. 500 keeps
# roughly a week of normal dictation; the file is ~150 KB and is rewritten once per
# take, so the write-amplification stays negligible.
PERF_MAX_RECORDS = 500

# Credential-shaped fragments that must never reach vox_perf_history.json (the GUI
# reads it, and it is exactly the file a user pastes into a bug report).
_SECRET_RX = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|gsk_[A-Za-z0-9]{8,}|dpapi:[A-Za-z0-9+/=]{16,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{8,}|api[-_]?key[\"'=:\s]+[A-Za-z0-9._\-]{8,})",
    re.IGNORECASE,
)


def _sanitize_error_text(s, limit=200):
    """Scrub credential-looking fragments and cap the length of a stored error."""
    return _SECRET_RX.sub("<redacted>", str(s or ""))[:limit]


def record_perf(engine="local", duration_s=1.0, latency_s=0.1, text="", **kwargs):
    """Record one transcription ATTEMPT to vox_perf_history.json.

    B1: this used to be called only after a success, so the fallback chain could
    degrade silently — nothing anywhere showed that an engine had been failing for
    days, which is the same invisibility that let the paste bug hide. It now also
    records failures. New fields:

      ok            did the chain produce text at all
      used_engine   the slot that actually served ("" on total failure)
      attempts      per-slot outcome [{slot, ok, error, error_class, ms}]
      error_class   normalised bucket (auth / quota / timeout / network / oom /
                    missing / empty / too_short / unknown)
      device        the microphone that actually captured this take (B2)
      session_peak  pre-AGC peak level, so "captured silence" and "not understood"
                    stop looking identical (B2)
    """
    try:
        from datetime import datetime
        engine_name = kwargs.get("engine_name", engine)
        audio_len = kwargs.get("audio_len_s", duration_s)
        lat = kwargs.get("latency_s", latency_s)
        txt = kwargs.get("text_preview", text)
        chars = kwargs.get("chars", len(txt) if txt else 0)
        speedup = round(float(audio_len) / max(0.001, float(lat)), 1)

        raw_attempts = kwargs.get("attempts") or []
        attempts = []
        for a in raw_attempts:
            if not isinstance(a, dict):
                continue
            attempts.append({
                "slot": str(a.get("slot", ""))[:40],
                "ok": bool(a.get("ok")),
                "error": _sanitize_error_text(a.get("error", "")),
                "error_class": str(a.get("error_class", "") or ""),
                "ms": a.get("ms"),
            })

        peak = kwargs.get("session_peak")
        item = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "engine": str(engine_name),
            # F9: per-model tracking — which MODEL ran, how long, how much text, quality.
            "model": kwargs.get("model", ""),
            "confidence": kwargs.get("confidence"),
            "duration_s": round(float(audio_len), 2),
            "audio_len_s": round(float(audio_len), 2),
            "latency_ms": round(float(lat) * 1000, 1),
            "speedup": speedup,
            "chars": chars,
            "text": txt[:40] if txt else "",
            "text_preview": txt[:40] if txt else "",
            # ── B1 / B2 attribution ──
            "ok": bool(kwargs.get("ok", True)),
            "used_engine": str(kwargs.get("used_engine", engine_name if kwargs.get("ok", True) else ""))[:60],
            "attempts": attempts,
            "error_class": str(kwargs.get("error_class", "") or ""),
            "error": _sanitize_error_text(kwargs.get("error", "")),
            "device": str(kwargs.get("device", ""))[:80],
            "session_peak": (round(float(peak), 5) if isinstance(peak, (int, float)) else None),
            "reason": str(kwargs.get("reason", ""))[:40],
        }
        history = safe_read_json(PERF_HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        history.insert(0, item)
        safe_write_json(PERF_HISTORY_FILE, history[:PERF_MAX_RECORDS])
        return item
    except Exception:
        return None


def _percentile(values, pct):
    """Nearest-rank percentile. Pure, no numpy, tolerant of small samples."""
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    if len(vals) == 1:
        return round(float(vals[0]), 1)
    idx = max(0, min(len(vals) - 1, int(round((pct / 100.0) * (len(vals) - 1)))))
    return round(float(vals[idx]), 1)


def engine_health(limit=None, records=None):
    """Aggregate the perf history into per-slot health stats (B1).

    Pure function over stored records so it is unit-testable without a GUI.
    Returns {slot: {n, ok, fail, ok_rate, p50_ms, p95_ms, top_error, last_error}}.
    Slots are taken from `attempts` when present, so an engine that only ever FAILS
    still shows up (that is the whole point of B1).
    """
    try:
        recs = records if records is not None else safe_read_json(PERF_HISTORY_FILE, [])
        if not isinstance(recs, list):
            recs = []
        if limit:
            recs = recs[:int(limit)]

        stats = {}

        def _slot(name):
            s = stats.setdefault(name, {
                "n": 0, "ok": 0, "fail": 0, "ok_rate": 0.0,
                "latencies": [], "errors": {}, "last_error": "",
            })
            return s

        for rec in recs:
            if not isinstance(rec, dict):
                continue
            attempts = rec.get("attempts") or []
            if attempts:
                for a in attempts:
                    if not isinstance(a, dict):
                        continue
                    slot = str(a.get("slot", "") or "")
                    if not slot:
                        continue
                    s = _slot(slot)
                    s["n"] += 1
                    if a.get("ok"):
                        s["ok"] += 1
                        if isinstance(a.get("ms"), (int, float)):
                            s["latencies"].append(a["ms"])
                    else:
                        s["fail"] += 1
                        cls = str(a.get("error_class", "") or "unknown")
                        s["errors"][cls] = s["errors"].get(cls, 0) + 1
                        s["last_error"] = str(a.get("error", ""))[:120]
            else:
                # Legacy records (written before B1) have no attempts list.
                slot = str(rec.get("engine", "") or "unknown")
                s = _slot(slot)
                ok = bool(rec.get("ok", True))
                s["n"] += 1
                s["ok" if ok else "fail"] += 1
                if ok and isinstance(rec.get("latency_ms"), (int, float)):
                    s["latencies"].append(rec["latency_ms"])

        for slot, s in stats.items():
            s["ok_rate"] = round(s["ok"] / s["n"], 3) if s["n"] else 0.0
            s["p50_ms"] = _percentile(s["latencies"], 50)
            s["p95_ms"] = _percentile(s["latencies"], 95)
            s["top_error"] = (max(s["errors"], key=s["errors"].get)
                              if s["errors"] else "")
            s["error_counts"] = dict(s["errors"])
            del s["errors"]
            del s["latencies"]
        return stats
    except Exception:
        return {}


# ─── Thread-Safe Logging ──────────────────────────────────────────
_log_lock = threading.Lock()
_log_fh = None

# vox.log was opened in append mode and never rotated or truncated, so it grew
# without bound for the life of the install. Size-cap it at ~2 MB and keep one
# previous generation.
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_CHECK_EVERY = 200          # lines between size checks (avoids a stat per line)
_log_lines_since_check = 0


def _rotate_log_if_needed():
    """Roll vox.log to vox.log.1 once it passes the size cap. Caller holds the lock."""
    global _log_fh, _log_lines_since_check
    _log_lines_since_check += 1
    if _log_lines_since_check < _LOG_CHECK_EVERY:
        return
    _log_lines_since_check = 0
    try:
        if _log_fh is not None:
            _log_fh.flush()
        if os.path.getsize(LOG_FILE) < _LOG_MAX_BYTES:
            return
        if _log_fh is not None:
            _log_fh.close()
            _log_fh = None
        backup = LOG_FILE + ".1"
        try:
            if os.path.exists(backup):
                os.unlink(backup)
        except Exception:
            pass
        os.replace(LOG_FILE, backup)
    except Exception:
        # Never let log maintenance break logging.
        pass


def log(msg, level="INFO"):
    global _log_fh
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    # A pythonw/daemon process has no console: sys.stdout is None and every
    # print() would raise and be swallowed. Skip it outright.
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass
    with _log_lock:
        try:
            _rotate_log_if_needed()
            if _log_fh is None:
                _log_fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
            _log_fh.write(line + "\n")
        except Exception:
            pass


# ─── Safe File IO ─────────────────────────────────────────────────
def safe_write_json(path, data, max_retries=5):
    """Atomically write JSON (temp file + os.replace) with retry.

    BUG-5 fix: the old ``open(path, "w")`` truncated the LIVE file before writing.
    config.json has multiple writers (GUI save, overlay drag/reset) and
    overlay_state.json is rewritten ~25x/s while recording; a reader could catch
    a half-written document, and two writers could interleave into a permanently
    corrupt file (config.json also holds the DPAPI key). ``os.replace`` is atomic
    on NTFS: a reader sees either the complete old file or the complete new one.
    """
    content = json.dumps(data, indent=2, ensure_ascii=False)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    for attempt in range(max_retries):
        tmp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".vox-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp_path, path)
            return True
        except Exception as exc:
            if tmp_path:
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    pass
            if attempt == max_retries - 1:
                log(f"写入 {os.path.basename(str(path))} 失败: {exc}", "WARN")
                return False
            time.sleep(0.04 * (attempt + 1))
    return False

def safe_read_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    for attempt in range(5):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            time.sleep(0.02 * (attempt + 1))
    return default if default is not None else {}


# ─── Single-Instance Lock (Mutex) ─────────────────────────────────
class SingleInstanceLock:
    """
    Win32 Named Mutex for guaranteeing strictly ONE Vox daemon instance.
    Automatically released on process exit or crash by Windows kernel.
    Falls back to socket lock if ctypes/win32 is unavailable.
    """
    def __init__(self, name="Global\\Vox_Windows_Daemon_SingleInstance_Mutex", port=38123):
        self.name = name
        self.port = port
        self.handle = None
        self.sock = None
        self.acquired = False

    def acquire(self):
        """Strict single-instance acquisition via AND semantics (Win32 named mutex AND an
        exclusive TCP port). This closes two documented failure modes:
          * OR gap — previously one instance could fall through to the socket fallback
            while another held the mutex, so both "acquired" the lock;
          * privileged/namespace split — a Global\\ object created by an elevated process
            and a same-named object from a non-elevated process can land in different
            bases and both "succeed". The TCP port lives in a single global namespace, so
            requiring it too means only ONE process can ever own both primitives.
        On any failure we release whatever we grabbed before returning False."""
        import socket

        # 1. Prefer Win32 Named Mutex (precise; released by the kernel on crash/exit)
        #    BUG-3 fix: use WinDLL(use_last_error=True) and explicit prototypes.
        #    ctypes.windll leaves restype at c_int (truncating 64-bit HANDLEs) and
        #    does not preserve the last-error slot, so the subsequent GetLastError()
        #    could spuriously return ERROR_ALREADY_EXISTS and make this instance
        #    believe another daemon owns the lock.
        mutex_ok = False
        try:
            import ctypes
            ERROR_ALREADY_EXISTS = 183
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p,
            ]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            self.handle = kernel32.CreateMutexW(None, False, self.name)
            last_err = ctypes.get_last_error()
            if self.handle and last_err != ERROR_ALREADY_EXISTS:
                mutex_ok = True
            elif self.handle:
                kernel32.CloseHandle(self.handle)
                self.handle = None
        except Exception:
            self.handle = None

        # 2. Exclusive TCP port (single-namespace tiebreaker)
        port_ok = False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # NOTE: do NOT set SO_REUSEADDR here. On Windows it allows a SECOND
            # socket to bind the same address:port while the first is listening,
            # which would silently defeat this exclusive-port tiebreaker
            # (verified empirically: a concurrent bind succeeded while a daemon
            # held the port). The mutex remains the primary gate; this port is
            # the cross-namespace fallback and must stay exclusive.
            sock.bind(("127.0.0.1", self.port))
            sock.listen(1)
            self.sock = sock
            port_ok = True
        except Exception:
            self.sock = None

        if mutex_ok and port_ok:
            self.acquired = True
            return True

        if self.handle:
            try:
                import ctypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            except Exception:
                pass
            self.handle = None
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.acquired = False
        return False

    def release(self):
        if self.handle:
            try:
                import ctypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            except Exception:
                pass
            self.handle = None
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.acquired = False


# ─── GUI Single-Instance + Restore (port-free) ─────────────────────
# The manager window used to advertise a RESTORE channel on TCP port 48124.
# On this machine the whole ~48100-48200 range is reserved (WSL2/Hyper-V
# dynamic exclusion), so that bind always failed with 10048 and the "wake
# existing window" path silently degraded to spawning a NEW window every
# click. The replacement uses no ports at all:
#   * a Win32 NAMED MUTEX — kernel-atomic single-instance + liveness signal
#     (the GUI keeps it open for its lifetime; released on exit/crash);
#   * a small FLAG FILE — the wake signal. A second launch (or the daemon
#     tray) writes it; the running GUI's ~2s refresh loop notices and surfaces.
GUI_INSTANCE_MUTEX = "Global\\Vox_Windows_GUI_SingleInstance_Mutex"
GUI_RESTORE_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vox.gui.restore")


def gui_is_running():
    """True while a manager window is alive (it keeps the named mutex open)."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        # SYNCHRONIZE (0x1) is required — OpenMutexW with access 0x0 is
        # rejected with ACCESS_DENIED on Windows.
        h = kernel32.OpenMutexW(0x0001, False, GUI_INSTANCE_MUTEX)
        if h:
            kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def gui_request_restore():
    """Ask the running manager window to surface (write the flag file)."""
    try:
        with open(GUI_RESTORE_FLAG, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        return True
    except Exception:
        return False


def gui_check_restore_request():
    """GUI side: return True exactly once per request; clears the flag."""
    last = 0.0
    try:
        with open(GUI_RESTORE_FLAG, "r", encoding="utf-8") as f:
            val = float(f.read().strip() or 0)
        if val > last + 1e-6 and val > time.time() - 120:  # ignore ancient flags
            try:
                os.remove(GUI_RESTORE_FLAG)
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


# ─── Sound Feedback (Synthesized Soft Cues) ───────────────────────
class SoundFeedback:
    """Non-blocking, synthesized pleasant audio feedback cues."""
    _tones = {}
    _init_lock = threading.Lock()

    @classmethod
    def _synthesize_tone(cls, freq_start, freq_end, duration_s=0.08, sample_rate=44100):
        num_samples = int(duration_s * sample_rate)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            frames = bytearray()
            for i in range(num_samples):
                t = i / float(num_samples)
                freq = freq_start + (freq_end - freq_start) * t
                # Raised-cosine envelope for zero click/pop
                env = 0.5 * (1.0 - math.cos(2.0 * math.pi * t))
                val = int(env * 0.28 * 32767 * math.sin(2 * math.pi * freq * (i / sample_rate)))
                frames.extend(struct.pack("<h", max(-32767, min(32767, val))))
            w.writeframes(frames)
        return buf.getvalue()

    @classmethod
    def init_tones(cls):
        with cls._init_lock:
            if not cls._tones and _WINSOUND_OK:
                try:
                    # Pop up on start: 600 -> 950 Hz
                    cls._tones["start"] = cls._synthesize_tone(600, 950, 0.07)
                    # Chime up on done: 850 -> 1350 Hz
                    cls._tones["done"] = cls._synthesize_tone(850, 1350, 0.09)
                    # Subtle down tone on cancel: 650 -> 400 Hz
                    cls._tones["cancel"] = cls._synthesize_tone(650, 400, 0.08)
                except Exception as e:
                    log(f"Tone synthesis error: {e}", "DEBUG")

    @classmethod
    def play(cls, kind="start", enabled=True):
        if not enabled or not _WINSOUND_OK:
            return
        if not cls._tones:
            cls.init_tones()
        tone_data = cls._tones.get(kind)
        if tone_data:
            try:
                # SND_ASYNC | SND_MEMORY: completely non-blocking
                winsound.PlaySound(tone_data, winsound.SND_MEMORY | winsound.SND_ASYNC)
            except Exception:
                pass


def input_device_signature(dev):
    """Stable, normalised signature of an input-device dict for hot-plug change
    detection (complaint #5 / audit B5). 'index|friendly_name' lower-cased so a
    re-enumeration that only shifts indices is still caught, while casing/whitespace
    noise does not cause false positives. Returns None for a missing device."""
    if dev is None:
        return None
    return f"{int(dev.get('index', -1))}|{str(dev.get('friendly_name', '') or dev.get('name', '')).strip().lower()}"


def input_device_changed(prev, curr):
    """True when the active recording device differs from the previously bound one.
    Pure + dependency-free so it can be unit-tested headlessly."""
    return input_device_signature(prev) != input_device_signature(curr)


# ─── Audio Pipeline (Capture, AGC, VAD, Resample) ─────────────────
class AudioPipeline:
    TARGET_SR = 16000

    def __init__(self, target_sr=16000):
        self.target_sr = target_sr
        self.frames = []
        self._stream = None
        self._lock = threading.Lock()
        self.device_idx = None
        self.device_sr = 16000
        self.device_name = ""
        self.live_level_cb = None  # callback(peak_level: float)
        self._cached_devices = None
        self._cache_time = 0
        # Recording ceiling. `max_recording_seconds` has always existed in
        # config.json but NOTHING ever read it, so a hands-free take the user
        # forgot about appended to `self.frames` forever — an unbounded memory
        # growth path disguised as a settings option.
        self.max_seconds = 300.0
        self.limit_reached = False
        self._captured = 0          # samples accepted for the current take
        self._max_samples = 0       # 0 => no cap configured
        # B2: highest PRE-AGC sample of the current take. normalize_audio() applies up
        # to 15x gain, so measuring after AGC would make "captured silence" and
        # "captured a whisper" indistinguishable — which is precisely the misdiagnosis
        # this guards against ("Vox is broken" vs "the default mic is a dead device").
        self.session_peak = 0.0

    @staticmethod
    def _friendly_name(name):
        s = (name or "").strip()
        m = re.search(r';\(\s*(.+?)\s*[\)]*$', s)
        if m:
            clean = m.group(1).rstrip(")")
            return clean.strip()
        # If open parenthesis is unclosed due to legacy MME 31-char limit, close it instead of deleting
        if s.count("(") > s.count(")"):
            s = s + ")"
        return s

    def list_input_devices(self, force_refresh=False):
        sd, sd_ok = _dep("sounddevice")     # LAZY: PortAudio loads on first device query
        if not sd_ok:
            return []
        now = time.time()
        if not force_refresh and self._cached_devices and (now - self._cache_time < 5.0):
            return self._cached_devices

        try:
            devs = sd.query_devices()
            apis = sd.query_hostapis()
        except Exception:
            return []

        out = []
        for i, d in enumerate(devs):
            in_ch = d.get("max_input_channels", 0)
            if in_ch > 0:
                hostapi_idx = d.get("hostapi", 0)
                api_name = apis[hostapi_idx]["name"] if hostapi_idx < len(apis) else ""
                name = d.get("name", f"Device {i}")
                friendly = self._friendly_name(name)
                native_sr = int(d.get("default_samplerate") or 16000)
                out.append({
                    "index": i,
                    "name": name,
                    "friendly_name": friendly,
                    "hostapi": api_name,
                    "channels": in_ch,
                    "native_sr": native_sr,
                })
        self._cached_devices = out
        self._cache_time = now
        return out

    def resolve_device(self, preferred_device=None):
        """
        Fast, zero-hang device resolution:
        1. Match preferred_device (by index or by friendly name)
        2. Fallback to Windows default recording device if valid
        3. Fallback to first openable input device
        """
        devs = self.list_input_devices()
        if not devs:
            return None, "未检测到音频输入设备"

        # 1. Match preferred_device
        if preferred_device is not None:
            # Match by integer index
            if isinstance(preferred_device, int):
                for d in devs:
                    if d["index"] == preferred_device:
                        return d["index"], d["friendly_name"]
            # Match by string (case-insensitive substring in name or friendly_name)
            pref_str = str(preferred_device).strip().lower()
            if pref_str and pref_str not in ("none", "null", "auto", "(自动 — 选信号最强的设备)"):
                matched = [d for d in devs if pref_str in d["friendly_name"].lower() or pref_str in d["name"].lower()]
                if matched:
                    # Route through the shared host-API preference (WASAPI first).
                    #
                    # This list used to be hardcoded to ["DirectSound", "WASAPI", "MME"]
                    # — DirectSound first — which is the exact opposite of what this
                    # machine measures back-to-back with the daemon's own parameters
                    # (channels=1), 3 alternating rounds each, medians:
                    #     DirectSound idx 7   peak 0.00055   rms 0.0032 / 0.0011 / 0.0007
                    #     WASAPI      idx 15  peak 0.00184   rms 0.0022 / 0.0071 / 0.0064
                    #     MME         idx 1   peak 0.00076   rms 0.0010 / 0.0028 / 0.0053
                    # DirectSound's RMS sat under the daemon's 0.004 silence gate on
                    # every round, so takes were discarded as "silent" and produced no
                    # output; WASAPI clears the gate. Because `preferred_device` is set
                    # in config.json, THIS path is the one that actually runs — fixing
                    # only the default-device path (as an earlier attempt did) changed
                    # nothing.
                    better = self._same_device_better_api(matched[0], matched)
                    return better["index"], better["friendly_name"]

        # 2. Match the default recording device — then re-resolve it onto a better
        #    host API. Returning `def_idx` directly silently chose MME, which is what
        #    `sd.default.device[0]` reports, and the host-API preference documented at
        #    the top of this method only existed in paths (1) and (3) — so the normal
        #    case bypassed it entirely.
        #
        #    Measured cost of that on a typical USB microphone:
        #        MME  endpoint, channels=1 -> peak 0.00064   (what the daemon captured)
        #        MME  endpoint, channels=2 -> peak 0.00476   (both channels identical)
        #        WASAPI endpoint, channels=1 -> peak 0.00809
        #    i.e. MME + mono loses ~7x of the signal versus stereo, and ~12x versus
        #    WASAPI mono. Every take then fell under the daemon's RMS<0.004 silence
        #    guard and was skipped as "silent" — the user saw nothing come out.
        sd, _sd_ok = _dep("sounddevice")
        try:
            def_idx = sd.default.device[0]
            if def_idx is not None and def_idx >= 0:
                for d in devs:
                    if d["index"] == def_idx:
                        better = self._same_device_better_api(d, devs)
                        return better["index"], better["friendly_name"]
        except Exception:
            pass

        # 3. Fallback to highest priority hostapi (prefer WASAPI/DirectSound over MME)
        priority_apis = ["Windows WASAPI", "Windows DirectSound", "MME"]
        for prio_api in priority_apis:
            for d in devs:
                if prio_api in d["hostapi"]:
                    return d["index"], d["friendly_name"]

        first = devs[0]
        return first["index"], first["friendly_name"]

    # Host APIs in preference order, best first. MME is last ON PURPOSE: it is the
    # legacy mapper, it truncates device names to 31 characters, and (measured above)
    # it loses most of the signal when asked for a single channel from a stereo device.
    _HOSTAPI_PREFERENCE = ("Windows WASAPI", "Windows DirectSound", "MME")

    @classmethod
    def _same_device_better_api(cls, dev, devs):
        """The same physical microphone on the best host API available.

        One microphone appears once per host API — a USB mic can be listed four times
        (MME / DirectSound / WASAPI / WDM-KS endpoints) — so
        selecting by index alone lands on whichever endpoint Windows calls "default"
        rather than the one that actually works best. Re-match by NAME inside the
        preference order instead.
        """
        try:
            name = (dev.get("name") or "").strip().lower()
            if not name:
                return dev
            # Compare on a PREFIX, not the whole name: MME truncates device names to 31
            # characters, so this machine's default endpoint reads
            # "microphone (samson q2u micropho" while the WASAPI endpoint of the very
            # same microphone reads "microphone (samson q2u microphone". An exact
            # comparison therefore never matched and the preference silently did
            # nothing — which is exactly what happened on the first attempt at this fix.
            key = name[:24]
            for api in cls._HOSTAPI_PREFERENCE:
                for d in devs:
                    if ((d.get("name") or "").strip().lower()[:24] == key
                            and api in (d.get("hostapi") or "")):
                        return d
        except Exception:
            pass
        return dev

    def start_recording(self, device_idx=None):
        sd, sd_ok = _dep("sounddevice")
        if not sd_ok:
            raise RuntimeError(f"sounddevice 不可用: {_dep_err('sounddevice')}")

        if device_idx is None:
            device_idx, _ = self.resolve_device()
        if device_idx is None:
            raise RuntimeError("无可用输入设备")

        self.device_idx = device_idx
        # Query native sample rate to avoid WASAPI PaErrorCode -9997
        try:
            info = sd.query_devices(device_idx)
            self.device_sr = int(info.get("default_samplerate") or self.TARGET_SR)
            self.device_name = self._friendly_name(info.get("name", ""))
        except Exception:
            self.device_sr = self.TARGET_SR
            self.device_name = f"Device {device_idx}"

        self.limit_reached = False
        self._captured = 0
        self._max_samples = int(max(1.0, float(self.max_seconds)) * self.device_sr)
        self.session_peak = 0.0

        with self._lock:
            self.frames = []

        self._stream = sd.InputStream(
            samplerate=self.device_sr,
            channels=1,
            dtype="float32",
            device=device_idx,
            callback=self._audio_callback,
        )
        self._stream.start()

    # B2 thresholds on the RAW (pre-AGC) peak.
    SILENT_PEAK = 0.005      # essentially digital silence -> wrong device / muted
    QUIET_PEAK = 0.030       # audible but very low -> mic gain / distance problem

    def peak_verdict(self):
        """"silent" | "quiet" | "ok" — classify the take by its pre-AGC peak (B2).

        Turns "transcription came back empty" into an actionable statement: silence
        almost always means the wrong input device was bound, whereas a normal-level
        take that still returns nothing was simply not understood.
        """
        try:
            peak = float(self.session_peak or 0.0)
        except Exception:
            return "ok"
        if peak < self.SILENT_PEAK:
            return "silent"
        if peak < self.QUIET_PEAK:
            return "quiet"
        return "ok"

    def _audio_callback(self, indata, frames, time_, status):
        # B2: compute the raw peak ONCE, before any gain is applied, and keep the
        # running maximum so the daemon can attribute an empty result later.
        np = _np()
        raw_peak = float(np.max(np.abs(indata))) if indata.size else 0.0
        with self._lock:
            if raw_peak > self.session_peak:
                self.session_peak = raw_peak
            if self._max_samples and self._captured >= self._max_samples:
                # Cap reached — stop buffering and flag it. The daemon watches
                # this flag and finishes the take gracefully (calling into
                # PortAudio from inside its own callback would risk a deadlock).
                self.limit_reached = True
            else:
                self.frames.append(indata.copy())
                self._captured += int(indata.shape[0])
        if self.live_level_cb is not None and indata.size > 0:
            try:
                self.live_level_cb(raw_peak)
            except Exception:
                pass

    def stop_recording(self):
        np = _np()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            finally:
                self._stream = None

        with self._lock:
            if not self.frames:
                return np.zeros(0, dtype="float32")
            raw = np.concatenate(self.frames, axis=0).ravel().astype("float32")
            self.frames = []

        # 1. Resample to 16,000 Hz if needed
        audio = self.resample(raw, self.device_sr, self.TARGET_SR)

        # 2. Intelligent AGC & Peak Normalization
        audio = self.normalize_audio(audio)

        # 3. VAD Silence Trim
        audio = self.vad_trim(audio, self.TARGET_SR)

        return audio

    @staticmethod
    def resample(audio, src_sr, dst_sr=16000):
        np = _np()
        if audio.size == 0 or int(src_sr) == int(dst_sr):
            return audio.astype("float32")
        duration = audio.size / float(int(src_sr))
        new_len = max(1, int(duration * int(dst_sr)))
        return np.interp(
            np.linspace(0, audio.size, new_len, endpoint=False),
            np.arange(audio.size),
            audio,
        ).astype("float32")

    @staticmethod
    def normalize_audio(audio, target_peak=0.85, max_gain=15.0):
        """
        Intelligent AGC: If audio is too quiet (whisper, low mic volume),
        smoothly amplify up to target_peak without clipping.
        """
        if audio.size == 0:
            return audio
        np = _np()
        peak = float(np.max(np.abs(audio)))
        if peak < 1e-4:
            return audio  # pure digital silence, don't amplify noise
        # Compute gain to reach target peak, capped at max_gain
        gain = min(target_peak / peak, max_gain)
        if gain > 1.05 or gain < 0.95:
            audio = audio * gain
        return np.clip(audio, -1.0, 1.0).astype("float32")

    @staticmethod
    def vad_trim(audio, sr=16000, frame_ms=30, threshold_rms=0.003, margin_ms=150):
        """
        Trim leading and trailing digital silence to speed up transcription
        and prevent Groq 400 Bad Request on empty snippets.
        """
        if audio.size < sr * 0.1:
            return audio
        np = _np()
        frame_len = int(sr * frame_ms / 1000.0)
        margin_len = int(sr * margin_ms / 1000.0)
        num_frames = audio.size // frame_len
        if num_frames == 0:
            return audio

        energies = [
            float(np.sqrt(np.mean(audio[i * frame_len : (i + 1) * frame_len] ** 2)))
            for i in range(num_frames)
        ]

        active = [i for i, e in enumerate(energies) if e >= threshold_rms]
        if not active:
            # Everything is below threshold, keep original or trimmed
            return audio

        start_idx = max(0, active[0] * frame_len - margin_len)
        end_idx = min(audio.size, (active[-1] + 1) * frame_len + margin_len)
        return audio[start_idx:end_idx]


# ─── WAV & FLAC Audio Helpers ─────────────────────────────────────
def write_wav(path, audio, sr=16000):
    np = _np()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        pcm = np.clip(audio, -1.0, 1.0)
        pcm = (pcm * 32767).astype("<i2")
        w.writeframes(pcm.tobytes())

def to_flac_or_wav(src_wav, preferred_format="flac"):
    """Convert to FLAC for ~60% less upload bandwidth.

    Encoding is done IN-PROCESS via libsndfile (soundfile). This previously
    shelled out to ffmpeg on every single transcription, which on Windows costs a
    process spawn plus ffmpeg's own startup (~100-200ms) directly on the
    post-recording critical path — usually more than the bandwidth it saved for a
    short dictation. ffmpeg is kept only as a fallback.
    """
    if preferred_format != "flac":
        return src_wav, "wav"

    dst = src_wav + ".flac"

    try:
        import soundfile as sf
        data, sr = sf.read(src_wav, dtype="float32", always_2d=False)
        sf.write(dst, data, int(sr), format="FLAC")
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst, "flac"
    except Exception as e:
        log(f"进程内 FLAC 编码不可用，回退 ffmpeg: {e}", "DEBUG")

    if shutil.which("ffmpeg"):
        try:
            res = subprocess.run(
                ["ffmpeg", "-y", "-i", src_wav, "-compression_level", "5", dst],
                capture_output=True,
                timeout=10,
            )
            if res.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
                return dst, "flac"
        except Exception:
            pass
    return src_wav, "wav"


# ─── STT Engines (Groq Cloud & Local Faster-Whisper) ──────────────
class STTEngine:
    name = "base"
    def transcribe(self, audio_path, language="auto"):
        raise NotImplementedError

class GroqEngine(STTEngine):
    name = "groq"

    def __init__(self, api_key, model=None, cfg=None):
        if not api_key:
            raise RuntimeError("Groq API Key 未配置 (请在 config.json 或环境变量 VOX_GROQ_KEY 中设置)")
        groq_sdk, groq_ok = _dep("groq")    # LAZY: the SDK loads only when Groq is used
        if not groq_ok:
            raise RuntimeError("groq Python SDK 未安装")
        self.client = groq_sdk.Groq(api_key=api_key)
        self.model = model or "whisper-large-v3-turbo"
        self.cfg = cfg

    def transcribe(self, audio_path, language="auto", prompt=None):
        t0 = time.time()
        file_size = os.path.getsize(audio_path)
        if file_size < 100:
            raise RuntimeError("音频文件过短或为空")

        with open(audio_path, "rb") as af:
            filename = "audio.flac" if audio_path.endswith(".flac") else "audio.wav"
            mime = "audio/flac" if audio_path.endswith(".flac") else "audio/wav"
            payload = {
                "file": (filename, af, mime),
                "model": self.model,
                "response_format": "text",
            }
            groq_cfg = (self.cfg or {}).get("engines", {}).get("groq", {})
            if "temperature" in groq_cfg:
                payload["temperature"] = float(groq_cfg["temperature"])
            if language and language.lower() not in ("auto", "none", ""):
                payload["language"] = language
            if prompt:
                fidelity = (self.cfg or {}).get("fidelity", {}).get("mode", "verbatim") if self.cfg else "verbatim"
                clean_prompt = sanitize_prompt_for_fidelity(prompt, fidelity)
                if clean_prompt:
                    payload["prompt"] = str(clean_prompt).strip()

            resp = self.client.audio.transcriptions.create(**payload, timeout=30)

        latency = time.time() - t0
        text = resp if isinstance(resp, str) else getattr(resp, "text", "")
        cost = ENGINE_COST_PER_S.get(self.name, 0) * latency
        return (text or "").strip(), latency, cost


class MimoEngine(STTEngine):
    """Xiaomi MiMo-V2.5-ASR cloud engine (OpenAI-compatible chat completions).

    Endpoint: POST {base}/chat/completions  (default base https://api.xiaomimimo.com/v1)
    Auth:     `api-key` header (plus `Authorization: Bearer` for gateway compatibility).
    Body:     model=mimo-v2.5-asr, user message content=[input_audio data URL],
              asr_options={language: auto|zh|en}.
    Input:    only wav / mp3 are accepted upstream — FLAC is converted on the fly.
    Quota:    ~30M credits/hour under the Token Plan subscription; cost booked as 0.
    """
    name = "mimo"
    # Token Plan 专属端点（coding-plan 订阅用户走这个网关，含 mimo-v2.5-asr）；
    # 非订阅用户可在 config 里改回公共端点 https://api.xiaomimimo.com/v1
    DEFAULT_BASE = "https://token-plan-sgp.xiaomimimo.com/v1"
    DEFAULT_MODEL = "mimo-v2.5-asr"
    # Upstream cap on the base64 audio payload (tts-mimo integration docs: 25 MB).
    MAX_B64_BYTES = 25 * 1024 * 1024

    def __init__(self, api_key, model=None, base_url=None, cfg=None, timeout=60):
        if not api_key:
            raise RuntimeError("MiMo API Key 未配置 (请在 config.json 或环境变量 VOX_MIMO_KEY 中设置)")
        _, req_ok = _dep("requests")        # LAZY: requests loads only when MiMo is used
        if not req_ok:
            raise RuntimeError("requests 库未安装 (pip install requests)")
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.base_url = (base_url or self.DEFAULT_BASE).rstrip("/")
        self.cfg = cfg
        self.timeout = timeout

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _lang_for_asr(language):
        """Map vox language codes to MiMo asr_options.language (auto/zh/en)."""
        l = (language or "auto").strip().lower()
        if l in ("zh", "chinese", "zh-cn", "cn"):
            return "zh"
        if l in ("en", "english"):
            return "en"
        return "auto"

    def _prepare_audio(self, audio_path):
        """Return (usable_path, tmp_path_or_None). Converts FLAC -> 16-bit WAV
        (soundfile first, ffmpeg fallback) because upstream only accepts wav/mp3."""
        if not audio_path.lower().endswith(".flac"):
            return audio_path, None
        tmp = audio_path + ".mimo.wav"
        try:
            import soundfile as sf
            data, sr = sf.read(audio_path, dtype="float32")
            mono = data[:, 0] if data.ndim == 2 else data
            write_wav(tmp, mono, int(sr))
            return tmp, tmp
        except Exception:
            pass
        if shutil.which("ffmpeg"):
            try:
                res = subprocess.run(
                    ["ffmpeg", "-y", "-i", audio_path, tmp],
                    capture_output=True, timeout=15,
                )
                if res.returncode == 0 and os.path.exists(tmp):
                    return tmp, tmp
            except Exception:
                pass
        raise RuntimeError("MiMo 引擎仅接受 wav/mp3, 且本机 FLAC→WAV 转换失败 (缺 soundfile 与 ffmpeg)")

    @staticmethod
    def _extract_text(data):
        """choices[0].message.content — string, or list of {text: ...} parts."""
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [it["text"] for it in content
                     if isinstance(it, dict) and isinstance(it.get("text"), str)]
            text = "".join(parts).strip()
            if text:
                return text
        return ""

    # -- main entry ----------------------------------------------------------
    def transcribe(self, audio_path, language="auto", prompt=None):
        requests, _req_ok = _dep("requests")
        t0 = time.time()
        file_size = os.path.getsize(audio_path)
        if file_size < 100:
            raise RuntimeError("音频文件过短或为空")

        usable, tmp = self._prepare_audio(audio_path)
        try:
            with open(usable, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            if len(b64) > self.MAX_B64_BYTES:
                raise RuntimeError(f"MiMo 音频超出上传上限 ({len(b64) / 1048576:.1f}MB > 25MB base64)")

            mime = "audio/mp3" if usable.lower().endswith(".mp3") else "audio/wav"
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": f"data:{mime};base64,{b64}"},
                            }
                        ],
                    }
                ],
                "asr_options": {"language": self._lang_for_asr(language)},
            }
            headers = {
                "Content-Type": "application/json",
                "api-key": self.api_key,                 # MiMo 官方鉴权头
                "Authorization": f"Bearer {self.api_key}",  # 兼容走 OpenAI 网关的代理
            }
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload, headers=headers, timeout=self.timeout,
            )
            if resp.status_code >= 400:
                try:
                    err = resp.json().get("error")
                    detail = (err.get("message") if isinstance(err, dict) else str(err)) \
                        or resp.text[:300]
                except Exception:
                    detail = resp.text[:300] or f"HTTP {resp.status_code}"
                raise RuntimeError(f"MiMo API {resp.status_code}: {detail}")

            text = self._extract_text(resp.json())
        finally:
            if tmp and tmp != usable and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

        latency = time.time() - t0
        cost = ENGINE_COST_PER_S.get(self.name, 0) * latency
        return (text or "").strip(), latency, cost


class SenseVoiceEngine(STTEngine):
    """G2: Alibaba FunAudioLLM SenseVoice (via FunASR) — a newer local ASR that
    beats faster-whisper 'base' on Chinese accuracy, with BUILT-IN punctuation and
    emotion/audio-event detection. ~1GB model downloads on first use; runs on
    GPU (CUDA) or CPU. Optional dependency: `pip install funasr`."""
    name = "sensevoice"
    _models = {}
    _load_lock = threading.Lock()
    _active_device = "cpu"

    def __init__(self, device="auto", model="iic/SenseVoiceSmall", use_itn=None):
        self.model = model
        # ITN on/off. None => default True (see transcribe()). Exposed so the
        # documented "built-in punctuation" is actually reachable from config.
        self.use_itn = use_itn
        has_cuda, _ = get_cuda_device_info()
        if device == "auto":
            self.device = "cuda" if has_cuda else "cpu"
        else:
            self.device = str(device).lower()

    def _get_model(self):
        auto_model, fasr_ok = _dep("funasr")
        if not fasr_ok:
            raise Exception("funasr 未安装 (pip install funasr)")
        dev = self.device
        if dev in SenseVoiceEngine._models:
            return SenseVoiceEngine._models[dev]
        with SenseVoiceEngine._load_lock:
            if dev in SenseVoiceEngine._models:
                return SenseVoiceEngine._models[dev]
            try:
                if dev == "cuda" and not _dep("torch")[0].cuda.is_available():
                    dev = "cpu"
            except Exception:
                pass
            # BUG-9 fix: transcribe() derives use_fp16 from self.device. If cuda
            # silently degraded to cpu here without updating it, fp16 inference
            # would be requested on a CPU-resident model.
            self.device = dev
            desc = "CUDA GPU" if dev == "cuda" else "CPU"
            log(f"加载 SenseVoice '{self.model}' [{desc}]... (首次使用会自动下载 ~1GB 模型)")
            try:
                SenseVoiceEngine._models[dev] = auto_model(
                    model=self.model, device=dev, disable_update=True)
            except Exception as e:
                if dev == "cuda":
                    log(f"SenseVoice CUDA 加载异常，降级 CPU: {e}", "WARN")
                    SenseVoiceEngine._models["cpu"] = auto_model(
                        model=self.model, device="cpu", disable_update=True)
                    dev = "cpu"
                    SenseVoiceEngine._models[dev] = SenseVoiceEngine._models["cpu"]
                else:
                    raise
            # BUG-9: a CUDA load failure also degrades to cpu — keep self.device
            # in sync so transcribe() does not request fp16 on the CPU model.
            self.device = dev
            SenseVoiceEngine._active_device = dev
            return SenseVoiceEngine._models[dev]

    def transcribe(self, audio_path, language="auto", prompt=None):
        t0 = time.time()
        model = self._get_model()
        # SenseVoice: language 'auto' does LID+ASR; built-in punctuation + inverse text.
        lang = "auto"
        if language and str(language).lower() not in ("auto", "none", ""):
            lang = str(language).lower()
        # G2: fp16 on CUDA — SenseVoiceSmall hits low RTF on a discrete GPU
        # (5s of audio in ~60ms); fp32 is ~250x slower on this GPU.
        #
        # ITN (inverse text normalization) is what ACTUALLY produces SenseVoice's
        # built-in punctuation and number formatting. Left off, funasr emits the
        # <|woitn|> tag and the text comes back with zero punctuation
        # ("大家好今天我们来测试云端口述输入功能"); switched on, the same audio yields
        # "大家好，今天我们来测试云端口述输入功能。" for effectively the same cost
        # (0.238s vs 0.272s measured on CPU). Configurable via
        # engines.sensevoice.use_itn; defaults to True when unset.
        itn = True if self.use_itn is None else bool(self.use_itn)
        res = model.generate(
            input=audio_path,
            language=lang,
            use_fp16=(self.device == "cuda"),
            batch_size_s=300,
            merge_vad=True,
            merge_length_s=15,
            use_itn=itn,
        )
        text = ""
        try:
            if res and len(res):
                text = res[0].get("text", "")
        except Exception:
            text = ""
        # SenseVoice prefixes language/emotion/event tags like
        # <|zh|><|NEUTRAL|><|Speech|><|woitn|> — strip all <|...|> tokens.
        text = re.sub(r"<\|[^|]*\|>", "", text)
        return text.strip(), time.time() - t0, 0.0


def offload_gpu_models(reason=""):
    """G7: offload the local ASR model (SenseVoice) from the GPU to free VRAM.

    The engine keeps its loaded model instances in a CLASS-LEVEL cache
    (SenseVoiceEngine._models), so a plain config hot-reload would NOT release the
    GPU memory — the old CUDA instance stays referenced forever. This function drops
    the cached instances, forces a GC pass, and asks PyTorch to return its reserved
    CUDA memory blocks. The model reloads lazily (on the next transcription) on
    whatever device the config currently specifies.

    Returns a short human-readable summary string.
    """
    freed = []
    try:
        if SenseVoiceEngine._models:
            SenseVoiceEngine._models.clear()
            SenseVoiceEngine._active_device = "cpu"
            freed.append("SenseVoice")
    except Exception:
        pass

    try:
        import gc
        gc.collect()
    except Exception:
        pass
    cuda_ok = False
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            cuda_ok = True
    except Exception:
        pass

    if not freed and not cuda_ok:
        return "无 GPU 上的本地模型需要卸载"
    return "、".join(freed) if freed else "仅释放 CUDA 缓存"


def get_gpu_vram_info():
    """Query current GPU VRAM usage and total via nvidia-smi.

    Returns (ok: bool, used_mb: int, total_mb: int).
    """
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        out = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,noheader,nounits'],
            timeout=1.5,
            creationflags=flags
        ).decode().strip()
        parts = [p.strip() for p in out.split(',')]
        if len(parts) >= 2:
            return True, int(parts[0]), int(parts[1])
    except Exception:
        pass
    return False, 0, 0


def request_gpu_offload():
    """Signals running vox daemon to offload GPU models and drops current process models."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    offload_file = os.path.join(script_dir, "vox.offload")
    try:
        with open(offload_file, "w", encoding="utf-8") as f:
            f.write(f"offload {time.time()}")
    except Exception:
        pass
    try:
        return offload_gpu_models()
    except Exception:
        return ""


def split_slot(key):
    """Typed priority spec -> (family, variant). 'groq-turbo' -> ('groq', 'turbo').

    'sensevoice-cuda' -> ('sensevoice', 'cuda'); a bare 'sensevoice' -> ('sensevoice', '').
    """
    key = (key or "").strip()
    if "-" in key:
        fam, variant = key.split("-", 1)
        return fam, variant
    return key, ""


# Config key -> priority-slot family name (the two vocabularies differ slightly).
_SLOT_FAMILY = {
    "sensevoice": "sensevoice",
    "groq": "groq",
    "mimo": "mimo",
}
# Only these families ever put a model on the GPU; groq/mimo are pure network calls.
_LOCAL_ASR_FAMILIES = ("sensevoice",)
_DEVICE_SLOTS = ("cuda", "gpu", "cpu")


def gpu_backed_engines(cfg):
    """Which enabled engines would actually load a model onto the GPU.

    Returns a list of (config_key, devices) pairs. Used to DEFER the expensive
    CUDA probe: get_cuda_device_info() can block for tens of seconds when the
    GPU is saturated, and on a pure-cloud priority there is nothing for it to
    answer — the check only matters once a GPU engine is really in the chain.

    A family counts as GPU-backed when either
      * its slot in engine_priority pins a device ('sensevoice-cuda'), or
      * engines.<name>.device is 'cuda', or
      * it is a local ASR family left on 'auto' (auto resolves to cuda at load time).

    ON-DEMAND RULE: only families reachable from `engine_priority` are reported.
    `EngineChain.transcribe` walks that list and nothing else, so an engine that is
    merely `enabled` — but not listed as a slot — is unreachable for normal
    dictation. It stays fully available (the GUI can add it as a Fallback slot, and
    the benchmark loads it explicitly), but it must cost NOTHING at startup: no CUDA
    probe, no model warmup, no torch/funasr import.

    This is what keeps the local engine genuinely "on demand": previously
    `engines.sensevoice.enabled = true` with `device = "cuda"` was enough to make
    every launch probe the GPU even though the priority list was pure cloud.
    """
    cfg = cfg or {}
    engines = cfg.get("engines", {}) or {}

    slot_devs = {}
    reachable_fams = set()
    for slot in cfg.get("engine_priority", []) or []:
        fam, variant = split_slot(slot)
        reachable_fams.add(fam)
        variant = str(variant or "").lower()
        if fam in _LOCAL_ASR_FAMILIES and variant in _DEVICE_SLOTS:
            slot_devs.setdefault(fam, set()).add("cuda" if variant == "gpu" else variant)

    out = []
    for name, ecfg in engines.items():
        if not isinstance(ecfg, dict) or not ecfg.get("enabled", True):
            continue
        fam = _SLOT_FAMILY.get(name, name)
        if fam not in reachable_fams:
            continue  # on demand only — not in the chain, so not worth probing
        devs = set(slot_devs.get(fam, ()))
        if devs:
            # A slot pins the device and wins over engines.<name>.device. A
            # '-cpu' slot is exactly the "keep this off the GPU" case.
            if "cuda" in devs:
                out.append((name, sorted(devs)))
            continue
        dev = str(ecfg.get("device", "auto")).lower()
        if dev in ("cuda", "gpu"):
            out.append((name, ["cuda"]))
        elif dev == "auto" and fam in _LOCAL_ASR_FAMILIES:
            # 'auto' resolves to cuda when a GPU is present -> still need the probe.
            out.append((name, ["auto"]))
    return out


# ─── Failure attribution (B1/B2) ──────────────────────────────────
class TranscriptionError(RuntimeError):
    """Every engine in the chain failed.

    Carries per-slot `attempts` so a failure can be ATTRIBUTED instead of vanishing
    into a single joined log line. `record_perf` consumes this to make silent engine
    degradation visible — the same class of invisibility that let the paste bug hide
    for weeks. Subclasses RuntimeError so existing `except RuntimeError` callers keep
    working unchanged.
    """

    def __init__(self, message, attempts=None, errors=None):
        super().__init__(message)
        self.attempts = attempts or []
        self.errors = errors or []


# (bucket, needles) — ORDER MATTERS: more specific buckets are tested first, so a
# message containing both "429" and "connection" is reported as quota, not network.
_ERROR_BUCKETS = (
    ("auth", ("api key missing", "api key 未配置", "invalid api key", "unauthorized",
              "authentication", "401")),
    ("quota", ("429", "quota", "rate limit", "rate_limit", "exhausted",
               "insufficient_quota", "credits")),
    ("timeout", ("timeout", "timed out", "超时")),
    ("missing", ("not installed", "未安装", "no module named")),
    ("oom", ("out of memory", "outofmemory", "cuda error", "显存")),
    ("network", ("connection", "getaddrinfo", "ssl", "proxy", "unreachable",
                 "network is unreachable", "name resolution")),
    ("too_short", ("too short", "过短", "音频文件过短")),
    ("empty", ("empty text", "returned empty", "无意义", "未识别到语音")),
)


def classify_engine_error(err) -> str:
    """Normalise heterogeneous engine failures into a stable bucket.

    The three providers fail in three different shapes: Groq raises SDK exceptions,
    MiMo raises HTTP-derived RuntimeErrors, and SenseVoice raises model/OOM errors.
    Without normalisation no dashboard can aggregate "why is this engine failing".
    """
    s = str(err or "").lower()
    for bucket, needles in _ERROR_BUCKETS:
        if any(n in s for n in needles):
            return bucket
    return "unknown"


# ─── A1: Dynamic context lexicon (deliberately reduced scope) ─────
class ContextLexicon:
    """Collect likely proper nouns / technical terms to bias the ASR prompt.

    DELIBERATELY REDUCED SCOPE. The upstream inspiration (DSH's "Dynamic
    Context-Aware Lexicon Boosting") also scrapes the CLIPBOARD. That source is
    intentionally NOT used here:

      * privacy — the clipboard routinely holds passwords, keys and tokens, and Vox
        would read it on every single dictation;
      * correctness — Vox's own clipboard-restore logic (`SmartOutput._schedule_restore`)
        rewrites the clipboard shortly after each paste, so what we read before a take
        would frequently be Vox's own previous transcription rather than user intent.

    What remains is cheap, local and side-effect free:
      * the foreground window title (one syscall, already implemented), and
      * recent transcription history (SQLite, cached for 60 s).

    Only LATIN identifiers are harvested from those two sources. CJK runs are taken
    solely from the user-curated static prompt: arbitrary window titles and past
    transcriptions are full of ordinary Chinese words, and injecting those would
    pollute the prompt far more than it would help.
    """

    _LATIN_RX = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}")
    # Curated CJK terms are joined with LIST separators in the prompt. Split on those
    # ONLY — deliberately NOT on whitespace: Chinese has no word spacing, so splitting
    # on spaces turns a prose sentence into grammatical fragments ("用户在讨论",
    # "的开发工作") that would then be injected as if they were terminology. A
    # whitespace-free prose sentence therefore yields no CJK terms at all, which is
    # the correct conservative outcome.
    _CJK_SEG_SPLIT = re.compile(r"[、，,；;。]+")
    _MAX_TOKEN_LEN = 24

    # Words that show up constantly in window titles but are never worth biasing for.
    _STOP = frozenset("""
    the and for with that this from your have will not are you but can all any new
    use get set run let var def class return import none true false
    https http www com html pdf doc docs file files view edit help tools window home
    page google chrome microsoft word excel powerpoint outlook visual studio code
    explorer settings system search task manager folder desktop document downloads
    pictures music videos users program data app apps local temp windows screenshot
    screen capture untitled tab open save close print share zoom meeting calendar
    mail inbox sent draft reply message chat channel server client api url json xml
    read write update delete create build test debug release version index main
    """.split())

    _HISTORY_TTL_S = 60.0
    _history_cache = {"ts": 0.0, "terms": []}
    _history_lock = threading.Lock()

    @classmethod
    def _keep(cls, tok, out, seen):
        if not tok:
            return
        # BUG-11 fix: _from_static deliberately accepts 2-8 character CJK
        # segments, but this guard rejected everything shorter than 3, so
        # curated two-character terms (千问/甘特/行距/圆角…) never reached the
        # prompt. Latin identifiers still require 3 chars to avoid noise.
        is_cjk = all("\u4e00" <= ch <= "\u9fff" for ch in tok)
        min_len = 2 if is_cjk else 3
        if len(tok) > cls._MAX_TOKEN_LEN or len(tok) < min_len:
            return
        low = tok.lower()
        if low in cls._STOP:
            return
        if low.isdigit() or low.replace(".", "").replace("-", "").isdigit():
            return
        if low in seen:
            return
        seen.add(low)
        out.append(tok)

    @classmethod
    def _from_static(cls, cfg):
        """Latin identifiers plus the user's curated CJK terms.

        CJK terms are taken by SPLITTING the prompt on its list delimiters, not by
        regexing arbitrary character runs. A run-based scan happily harvested
        grammatical fragments straight out of the prompt's own prose ("用户在讨论",
        "的开发工作") and would then inject them as if they were terminology. Only
        segments that are entirely CJK and 2-8 characters long qualify.
        """
        text = str((cfg or {}).get("whisper_prompt", "") or "")
        out, seen = [], set()
        for tok in cls._LATIN_RX.findall(text):
            cls._keep(tok, out, seen)
        for seg in cls._CJK_SEG_SPLIT.split(text):
            seg = (seg or "").strip()
            if 2 <= len(seg) <= 8 and all("\u4e00" <= ch <= "\u9fff" for ch in seg):
                cls._keep(seg, out, seen)
        return out

    @classmethod
    def _from_foreground(cls):
        """Latin identifiers from the active window title (project / file names)."""
        out, seen = [], set()
        try:
            title = foreground_window_title()
        except Exception:
            return out
        for tok in cls._LATIN_RX.findall(title or ""):
            cls._keep(tok, out, seen)
        return out

    @classmethod
    def _from_history(cls, limit=20):
        """Latin identifiers from recent transcriptions, cached to avoid a DB read
        on every take."""
        now = time.time()
        with cls._history_lock:
            if now - cls._history_cache["ts"] < cls._HISTORY_TTL_S:
                return list(cls._history_cache["terms"])
        out, seen = [], set()
        try:
            for row in (read_history(limit) or []):
                for tok in cls._LATIN_RX.findall(str(row.get("text", ""))):
                    cls._keep(tok, out, seen)
        except Exception:
            out = []
        with cls._history_lock:
            cls._history_cache["ts"] = now
            cls._history_cache["terms"] = list(out)
        return out

    @classmethod
    def collect(cls, cfg, max_terms=30, budget_chars=200):
        """Weighted, de-duplicated, truncated term list. Never raises.

        Weights: static terms 3 (explicit user intent, must survive truncation),
        foreground title 2 (what the user is looking at right now), history 1.
        """
        try:
            cfg = cfg or {}
            lexical = cfg.get("context_lexicon", {}) or {}
            try:
                max_terms = max(1, int(lexical.get("max_terms", max_terms)))
                budget_chars = max(20, int(lexical.get("budget_chars", budget_chars)))
            except Exception:
                pass

            weighted = []
            for weight, terms in ((3, cls._from_static(cfg)),
                                  (2, cls._from_foreground()),
                                  (1, cls._from_history())):
                for pos, tok in enumerate(terms):
                    weighted.append((weight, -pos, tok))

            # Highest weight first; stable within a source by original order.
            weighted.sort(key=lambda t: (-t[0], t[1]))

            out, seen, used = [], set(), 0
            for _w, _p, tok in weighted:
                low = tok.lower()
                if low in seen:
                    continue
                extra = len(tok) + (1 if out else 0)      # '、' separator
                if len(out) >= max_terms or used + extra > budget_chars:
                    continue
                seen.add(low)
                out.append(tok)
                used += extra

            # Suppress shorter forms fully contained in a longer collected term: with
            # both 'SmartPaste' and its fragments 'Smart'/'Paste' present, only the
            # specific form is worth spending prompt budget on.
            try:
                lowered = [t.lower() for t in out]
                out = [t for i, t in enumerate(out)
                       if not any(i != j and lowered[i] in lowered[j]
                                  for j in range(len(out)))]
            except Exception:
                pass
            return out
        except Exception:
            return []


def build_prompt(cfg):
    """Static whisper_prompt plus a bounded list of context terms (A1).

    MUST return the static prompt byte-for-byte whenever collection yields nothing or
    fails. Prompt content is a known hallucination risk on long audio (the Mac P0/P1
    lesson), so this is only ever allowed to ADD bare nouns, never instructions, and
    it degrades to exactly the old behaviour.
    """
    base = str((cfg or {}).get("whisper_prompt", "") or "")
    try:
        lexical = (cfg or {}).get("context_lexicon", {}) or {}
        if not lexical.get("enabled", True):
            return base
        terms = ContextLexicon.collect(cfg)
    except Exception:
        return base
    if not terms:
        return base
    return f"{base} 本次可能涉及：{'、'.join(terms)}"


class EngineChain:
    """Cascade fallback across engines according to configured priority.

    G1/G2: priority entries are TYPED specs — the user orders explicit fallback
    slots in the GUI (Fallback 1/2/3), each picking one of:
      - "groq-turbo"       Groq whisper-large-v3-turbo (fast/cheap)
      - "groq-l3"          Groq whisper-large-v3 (most accurate)
      - "sensevoice-cuda"  SenseVoice (FunASR) on GPU — stronger Chinese + punctuation
      - "sensevoice-cpu"   SenseVoice on CPU
      - "mimo"              Xiaomi MiMo-V2.5-ASR cloud (zh/en code-switch, dialects,
                            noisy speech; Token Plan quota ~30M credits/hour)
    Bare legacy specs "groq" / "sensevoice" still resolve via the configured
    model/device, so old configs keep working."""

    GROQ_MODEL_MAP = {
        "turbo": "whisper-large-v3-turbo",
        "l3": "whisper-large-v3",
    }

    def __init__(self, cfg):
        self.cfg = cfg
        self.priority = cfg.get("engine_priority", ["groq-turbo"])

    def resolve_spec(self, key):
        """G1: typed spec -> (family, variant). 'groq-turbo' -> ('groq', 'turbo')."""
        return split_slot(key)

    def _make(self, key):
        """Construct engine instance or return None if unavailable."""
        if key == "groq":
            key_val = self._resolve_api_key()
            if not key_val:
                return None
            return GroqEngine(key_val, self.cfg.get("engines", {}).get("groq", {}).get("model"), cfg=self.cfg)
        return None

    def _resolve_api_key(self):
        env_key = os.environ.get("VOX_GROQ_KEY") or os.environ.get("GROQ_API_KEY")
        if env_key:
            return env_key
        return self.cfg.get("engines", {}).get("groq", {}).get("api_key", "").strip()

    def _resolve_mimo_api_key(self):
        env_key = os.environ.get("VOX_MIMO_KEY") or os.environ.get("MIMO_API_KEY")
        if env_key:
            return env_key
        return self.cfg.get("engines", {}).get("mimo", {}).get("api_key", "").strip()

    def transcribe(self, audio_path, language=None, prompt=None):
        errors = []
        attempts = []
        if language is None:
            language = self.cfg.get("language") or self.cfg.get("engines", {}).get("groq", {}).get("language", "zh")
        if prompt is None:
            # A1: dynamic context lexicon (foreground window title + recent history +
            # static terms). Degrades byte-for-byte to the static prompt on any problem.
            prompt = build_prompt(self.cfg)

        def _note(slot, ok, err="", elapsed=None):
            """Record one slot's outcome so the chain's decisions are inspectable."""
            attempts.append({
                "slot": slot,
                "ok": bool(ok),
                "error": (str(err)[:200] if err else ""),
                "error_class": ("" if ok else classify_engine_error(err)),
                "ms": (round(elapsed * 1000, 1) if elapsed is not None else None),
            })

        for key in self.priority:
            _t_slot = time.time()
            try:
                fam, variant = self.resolve_spec(key)
                if fam == "groq":
                    groq_cfg = self.cfg.get("engines", {}).get("groq", {})
                    if not groq_cfg.get("enabled", True):
                        _note(key, False, "disabled in config")
                        continue
                    key_val = self._resolve_api_key()
                    if not key_val:
                        errors.append("groq: API key missing")
                        _note(key, False, "API key missing")
                        continue
                    # G1: the slot variant wins; bare 'groq' uses the configured model.
                    groq_model = self.GROQ_MODEL_MAP.get(
                        variant, groq_cfg.get("model", "whisper-large-v3-turbo"))
                    engine = GroqEngine(key_val, groq_model, cfg=self.cfg)
                    text, lat, cost = engine.transcribe(audio_path, language=language, prompt=prompt)
                    if text:
                        _note(key, True, elapsed=time.time() - _t_slot)
                        return {
                            # F2/F9: include the model id so the bubble badge and perf
                            # tracking can distinguish whisper-large-v3 vs -turbo.
                            "engine": f"groq ({groq_model})",
                            "model": groq_model,
                            "text": text,
                            "latency_s": round(lat, 3),
                            "cost": round(cost, 5),
                            "slot": key,
                            "attempts": attempts,
                        }
                    else:
                        errors.append("groq: returned empty text")
                        _note(key, False, "returned empty text", time.time() - _t_slot)

                elif fam == "mimo":
                    # MiMo-V2.5-ASR cloud engine — free quota under the coding-plan
                    # subscription. Model id fixed upstream; no variant in the slot.
                    mimo_cfg = self.cfg.get("engines", {}).get("mimo", {})
                    if not mimo_cfg.get("enabled", True):
                        _note(key, False, "disabled in config")
                        continue
                    key_val = self._resolve_mimo_api_key()
                    if not key_val:
                        errors.append("mimo: API key missing")
                        _note(key, False, "API key missing")
                        continue
                    engine = MimoEngine(
                        key_val,
                        model=mimo_cfg.get("model"),
                        base_url=mimo_cfg.get("base_url"),
                        cfg=self.cfg,
                    )
                    text, lat, cost = engine.transcribe(audio_path, language=language, prompt=prompt)
                    if text:
                        _note(key, True, elapsed=time.time() - _t_slot)
                        return {
                            "engine": f"mimo ({engine.model})",
                            "model": engine.model,
                            "text": text,
                            "latency_s": round(lat, 3),
                            "cost": round(cost, 5),
                            "slot": key,
                            "attempts": attempts,
                        }
                    else:
                        errors.append("mimo: returned empty text")
                        _note(key, False, "returned empty text", time.time() - _t_slot)

                elif fam == "sensevoice":
                    # G2: SenseVoice via FunASR (GPU/CPU per slot variant).
                    if not _dep_ok("funasr"):
                        errors.append("sensevoice: funasr not installed")
                        _note(key, False, "funasr not installed")
                        continue
                    sv_cfg = self.cfg.get("engines", {}).get("sensevoice", {})
                    device = {"cuda": "cuda", "cpu": "cpu"}.get(
                        variant, sv_cfg.get("device", "auto"))
                    engine = SenseVoiceEngine(device=device,
                                              use_itn=sv_cfg.get("use_itn"))
                    text, lat, cost = engine.transcribe(audio_path, language=language, prompt=prompt)
                    if text:
                        _note(key, True, elapsed=time.time() - _t_slot)
                        return {
                            "engine": f"sensevoice ({SenseVoiceEngine._active_device})",
                            "model": f"SenseVoice ({SenseVoiceEngine._active_device})",
                            "text": text,
                            "latency_s": round(lat, 3),
                            "cost": 0.0,
                            "slot": key,
                            "attempts": attempts,
                        }
                    else:
                        errors.append("sensevoice: returned empty text")
                        _note(key, False, "returned empty text", time.time() - _t_slot)

            except Exception as e:
                errors.append(f"{key}: {e}")
                _note(key, False, e, time.time() - _t_slot)
                log(f"引擎 '{key}' 异常: {e}", "WARN")

        raise TranscriptionError(
            "全部转写引擎均失败: " + " | ".join(errors),
            attempts=attempts, errors=errors,
        )

    def transcribe_preview(self, audio, prompt=None):
        """
        Interim streaming preview transcription for real-time overlay display.

        PERF/VRAM (2026-09): this used to hard-prefer a LOCAL Whisper engine
        "because a local GPU takes ~19ms", so EVERY launch silently pulled
        large-v3 onto the GPU — a 4.2s load on the recording critical path plus
        ~3GB of VRAM pinned for the whole session — even when the user's
        engine_priority was pure cloud.

        The interim engine is EXPLICIT via `streaming.interim_engine`, and it must be
        a cheap LOCAL slot: the daemon re-transcribes the whole growing buffer every
        `streaming.interval_ms` while you hold the key, so a cloud engine here would
        re-upload the audio and burn quota on every tick. It ships enabled as
        "sensevoice-cpu" (CPU RTF 0.064, so a typical take costs a fraction of the
        tick). Remove the key to turn live preview off entirely; a "-cuda" slot is
        only touched when you explicitly ask for one.
        """
        slot = (self.cfg.get("streaming", {}) or {}).get("interim_engine") or ""
        if not slot:
            return ""
        if audio.size < 16000 * 0.35:
            return ""
        wav_path = None
        try:
            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            write_wav(wav_path, audio, 16000)
            lang = self.cfg.get("language") or "zh"
            prompt = prompt or self.cfg.get("whisper_prompt", "")

            interim_cfg = dict(self.cfg)
            interim_cfg["engine_priority"] = [slot]
            res = EngineChain(interim_cfg).transcribe(wav_path, language=lang, prompt=prompt)
            text = res.get("text", "")

            clean = PostProcessor.process(text, self.cfg)
            if clean.endswith("。") or clean.endswith("."):
                clean = clean[:-1]
            return clean
        except Exception:
            return ""
        finally:
            if wav_path and os.path.exists(wav_path):
                try:
                    os.unlink(wav_path)
                except Exception:
                    pass


def benchmark_all_engines(cfg=None, wav_sample=None):
    """
    Benchmarks the configured cloud engines: Groq Cloud (Whisper Large V3 Turbo)
    and MiMo Cloud. Returns structured results for performance monitoring UI.

    NOTE: there is no local-engine row any more. faster-whisper (local_cuda /
    local_cpu) was removed, and SenseVoice is left out of the benchmark on purpose
    so a "run benchmark" click can never pull a local model onto the GPU.
    """
    cfg = cfg or ConfigManager.load()
    if not wav_sample or not os.path.exists(wav_sample):
        wav_sample = os.path.join(SCRIPT_DIR, "tts_test.wav.flac")

    dur_s = 1.0
    if os.path.exists(wav_sample):
        sz = os.path.getsize(wav_sample)
        dur_s = max(0.5, round(sz / 32000.0, 2))

    results = {}

    # 1. Groq Cloud Whisper Large V3 Turbo
    groq_key = EngineChain(cfg)._resolve_api_key()
    _, groq_ok = _dep("groq")       # LAZY: same test as the old _GROQ_OK flag
    if groq_key and groq_ok:
        try:
            eng = GroqEngine(groq_key, "whisper-large-v3-turbo")
            text, lat, _ = eng.transcribe(wav_sample, language="zh")
            sp = round(dur_s / max(0.001, lat), 1)
            results["groq"] = {
                "name": "Groq 云端 (Whisper Large V3 Turbo)",
                "engine": "Groq Turbo (Cloud API)",
                "latency_ms": round(lat * 1000, 1),
                "speedup": sp,
                "text": text,
                "status": "🔵 云端大模型 · 高精准度 · 消耗云算力",
                "recommended": False,
            }
        except Exception as e:
            results["groq"] = {"name": "Groq 云端", "engine": "Groq 云端", "speedup": 0.0, "status": f"失败: {e}"}

    # 2. MiMo Cloud (mimo-v2.5-asr) — coding-plan free quota
    mimo_key = EngineChain(cfg)._resolve_mimo_api_key()
    if mimo_key:
        try:
            m_cfg = cfg.get("engines", {}).get("mimo", {})
            eng = MimoEngine(mimo_key, m_cfg.get("model"), m_cfg.get("base_url"))
            text, lat, _ = eng.transcribe(wav_sample, language="zh")
            sp = round(dur_s / max(0.001, lat), 1)
            results["mimo"] = {
                "name": "MiMo 云端 (MiMo-V2.5-ASR)",
                "engine": "MiMo V2.5 ASR (Cloud API)",
                "latency_ms": round(lat * 1000, 1),
                "speedup": sp,
                "text": text,
                "status": "🟣 中英混读·方言·抗噪 · 30M/小时额度 · 谷价8折(北京00:00-08:00)",
                "recommended": False,
            }
        except Exception as e:
            results["mimo"] = {"name": "MiMo 云端", "engine": "MiMo 云端", "speedup": 0.0, "status": f"失败: {e}"}

    return results


# ─── Post-Processing Pipeline ─────────────────────────────────────
class PostProcessor:
    _HALLUC_PATTERNS = [
        r"https?://[^\s]*youtube(?:-nocookie)?\.[^\s]*(?:\s|$)",
        r"youtu\.be/[^\s]*(?:\s|$)",
        r"(?i)\b(?:don't forget to|please remember to)\b[^.]*like and subscribe[^.]*\.?",
        r"(?i)\b(?:and don't forget|also don't forget)[^.]*subscribe[^.]*\.?",
        r"(?i)\bthanks for watching\.?\b",
        r"(?i)\bthank you for watching\.?\b",
        r"(?i)\bthank you very much\.?\b",
        r"MBC\s*뉴스",
        # Known Whisper silence / low-noise hallucinations (Japanese & Korean)
        r"#?\s*おやすみなさい[。.]*",
        r"#?\s*고춧가루[。.]*",
        r"視聴ありがとうございました[。.]*",
        r"チャンネル登録[。.]*",
        r"ご視聴ありがとうございました[。.]*",
        r"どうぞよろしくお願い(?:します|致します)[。.]*",
        # Chinese & English subtitle / silence hallucinations (Whisper)
        r"请不吝点赞\s*订阅\s*转发.*",
        r"感谢(?:您的)?(?:收看|观看|收听).*",
        r"欢迎(?:点击)?订阅.*",
        r"请(?:点赞|关注|收藏|投币|转发).*",
        r"(?i)\bMING PAO\b.*",
        r"字幕志愿者.*",
        r"广告谢谢观看.*",
        r"(?:感谢|谢谢)观看.*",
        r"以下是用户(?:内|的)?语音输入.*",
    ]
    _HALLUC_RE = [re.compile(p) for p in _HALLUC_PATTERNS]
    _DUP_PUNCT = re.compile(r"([，。！？；：,.!?;:])\1+")
    _CH_END = set("。！？；：,.!?;：；")

    _repl_cache = None
    _repl_mtime = 0
    _repl_path = None
    # A5: bounds on the regex rule set. The file is user-authored and local (same trust
    # level as config.json), but a runaway pattern would stall a dictation, so keep the
    # surface small and reject obvious typos instead of hanging.
    _REPL_REGEX_MAX = 200
    _REPL_REGEX_MAX_LEN = 500

    @classmethod
    def load_replacements(cls, file_path=REPLACEMENTS_PATH):
        """Load correction rules as [(compiled_pattern, replacement, is_regex), ...].

        Two rule kinds; the first preserves the original behaviour exactly:

        * `confirmed` / `high_probability` — LITERAL terms. The key is escaped, so
          "C++" matches those four characters, as before.
        * `regex` (A5, new) — the key is compiled as a REAL pattern, so rules can
          express alternation, optional characters and groups:
              {"regex": {"(接|解)口文档": "接口文档",
                         "\\\\bqwen\\\\s?3\\\\b": "Qwen3"}}
          Group references work in both `\\\\1` and `$1` form. Case-insensitivity is
          opt-in per pattern via an inline `(?i)`.

        Literals are applied first (longest key first) and regexes afterwards, so an
        exact term is always corrected before a broader pattern can touch it.
        """
        if not file_path or not os.path.exists(file_path):
            return []
        try:
            mtime = os.path.getmtime(file_path)
            # The cache key must include the PATH, not just the mtime: two different
            # rule files can easily share a timestamp, and the old key would then
            # serve one file's rules for the other.
            if (cls._repl_cache is not None and mtime == cls._repl_mtime
                    and file_path == cls._repl_path):
                return cls._repl_cache
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            literal = {}
            for section in ("confirmed", "high_probability"):
                blk = data.get(section, {})
                if isinstance(blk, dict):
                    literal.update(blk)

            # is_regex=False: the replacement is inserted VERBATIM (see process()), so
            # a literal correction containing "\\1" can no longer raise
            # "invalid group reference" and abort a transcription.
            compiled = []
            items = sorted(literal.items(), key=lambda kv: len(str(kv[0])), reverse=True)
            for k, v in items:
                if not isinstance(k, str) or not k:
                    continue
                try:
                    compiled.append((re.compile(re.escape(k), re.IGNORECASE), str(v), False))
                except Exception:
                    continue

            regex_blk = data.get("regex", {})
            if isinstance(regex_blk, dict):
                accepted = 0
                for pat, rep in regex_blk.items():
                    if str(pat).startswith("_"):
                        continue                      # allow "_comment" style keys
                    if not isinstance(pat, str) or not pat:
                        continue
                    if accepted >= cls._REPL_REGEX_MAX:
                        log(f"replacements.json: regex 规则超过 {cls._REPL_REGEX_MAX} 条，其余已忽略", "WARN")
                        break
                    if len(pat) > cls._REPL_REGEX_MAX_LEN:
                        log(f"replacements.json: regex 规则过长已跳过 ({len(pat)} 字符)", "WARN")
                        continue
                    # JS-style $1 group references are common; normalise to Python \1.
                    value = re.sub(r"\$(\d+)", r"\\\1", str(rep))
                    try:
                        compiled.append((re.compile(pat), value, True))
                    except re.error as e:
                        # Name the file that was actually read. This used to hardcode
                        # "replacements.json", so a bad pattern in ANY other path (a
                        # temp file, a test fixture, an alternate config) was reported
                        # against the user's real file — which then looked corrupted
                        # when it was untouched.
                        log(f"{os.path.basename(str(file_path))}: 无效 regex 已跳过 "
                            f"{pat!r}: {e}", "WARN")
                        continue
                    accepted += 1

            cls._repl_cache = compiled
            cls._repl_mtime = mtime
            cls._repl_path = file_path
            return cls._repl_cache
        except Exception:
            return []

    _RESTORE_PUNCT = set("，。！？；：、,.!?;:")
    _CONJUNCTIONS = [
        "然后", "但是", "不过", "所以", "因为", "如果", "那么",
        "而且", "或者", "就是", "其实", "目前", "另外", "同时",
        "接下来", "首先", "最后", "总之", "比如", "可能",
        "不然", "否则", "虽然", "尽管", "既然",
        # v2: more safe discourse markers (2026-09-21, user: 标点符号有点问题)
        "一旦", "每当", "无论", "只要", "只有", "除了", "关于", "对于",
        "至于", "相比", "可见", "显然", "说白了", "换句话说", "也就是说", "要是",
    ]

    @classmethod
    def _restore_punctuation(cls, text, cfg=None):
        """Insert punctuation into text that has very little or none (B1).

        Ported from the macOS VOX `add_chinese_punctuation` mechanism:
        - skip when text is short (<10 chars) or already densely punctuated
          (char/punct < 25 — v2: Whisper often emits only ~1 comma/36 chars, which
          users still read as "no punctuation"; only truly dense text is skipped)
        - insert "，" before discourse conjunctions (然后/但是/不过/...)
        - v2: insert "，" before ordinal enumerations (第一步/第二个/...)
        - v2: insert "，" at the 的话 clause boundary (…的话是/就/还…)
        - insert "。" after sentence-final particles (了/吧/啊/呢/吗/嘛/点) when a
          pronoun/demonstrative follows
        - append a trailing "。" ONLY when fidelity mode asks for it (auto_period/polished)
        """
        if not text:
            return text
        punct_count = sum(1 for c in text if c in cls._RESTORE_PUNCT)
        char_count = len(text)
        if char_count < 10 or (punct_count > 0 and char_count / punct_count < 25):
            return text

        for conj in cls._CONJUNCTIONS:
            text = re.sub(rf"([\u4e00-\u9fff]){conj}", rf"\1，{conj}", text)
        # Ordinal enumeration (第一步/第二个/第3点...) starts a new list item.
        text = re.sub(r"([\u4e00-\u9fff])(第[一二三四五六七八九十百0-9]+[个步点项条层])", r"\1，\2", text)
        # 的话 clause boundary before a copula/auxiliary verb.
        text = re.sub(r"([\u4e00-\u9fff])的话(?=[是就还也可能会要能])", r"\1的话，", text)
        text = re.sub(r"([了吧啊呢吗嘛点])([我你他她它们这那])", r"\1。\2", text)
        # v2: sparse text reads as one long breath — promote commas to periods when a
        # clause has accumulated enough weight, restoring sentence rhythm (user: 句号
        # 太少了). Only runs here, i.e. on sparse text that already passed the guard.
        if "，" in text:
            parts = text.split("，")
            if len(parts) >= 3:
                out = []
                acc = 0
                for i, p in enumerate(parts):
                    out.append(p)
                    acc += len(p)
                    if i < len(parts) - 1:
                        if acc >= 22:
                            out.append("。")
                            acc = 0
                        else:
                            out.append("，")
                text = "".join(out)

        auto_period = False
        if cfg:
            fid = cfg.get("fidelity", {})
            auto_period = bool(fid.get("auto_period", False) or fid.get("mode") == "polished")
        if auto_period and text and text[-1] not in cls._RESTORE_PUNCT:
            text += "。" if any("\u4e00" <= c <= "\u9fff" for c in text) else "."
        return text

    @classmethod
    def _normalize_punctuation(cls, text):
        """Normalize punctuation artifacts (B1 inconsistency fix).

        Ported from the macOS VOX `normalize_punctuation` with two enhancements for
        Windows: half-width→full-width conversion happens only in a Chinese context
        (adjacent CJK char), so tech tokens like `v3.0`, `API.` and decimals stay intact.
        Fixes: ，。→。, ，、→、, 、，→，, 。，→。, half-width→full-width, collapsed dupes.
        """
        if not text:
            return text

        # Half-width comma/period → full-width only adjacent to CJK characters.
        text = re.sub(r"([\u4e00-\u9fff]),", r"\1，", text)
        text = re.sub(r",([\u4e00-\u9fff])", r"，\1", text)
        text = re.sub(r"([\u4e00-\u9fff])\.", r"\1。", text)
        text = re.sub(r"\.([\u4e00-\u9fff])", r"。\1", text)

        # Fix double punctuation combos (model artifacts)
        text = re.sub(r"，。", "。", text)
        text = re.sub(r"，、", "、", text)
        text = re.sub(r"、，", "，", text)
        text = re.sub(r"。，", "。", text)

        # Collapse repeated punctuation
        text = re.sub(r"，{2,}", "，", text)
        text = re.sub(r"。{2,}", "。", text)
        text = re.sub(r"、{2,}", "、", text)

        # Remove a comma right before sentence-ending punctuation
        text = re.sub(r"，([。！？])", r"\1", text)
        return text

    @classmethod
    def process(cls, text, cfg=None):
        if not text:
            return ""

        # 1. Traditional to Simplified Chinese. zhconv is a LAZY dep: it is only
        # imported for text that actually contains CJK (same gate as the old
        # `_HAS_ZHCONV and any(...)` test, just without paying the import first).
        if any("\u4e00" <= c <= "\u9fff" for c in text):
            zhconv, zh_ok = _dep("zhconv")
            if zh_ok:
                try:
                    text = zhconv.convert(text, "zh-cn")
                except Exception:
                    pass

        # 2. Hallucination Removal
        for rx in cls._HALLUC_RE:
            text = rx.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()

        # 2.1 Foreign script noise filter (Japanese Kana / Korean Hangul)
        cfg_lang = (cfg or {}).get("language") or (cfg or {}).get("engines", {}).get("groq", {}).get("language", "zh")
        if str(cfg_lang).lower() in ("zh", "zh-cn", "auto", ""):
            # Strip Japanese kana (\u3040-\u30ff) and Korean hangul (\uac00-\ud7a3)
            cleaned_script = re.sub(r"[\u3040-\u30ff\uac00-\ud7a3]+", "", text)
            # Strip dangling leading hash or punctuation left by hallucinations
            cleaned_script = re.sub(r"^[#\s,.:;，。！？]+", "", cleaned_script).strip()
            # If no meaningful Chinese character or alphanumeric word is left, treat as pure noise hallucination
            has_valid = any(("\u4e00" <= c <= "\u9fff") or c.isalnum() for c in cleaned_script)
            if not has_valid:
                return ""
            text = cleaned_script

        # 3. Replacements Dictionary
        repl_file = (cfg or {}).get("replacements_file", REPLACEMENTS_PATH)
        if not os.path.isabs(repl_file):
            repl_file = os.path.join(SCRIPT_DIR, repl_file)
        repl_rules = cls.load_replacements(repl_file)
        if repl_rules:
            for pat, rep, is_regex in repl_rules:
                try:
                    if is_regex:
                        text = pat.sub(rep, text)          # supports \1 / $1 groups
                    else:
                        # Function form: a literal correction is inserted verbatim, so
                        # it can never be reinterpreted as a group reference.
                        text = pat.sub(lambda m, _r=rep: _r, text)
                except Exception as e:
                    # One bad rule must never abort a transcription.
                    log(f"词表规则应用失败已跳过 {pat.pattern!r}: {e}", "DEBUG")

        # 4.5 Punctuation Restoration (ported from macOS VOX add_chinese_punctuation):
        # long continuous dictation often arrives with sparse/zero punctuation. Insert
        # commas at conjunction boundaries and periods after sentence-final particles.
        # The trailing period respects fidelity mode (verbatim keeps colloquial endings).
        text = cls._restore_punctuation(text, cfg)
        # 4.6 Punctuation artifact normalization (macOS VOX normalize_punctuation,
        # enhanced): half-width→full-width in Chinese context, "，。"→"。", dupes.
        text = cls._normalize_punctuation(text)

        # 4. Punctuation Fixup
        # Remove whitespace before punctuation
        text = re.sub(r"\s+([，。！？；：,.!?;:])", r"\1", text)
        # Collapse multiple punctuation (including duplicate dots & commas)
        text = re.sub(r"\.{2,}", ".", text)
        text = re.sub(r"([，。！？；：,;.!?:])\1+", r"\1", text)
        text = re.sub(r"[，,]+([。！？；：!?;:])", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        # Fidelity mode check: verbatim mode preserves colloquial ending without forced trailing period
        auto_period = False
        if cfg:
            fid = cfg.get("fidelity", {})
            auto_period = bool(fid.get("auto_period", False) or fid.get("mode") == "polished")
        if auto_period and text:
            last = text[-1]
            if last not in cls._CH_END and last.isalnum():
                if any("\u4e00" <= c <= "\u9fff" for c in text):
                    text += "。"
                else:
                    text += "."

        return text.strip()


# ─── Smart Output & Clipboard Preservation ────────────────────────
class SmartOutput:
    """
    Win32 Output Injector (dependency-free).

    Backed by `vox_win32`, which talks to the Win32 API through ctypes. pywin32 is
    no longer required: this used to be the tool's worst failure mode, because
    `import win32clipboard` failing only flipped `_WIN_OK` to False while every
    clipboard call kept returning a harmless-looking `False`. Transcription still
    worked, so the tool *looked* alive while nothing was ever copied or pasted.

    Guarantees now:
    - Every failure returns a truthful `hint`/`error` instead of silence.
    - The transcription text is persisted to disk if the clipboard is unusable,
      so a take can never be lost.
    - Ctrl+V is injected with SendInput, releasing stray Win/Shift/Alt modifiers
      first (the default hotkey is Ctrl+Win, so Win is often still held).
    - The previous clipboard is restored only when it held nothing but text,
      so a copied file or image is never clobbered by a plain string.
    """

    # Clipboard formats whose presence means "restoring text would lose data".
    _RICH_FORMATS = frozenset({
        2,   # CF_BITMAP
        3,   # CF_METAFILEPICT
        8,   # CF_DIB
        14,  # CF_ENHMETAFILE
        15,  # CF_HDROP — copied files/folders
        17,  # CF_DIBV5
    })

    _restore_lock = threading.Lock()
    _restore_token = 0

    @staticmethod
    def backend():
        """Clipboard/paste backend health (surfaced by diagnostics + the GUI)."""
        return _win32.status()

    @staticmethod
    def get_clipboard_text():
        return _win32.get_clipboard_text()

    @staticmethod
    def set_clipboard_text(text):
        ok = _win32.set_clipboard_text(text)
        if not ok:
            log(f"⚠️ 写入剪贴板失败: {_win32.last_error()}", "ERROR")
        return bool(ok)

    @classmethod
    def clipboard_formats(cls):
        """Format ids currently on the clipboard (empty set when unreadable)."""
        return _win32.clipboard_formats()

    @classmethod
    def clipboard_is_text_only(cls):
        """True when restoring text cannot destroy richer clipboard content.

        Copying a *file* (CF_HDROP), an image (CF_BITMAP/CF_DIB) or HTML also
        puts a text flavour on the clipboard. Blindly restoring that text
        flavour after a transcription silently replaces the copied file/image
        with a plain string — real user data loss. So the restore path only runs
        when the clipboard genuinely held nothing but text.
        """
        fmts = cls.clipboard_formats()
        if not fmts:
            return True
        return not (fmts & cls._RICH_FORMATS)

    @staticmethod
    def simulate_ctrl_v():
        ok = _win32.send_paste()
        err = _win32.last_error()
        if not ok:
            log(f"⚠️ Ctrl+V 注入失败: {err}", "WARN")
        elif err:
            # send_paste succeeded but could not restore a physically held
            # modifier. Never report this as a failed paste (would invite a
            # second Ctrl+V and duplicate the text) — just make it visible.
            log(f"⚠️ Ctrl+V 已注入，但后处理异常: {err}", "WARN")
        return bool(ok)

    # Cached (rid, name) of THIS process's integrity level; None until probed.
    # A process's integrity level cannot change while it runs, so probe once.
    _IL_CACHE = None

    @classmethod
    def _integrity_ok_cached(cls):
        """True unless THIS process verifiably runs below Medium.

        2026-11-18 incident: UIPI silently drops SendInput from a Low-integrity
        process when the foreground window belongs to a Medium process, and
        SendInput still reports success — that is exactly the "pasted: true
        but nothing landed" the user saw. This probe exists so the hint can
        tell the truth if the daemon ever finds itself Low again (it should
        not: the .venv interpreters were relabeled Medium). Unreadable (None)
        counts as OK — never nag when the probe itself fails.
        """
        if cls._IL_CACHE is None:
            try:
                cls._IL_CACHE = _win32.integrity_level()
            except Exception:
                cls._IL_CACHE = (None, "unknown")
        rid = cls._IL_CACHE[0]
        return rid is None or rid >= 0x2000

    # Terminal / emulator targets where Ctrl+V frequently does NOT inject text
    # (they need Ctrl+Shift+V / Shift+Ins / auto-read the clipboard). See complaint #9.
    _TERMINAL_MARKERS = (
        "cmd", "powershell", "pwsh", "wsl", "bash", "putty", "securecrt",
        "xshell", "xmanager", "mobaterm", "netterm", "iterm", "ghost", "mintty",
    )

    @staticmethod
    def _looks_like_terminal(title):
        """Pure heuristic: does the target window look like a terminal/emulator where
        Ctrl+V typically fails to inject text?"""
        t = (title or "").lower()
        if not t:
            return False
        return any(m in t for m in SmartOutput._TERMINAL_MARKERS)

    @classmethod
    def select_method(cls, foreground_title=None, cfg=None):
        """Decide the paste strategy from the target context. Pure + testable.
        - An explicit config override wins ("auto" opts back into detection).
        - A terminal-like foreground -> clipboard_only (safer; user can Ctrl+Shift+V).
        - Otherwise -> smart_paste (Ctrl+V)."""
        cfg = cfg or {}
        override = cfg.get("output_method")
        if override in ("smart_paste", "clipboard_only"):
            return override
        if cls._looks_like_terminal(foreground_title):
            return "clipboard_only"
        return "smart_paste"

    @classmethod
    def _schedule_restore(cls, orig_text, delay_s=0.35):
        """Put the user's previous clipboard text back after the target app has
        had time to read ours.

        A monotonic token guards the race the old code had: two transcriptions
        arriving within the delay window meant the *first* restore could wipe the
        *second* transcription before the user pasted it. Only the newest
        scheduled restore is allowed to run.
        """
        with cls._restore_lock:
            cls._restore_token += 1
            my_token = cls._restore_token

        def worker():
            time.sleep(delay_s)
            with cls._restore_lock:
                if my_token != cls._restore_token:
                    return  # superseded by a newer transcription
            try:
                _win32.set_clipboard_text(orig_text)
            except Exception:
                pass

        threading.Thread(
            target=worker, daemon=True, name="vox-clipboard-restore"
        ).start()

    @classmethod
    def _emergency_dump(cls, text, res):
        """Last-resort sink so a transcription is never lost to a broken clipboard."""
        try:
            path = os.path.join(SCRIPT_DIR, "_last_transcript.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            res["saved_to"] = path
            res["hint"] += f" · 已暂存到 {os.path.basename(path)}"
        except Exception as e:
            log(f"暂存转写文本失败: {e}", "ERROR")

    @classmethod
    def output(cls, text, method=None, restore_clipboard=True,
               foreground_title=None, cfg=None):
        """Write + optionally paste a transcription.

        Returns {method, copied, pasted, hint, error, saved_to?}. `method=None`
        means "decide from context"; an explicit "smart_paste"/"clipboard_only"
        is honoured as-is.

        Reliability contract (this is what the old version broke):
        * never report success it did not achieve;
        * always tell the caller *why* it failed;
        * never lose the text — it is dumped to disk if the clipboard is dead.
        """
        res = {"method": None, "copied": False, "pasted": False,
               "hint": "", "error": ""}
        if not text:
            return res

        if method in ("smart_paste", "clipboard_only"):
            chosen = method
        else:
            chosen = cls.select_method(foreground_title, cfg)
        res["method"] = chosen

        if chosen == "clipboard_only":
            res["copied"] = cls.set_clipboard_text(text)
            if res["copied"]:
                res["hint"] = "已复制到剪贴板 · 请按 Ctrl+V 粘贴"
            else:
                res["error"] = _win32.last_error() or "剪贴板后端未就绪"
                res["hint"] = f"复制到剪贴板失败：{res['error']}"
                cls._emergency_dump(text, res)
            return res

        # ── smart_paste ───────────────────────────────────────────
        # Only snapshot for restore when the clipboard is text-only, so a copied
        # file/image is never destroyed by the text-flavour round trip.
        safe_restore = restore_clipboard and cls.clipboard_is_text_only()
        orig_text = cls.get_clipboard_text() if safe_restore else None
        if restore_clipboard and not safe_restore:
            log("剪贴板含文件/图片等非纯文本内容，已跳过自动还原以免覆盖它", "DEBUG")

        res["copied"] = cls.set_clipboard_text(text)

        if res["copied"]:
            # Give Windows a moment to publish the new clipboard contents.
            time.sleep(0.02)
            res["pasted"] = cls.simulate_ctrl_v()
            if res["pasted"] and _win32.last_error():
                extra = _win32.last_error()
                res["hint"] = (res["hint"] + " · " + extra).strip(" ·")
            # UIPI tripwire (2026-11-18): below Medium, SendInput can be
            # silently swallowed while still reporting success. Surface that
            # possibility instead of letting the user find an empty cursor.
            if res["pasted"] and not cls._integrity_ok_cached():
                warn = ("⚠️ 当前进程为 Low 完整性：自动粘贴可能已被系统拦截，"
                        "内容已复制到剪贴板，请手动 Ctrl+V")
                res["hint"] = (warn + " · " + res["hint"]).strip(" ·") if res["hint"] else warn

        if res["copied"] and safe_restore and orig_text and orig_text != text:
            cls._schedule_restore(orig_text)

        if not res["copied"]:
            res["error"] = _win32.last_error() or "剪贴板后端未就绪"
            res["hint"] = f"粘贴失败：无法写入剪贴板（{res['error']}）"
            cls._emergency_dump(text, res)
        elif not res["pasted"]:
            res["hint"] = "自动粘贴未生效 · 内容已在剪贴板，请按 Ctrl+V"

        return res


def foreground_window_title():
    """Best-effort title of the current foreground top-level window. Used to detect a
    terminal/emulator target so the paste strategy can switch to clipboard-only.
    Returns "" on any failure (non-Windows, no GUI session, permission denied)."""
    return _win32.foreground_window_title()


# ─── Config Manager (Thread-Safe & Hot-Reload) ────────────────────
DEFAULT_CONFIG = {
    "hotkey": {
        "modifiers": ["shift"],
        "key": "",
        "mode": "adaptive",  # "adaptive" (hold or toggle), "hold_only", "toggle_only"
    },
    "engine_priority": ["groq-l3", "groq-turbo", "mimo", "sensevoice-cpu"],
    "output_method": "smart_paste",
    "restore_clipboard": True,
    "sound_effects": True,
    "audio": {
        "sample_rate": 16000,
        "max_recording_seconds": 300,
        "upload_format": "flac",
        "preferred_device": None,
    },
    "engines": {
        "groq": {
            "enabled": True,
            "model": "whisper-large-v3-turbo",
            "language": "auto",
            "api_key": "",
        },
        "mimo": {
            "enabled": True,
            "model": "mimo-v2.5-asr",
            "base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
            "language": "auto",
            "api_key": "",
        },
        "sensevoice": {
            # The single retained local engine (CPU-capable, RTF ~0.04 on CPU).
            # Not in the default engine_priority, so it costs nothing unless the user
            # adds a sensevoice-* slot or points streaming.interim_engine at it.
            "enabled": True,
            "model": "iic/SenseVoiceSmall",
            "device": "cpu",
            "language": "auto",
            # ITN is what actually produces SenseVoice's built-in punctuation.
            "use_itn": True,
            # Preload the weights at daemon start? DEFAULT FALSE, and the reason is
            # measured, not stylistic: a warm SenseVoice costs ~3.2 GB of RESIDENT RAM
            # (WorkingSet 21 MB -> 3190 MB, measured), and unloading it gives almost
            # nothing back — del + gc.collect() + 3x gc + msvcrt._heapmin returned
            # 16 MB of 3168 MB, because CPython's allocator and torch's native pools
            # keep freed memory in the process. So "warm now, free later" is not a
            # trade this process can make; the only way to hand the RAM back is for
            # the process holding it to exit.
            #
            # With this False the engine loads on demand — only if the whole cloud
            # chain fails — and the daemon then sits at ~30 MB instead of 3.2 GB.
            # Set True if you would rather pay 3.2 GB permanently and never wait.
            "warmup": False,
        },
    },
    "replacements_file": "replacements.json",
    "history_dir": "history",
    # Live interim preview while recording.
    #
    # This section was READ by both the daemon and the GUI but was absent from
    # DEFAULT_CONFIG, so a fresh install ran with `.get(..., True)` -> enabled and no
    # interim engine. Now it is part of the schema.
    #
    # interim_engine is EMPTY by default. Pointing it at sensevoice-* forces the local
    # model to be resident (see `warmup` above); pointing it at a cloud engine spends
    # API calls on previews that compete with the REAL transcription for the same
    # rate limit, so a preview burst can starve the take it is previewing. Leave it
    # empty unless you have quota to spare.
    "streaming": {
        "enabled": False,
        "interval_ms": 500,
        "interim_engine": "",
    },
    # A1: dynamic context lexicon. Reads the foreground window title and recent
    # history ONLY (never the clipboard) and appends bare nouns to the prompt.
    "context_lexicon": {
        "enabled": True,
        "max_terms": 30,
        "budget_chars": 200,
    },
    # Boot autostart. The AUTHORITATIVE state is the Startup-folder .bat the GUI
    # writes (and install_autostart.bat now writes) — this key only records the
    # intent, because a missing key made the GUI switch read "off" while autostart
    # was in fact enabled by the .bat scripts.
    "system": {
        "auto_start": False,
    },
    "overlay": {
        "enabled": True,
        "position": "bottom_center",  # "bottom_center", "bottom_right"
        "base_width": 480,            # slim default pill width (logical px, DPI-scaled)
        "base_height": 52,
        "custom_width": None,         # user-dragged width; overrides base_width when set
        # E1: user-adjustable bubble appearance (admin panel 气泡外观 card).
        "ui": {
            "font_scale": 1.0,        # multiplier on design font size (0.6 - 2.0)
            "line_scale": 1.0,        # multiplier on design line spacing (0.7 - 2.0)
            "opacity": 85,            # window alpha, % (40 - 100)
            # F6: done-state display durations (ms) before the bubble auto-hides.
            "done_short_ms": 2000,    # <= 40 chars
            "done_long_ms": 5000,     # > 40 chars
        },
    },
}

class ConfigManager:
    _lock = threading.Lock()
    _current_cfg = None
    _last_mtime = 0
    _change_callbacks = []

    @classmethod
    def load(cls):
        with cls._lock:
            if not os.path.exists(CONFIG_PATH):
                cls._current_cfg = copy.deepcopy(DEFAULT_CONFIG)
                safe_write_json(CONFIG_PATH, cls._current_cfg)
                cls._last_mtime = os.path.getmtime(CONFIG_PATH)
                return cls._current_cfg

            try:
                mtime = os.path.getmtime(CONFIG_PATH)
                if cls._current_cfg is None or mtime != cls._last_mtime:
                    loaded = safe_read_json(CONFIG_PATH, {})
                    # Merge with defaults
                    # BUG-17: deep copy — a shallow copy shares nested dicts
                    # with DEFAULT_CONFIG, so mutating cfg["engines"][...] in
                    # memory silently corrupted the defaults for later loads.
                    merged = copy.deepcopy(DEFAULT_CONFIG)
                    for k, v in loaded.items():
                        if isinstance(v, dict) and isinstance(merged.get(k), dict):
                            merged[k] = {**merged[k], **v}
                        else:
                            merged[k] = v
                    # Prune engine entries the code no longer implements.
                    #
                    # Why this exists: DEFAULT_CONFIG is merged in above and the whole
                    # merged dict is written back by save(). So a stale entry in
                    # DEFAULT_CONFIG RESURRECTS itself into the user's config.json on
                    # every save — and the overlay saves config.json whenever the bubble
                    # is dragged. That is how the removed faster-whisper "local" engine
                    # kept reappearing after it had been deleted from the codebase.
                    # Unknown engines can never be selected, so dropping them is safe
                    # and self-healing.
                    try:
                        known_engines = set((DEFAULT_CONFIG.get("engines") or {}).keys())
                        engs = merged.get("engines")
                        if isinstance(engs, dict):
                            for _name in [n for n in list(engs)
                                          if n not in known_engines and not str(n).startswith("_")]:
                                engs.pop(_name, None)
                    except Exception:
                        pass
                    # B12: transparently decrypt the at-rest cloud keys (Groq + MiMo) so
                    # every in-memory consumer sees plaintext. Only ciphertext
                    # (dpapi: marker) is touched; legacy plaintext keys pass through
                    # unchanged until the next save.
                    for _ek in ("groq", "mimo"):
                        _e = merged.get("engines", {}).get(_ek, {})
                        if isinstance(_e, dict) and _e.get("api_key"):
                            _e["api_key"] = maybe_decrypt(_e["api_key"])
                    cls._current_cfg = merged
                    cls._last_mtime = mtime
            except Exception:
                if cls._current_cfg is None:
                    cls._current_cfg = copy.deepcopy(DEFAULT_CONFIG)

            return cls._current_cfg

    @classmethod
    def save(cls, cfg):
        with cls._lock:
            cls._current_cfg = cfg
            # B12: never write the Groq key to disk in plaintext. Encrypt a deep copy for
            # storage only; the caller's (in-memory) cfg keeps the plaintext key intact so
            # downstream readers are unaffected. Empty / unset keys are left as-is.
            to_write = copy.deepcopy(cfg)
            for _ek in ("groq", "mimo"):
                _e = to_write.get("engines", {}).get(_ek, {})
                if isinstance(_e, dict) and _e.get("api_key"):
                    _e["api_key"] = maybe_encrypt(str(_e["api_key"]))
            safe_write_json(CONFIG_PATH, to_write)
            try:
                cls._last_mtime = os.path.getmtime(CONFIG_PATH)
            except Exception:
                pass
            for cb in cls._change_callbacks:
                try:
                    cb(cfg)
                except Exception:
                    pass

    @classmethod
    def register_change_callback(cls, cb):
        if cb not in cls._change_callbacks:
            cls._change_callbacks.append(cb)


# ─── Windows Taskbar & System Tray Integration ────────────────────
ICO_PATH = os.path.join(SCRIPT_DIR, "vox.ico")

def ensure_vox_ico(ico_path=ICO_PATH):
    """Generates multi-resolution vox.ico if it doesn't already exist."""
    if os.path.exists(ico_path) and os.path.getsize(ico_path) > 1000:
        return ico_path
    try:
        from PIL import Image, ImageDraw
        sizes = [16, 32, 48, 64, 128, 256]
        images = []
        for s in sizes:
            img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            r = max(2, s // 5)
            bg = (16, 185, 129, 255)       # Emerald Green
            outline = (5, 150, 105, 255)
            d.rounded_rectangle([1, 1, s - 2, s - 2], radius=r, fill=bg, outline=outline, width=max(1, s // 32))

            # Mic body
            mx0 = int(s * 0.38)
            mx1 = int(s * 0.62)
            my0 = int(s * 0.22)
            my1 = int(s * 0.52)
            mrad = max(2, (mx1 - mx0) // 2)
            d.rounded_rectangle([mx0, my0, mx1, my1], radius=mrad, fill=(255, 255, 255, 255))

            # Mic cradle arc
            arc_x0 = int(s * 0.28)
            arc_x1 = int(s * 0.72)
            arc_y0 = int(s * 0.34)
            arc_y1 = int(s * 0.62)
            lw = max(1, s // 18)
            d.arc([arc_x0, arc_y0, arc_x1, arc_y1], start=0, end=180, fill=(255, 255, 255, 255), width=lw)

            # Stem & Base
            stem_top = int(s * 0.62)
            stem_bot = int(s * 0.76)
            d.line([(s // 2, stem_top), (s // 2, stem_bot)], fill=(255, 255, 255, 255), width=lw)
            base_x0 = int(s * 0.34)
            base_x1 = int(s * 0.66)
            d.line([(base_x0, stem_bot), (base_x1, stem_bot)], fill=(255, 255, 255, 255), width=lw)

            images.append(img)

        images[-1].save(ico_path, format="ICO", sizes=[(s, s) for s in sizes])
        return ico_path
    except Exception as e:
        log(f"ensure_vox_ico error: {e}", "WARN")
        return None


def promote_tray_icon_in_windows():
    """
    Ensures Vox system tray icon is promoted to visible taskbar in Windows 11 / 10
    by setting IsPromoted=1 in HKCU\\Control Panel\\NotifyIconSettings.
    This prevents Windows from hiding the icon inside the '^' overflow chevron.
    """
    # NOTE: this only needs the stdlib `winreg`, so it must NOT be gated on
    # _WIN_OK (which tracks pywin32). Gating it there meant that dropping pywin32
    # silently disabled tray-icon promotion — a feature with no relation to it.
    if sys.platform != "win32":
        return 0
    import winreg
    base = r"Control Panel\NotifyIconSettings"
    promoted_count = 0
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base, 0, winreg.KEY_READ | winreg.KEY_WRITE)
        num_subkeys = winreg.QueryInfoKey(key)[0]
        subkeys = [winreg.EnumKey(key, i) for i in range(num_subkeys)]
        for sk in subkeys:
            try:
                sub = winreg.OpenKey(key, sk, 0, winreg.KEY_READ | winreg.KEY_WRITE)
                try:
                    exe, _ = winreg.QueryValueEx(sub, "ExecutablePath")
                except Exception:
                    exe = ""
                try:
                    tip, _ = winreg.QueryValueEx(sub, "InitialTooltip")
                except Exception:
                    tip = ""

                exe_lower = str(exe).lower()
                tip_lower = str(tip).lower()
                is_vox = "vox" in tip_lower or "vox" in exe_lower or ("python" in exe_lower and "vox" in SCRIPT_DIR.lower())
                if is_vox:
                    winreg.SetValueEx(sub, "IsPromoted", 0, winreg.REG_DWORD, 1)
                    promoted_count += 1
                winreg.CloseKey(sub)
            except Exception:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        log(f"promote_tray_icon_in_windows error: {e}", "DEBUG")
    return promoted_count

