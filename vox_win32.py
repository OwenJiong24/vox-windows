"""Vox Win32 primitives — clipboard + keyboard injection via ctypes (stdlib only).

Why this module exists
----------------------
The original implementation used pywin32 (`win32clipboard` / `win32api.keybd_event`).
That created a silent, catastrophic failure mode: `vox_core.py` wraps the pywin32
import in `try/except` and sets `_WIN_OK = False`, then every clipboard operation
returns a bare `False`. When Vox happened to launch under an interpreter that did
not have pywin32 installed (e.g. the system `C:\\Python314\\python.exe` that
`run.bat` preferred over `.venv`), transcription kept working but **nothing was ever
copied or pasted** — with no error message anywhere. That was the reported
"剪贴板无法识别 / 粘贴不出来" bug.

This module removes the dependency on pywin32 entirely by calling the Win32 API
through `ctypes`, which is part of the standard library and therefore always
available on Windows. Two extra wins:

* `SendInput` replaces the deprecated `keybd_event`. It is the API modern Windows
  expects and is markedly more reliable for synthetic input.
* Physical modifier keys are released before the paste and restored afterwards.
  This matters because Vox's default hotkey is `Ctrl+Win`: in hands-free (toggle)
  mode the user presses that combo to *stop* recording, so at paste time both
  Ctrl **and Win** are physically held. Injecting Ctrl+V while Win is down makes
  Windows open the Clipboard History panel (`Win+V`) instead of pasting. Releasing
  the stray modifiers first fixes that class of failure.

Every public function is best-effort and never raises: callers get a bool/None and
can consult `last_error()` for a human-readable reason.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"

# ─── Win32 constants ──────────────────────────────────────────────
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

VK_CONTROL = 0x11
VK_V = 0x56

# Modifiers that must not be physically down while we inject Ctrl+V, because
# Windows folds them into the chord (Win+V = clipboard history,
# Ctrl+Shift+V = terminal paste, Alt+... = menu mnemonic).
_STRAY_MODIFIERS = (
    0x5B,  # VK_LWIN
    0x5C,  # VK_RWIN
    0x10,  # VK_SHIFT
    0xA0,  # VK_LSHIFT
    0xA1,  # VK_RSHIFT
    0x12,  # VK_MENU (Alt)
    0xA4,  # VK_LMENU
    0xA5,  # VK_RMENU
)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


# ─── ctypes structures ────────────────────────────────────────────
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    # `pad` keeps the union the size of the largest member (MOUSEINPUT) so the
    # struct layout matches what SendInput expects on both x86 and x64.
    _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


# ─── DLL bindings ─────────────────────────────────────────────────
if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.GlobalAlloc.restype = ctypes.c_void_p
    _kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalUnlock.restype = ctypes.c_int
    _kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalFree.restype = ctypes.c_void_p
    _kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    _kernel32.CloseHandle.restype = ctypes.c_int
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.GetExitCodeProcess.restype = ctypes.c_int
    _kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_ulong)]

    _user32.OpenClipboard.restype = ctypes.c_int
    _user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    _user32.CloseClipboard.restype = ctypes.c_int
    _user32.EmptyClipboard.restype = ctypes.c_int
    _user32.SetClipboardData.restype = ctypes.c_void_p
    _user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    _user32.GetClipboardData.restype = ctypes.c_void_p
    _user32.GetClipboardData.argtypes = [ctypes.c_uint]
    _user32.IsClipboardFormatAvailable.restype = ctypes.c_int
    _user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    _user32.EnumClipboardFormats.restype = ctypes.c_uint
    _user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.SendInput.restype = ctypes.c_uint
    _user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

    # Self-built low-level keyboard hook (see KeyboardWatcher). Declaring the
    # prototypes here keeps the 64-bit HHOOK/HANDLE values intact.
    _kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    _kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    _kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    _user32.SetWindowsHookExW.restype = ctypes.c_void_p
    _user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    _user32.UnhookWindowsHookEx.restype = ctypes.c_int
    _user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    _user32.CallNextHookEx.restype = ctypes.c_long
    _user32.CallNextHookEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.GetMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    _user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    _user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    _user32.PostThreadMessageW.restype = ctypes.c_int
    _user32.PostThreadMessageW.argtypes = [
        ctypes.c_ulong, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    _user32.RegisterHotKey.restype = ctypes.c_int
    _user32.RegisterHotKey.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    _user32.UnregisterHotKey.restype = ctypes.c_int
    _user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
else:  # pragma: no cover - non-Windows is unsupported but must import cleanly
    _user32 = _kernel32 = None


# ─── Diagnostics ──────────────────────────────────────────────────
_err_lock = threading.Lock()
_last_error = ""


def _set_error(msg: str) -> None:
    global _last_error
    with _err_lock:
        _last_error = msg


def last_error() -> str:
    """Human-readable reason for the most recent failure ("" when healthy)."""
    with _err_lock:
        return _last_error


def available() -> bool:
    """True when the Win32 entry points were bound successfully."""
    return bool(IS_WINDOWS and _user32 is not None and _kernel32 is not None)


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25
_INTEGRITY_NAMES = {
    0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium",
    0x2100: "Medium-Plus", 0x3000: "High", 0x4000: "System",
}


def integrity_level(pid=None):
    """Return (rid, name) for a process's integrity level, or (None, reason).

    WHY THIS EXISTS: a process below Medium cannot have its WH_KEYBOARD_LL hook
    receive keystrokes destined for Medium processes (Chrome, editors, most apps).
    The symptom is "the global hotkey only works while the Vox window is focused".
    Reading it for ANOTHER pid is unreliable under UIPI, so the daemon reports its
    OWN level (pid=None) and the value goes into vox_health.json.

    Uses GetSidSubAuthority instead of manual byte offsets — a hand-rolled offset
    read returned Low for a Medium process, which sent one investigation the wrong
    way for an hour.
    """
    if not IS_WINDOWS or _kernel32 is None:
        return None, "non-windows"
    try:
        import os as _os
        target = int(pid) if pid is not None else _os.getpid()
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.OpenProcessToken.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        adv.GetTokenInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong)]
        adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        adv.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_ulong)
        adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, target)
        if not h:
            return None, f"OpenProcess err={ctypes.get_last_error()}"
        try:
            tok = ctypes.c_void_p()
            if not adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
                return None, f"OpenProcessToken err={ctypes.get_last_error()}"
            try:
                need = ctypes.c_ulong(0)
                adv.GetTokenInformation(tok, _TOKEN_INTEGRITY_LEVEL, None, 0,
                                        ctypes.byref(need))
                buf = ctypes.create_string_buffer(need.value)
                if not adv.GetTokenInformation(tok, _TOKEN_INTEGRITY_LEVEL, buf,
                                               need.value, ctypes.byref(need)):
                    return None, f"GetTokenInformation err={ctypes.get_last_error()}"
                sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                cnt = adv.GetSidSubAuthorityCount(sid).contents.value
                rid = adv.GetSidSubAuthority(sid, cnt - 1).contents.value
                return rid, _INTEGRITY_NAMES.get(rid & 0xF000, hex(rid))
            finally:
                _kernel32.CloseHandle(ctypes.c_void_p(tok.value))
        finally:
            _kernel32.CloseHandle(ctypes.c_void_p(h))
    except Exception as exc:
        return None, f"error:{exc}"


def process_alive(pid) -> bool:
    """True only while `pid` refers to a RUNNING process.

    `OpenProcess` alone is NOT enough, and getting this wrong is subtle: a terminated
    process object survives on Windows for as long as any handle to it stays open, and
    Python's `subprocess` keeps exactly such a handle. So after `Popen.kill()` +
    `wait()`, `OpenProcess(pid)` **still succeeds** and the process looks alive
    forever. `GetExitCodeProcess` is what actually distinguishes "the object exists"
    from "it is still running" (STILL_ACTIVE).

    This matters for two real features: the overlay's orphan watchdog (an overlay that
    wrongly believes its daemon is alive becomes the stuck pill on the desktop) and the
    daemon's single-instance / stale-process checks.
    """
    if not IS_WINDOWS or not pid:
        return False
    handle = None
    try:
        handle = _kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    except Exception:
        return False
    finally:
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass


def status() -> dict:
    """Small health snapshot — used by check_env diagnostics and the GUI."""
    ok = available()
    return {
        "backend": "ctypes" if ok else "unavailable",
        "available": ok,
        "is_windows": IS_WINDOWS,
        "last_error": last_error(),
    }


# ─── Clipboard ────────────────────────────────────────────────────
def get_clipboard_text(retries: int = 8, delay_s: float = 0.015):
    """Return the current clipboard text, or None when unavailable/empty.

    The Windows clipboard is a single global lock: another process (a clipboard
    manager, an office app) may hold it open for a few milliseconds, so a short
    bounded retry is used instead of a single failing attempt.
    """
    if not available():
        _set_error("Win32 不可用（非 Windows 平台或 user32 绑定失败）")
        return None

    for _ in range(max(1, retries)):
        if not _user32.OpenClipboard(None):
            time.sleep(delay_s)
            continue
        try:
            if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = _kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                _kernel32.GlobalUnlock(handle)
        except Exception as exc:  # pragma: no cover - defensive
            _set_error(f"读取剪贴板失败: {exc}")
            return None
        finally:
            _user32.CloseClipboard()

    _set_error("读取剪贴板失败: 剪贴板被其他程序占用 (OpenClipboard 超时)")
    return None


def clipboard_formats(retries: int = 4, delay_s: float = 0.015) -> set:
    """Ids of every format currently on the clipboard (empty set on failure)."""
    if not available():
        return set()
    for _ in range(max(1, retries)):
        if not _user32.OpenClipboard(None):
            time.sleep(delay_s)
            continue
        found = set()
        try:
            fmt = 0
            while True:
                fmt = _user32.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                found.add(int(fmt))
            return found
        except Exception:
            return found
        finally:
            _user32.CloseClipboard()
    return set()


def set_clipboard_text(text: str, retries: int = 8, delay_s: float = 0.015) -> bool:
    """Put `text` on the clipboard as CF_UNICODETEXT. Returns True on success."""
    if not available():
        _set_error("Win32 不可用（非 Windows 平台或 user32 绑定失败）")
        return False
    if text is None:
        return False

    # UTF-16LE, NUL-terminated — the exact layout CF_UNICODETEXT requires.
    payload = str(text).encode("utf-16-le") + b"\x00\x00"

    for _ in range(max(1, retries)):
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            _set_error("写入剪贴板失败: GlobalAlloc 内存分配失败")
            return False

        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            _kernel32.GlobalFree(handle)
            _set_error("写入剪贴板失败: GlobalLock 失败")
            return False
        ctypes.memmove(ptr, payload, len(payload))
        _kernel32.GlobalUnlock(handle)

        if not _user32.OpenClipboard(None):
            # Nobody owns it — free our block and retry shortly.
            _kernel32.GlobalFree(handle)
            time.sleep(delay_s)
            continue

        try:
            _user32.EmptyClipboard()
            if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
                # Ownership stays with us only on failure.
                _kernel32.GlobalFree(handle)
                _set_error("写入剪贴板失败: SetClipboardData 被拒绝")
                return False
            # Success: the clipboard now owns the memory. Do NOT free it.
            _set_error("")
            return True
        except Exception as exc:  # pragma: no cover - defensive
            _kernel32.GlobalFree(handle)
            _set_error(f"写入剪贴板失败: {exc}")
            return False
        finally:
            _user32.CloseClipboard()

    _set_error("写入剪贴板失败: 剪贴板被其他程序占用 (OpenClipboard 超时)")
    return False


# ─── Keyboard injection ───────────────────────────────────────────
def _key_event(vk: int, keyup: bool = False) -> _INPUT:
    ev = _INPUT()
    ev.type = INPUT_KEYBOARD
    ev.u.ki.wVk = vk
    ev.u.ki.wScan = 0
    ev.u.ki.dwFlags = KEYEVENTF_KEYUP if keyup else 0
    ev.u.ki.time = 0
    ev.u.ki.dwExtraInfo = 0
    return ev


def _send(events) -> bool:
    if not events:
        return False
    arr = (_INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        _set_error(f"SendInput 只送出了 {sent}/{len(events)} 个事件")
        return False
    return True


def _physically_down(vk: int) -> bool:
    try:
        return bool(_user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def send_paste(settle_s: float = 0.012) -> bool:
    """Inject Ctrl+V, temporarily releasing stray modifiers (Win/Shift/Alt).

    Modifiers that were physically held are re-pressed after the chord so the
    user's keyboard state is not left inconsistent.
    """
    if not available():
        _set_error("Win32 不可用，无法模拟 Ctrl+V")
        return False

    # Start each attempt with a clean error slot so a failure always carries a
    # reason from THIS attempt, never a stale one from an earlier operation.
    _set_error("")
    try:
        # 1. Release any held modifier that would corrupt the Ctrl+V chord.
        #    BUG-1 fix: every _send() result is checked. Previously all three
        #    results were discarded and the function returned True unconditionally,
        #    so a blocked/failed SendInput was reported as a successful paste
        #    (the project's "#1 red line" violation). _set_error() is only cleared
        #    on the all-success path; _send() records the concrete reason on failure.
        def _failed():
            # _send() normally records a concrete reason; guarantee a visible
            # one even if a low-level path returns False without setting it.
            if not last_error():
                _set_error("SendInput 注入失败（底层未给出详细原因）")
            return False

        held = [vk for vk in _STRAY_MODIFIERS if _physically_down(vk)]
        if held:
            if not _send([_key_event(vk, keyup=True) for vk in held]):
                return _failed()
            time.sleep(settle_s)

        # 2. Ctrl+V.
        if not _send(
            [
                _key_event(VK_CONTROL, keyup=False),
                _key_event(VK_V, keyup=False),
                _key_event(VK_V, keyup=True),
                _key_event(VK_CONTROL, keyup=True),
            ]
        ):
            return _failed()

        # 3. Restore the physical modifier state we disturbed. The paste itself
        #    already succeeded at this point, so a restore failure must NOT be
        #    reported as a failed paste (that would tell the user to paste again
        #    and cause a double insert). Surface it as a warning instead.
        restored = True
        if held:
            time.sleep(settle_s)
            restored = _send([_key_event(vk, keyup=False) for vk in held])

        if not restored:
            _set_error("Ctrl+V 已注入，但恢复物理修饰键失败（用户键盘状态可能不一致）")
        else:
            _set_error("")
        return True
    except Exception as exc:  # pragma: no cover - defensive
        _set_error(f"模拟 Ctrl+V 失败: {exc}")
        return False


def foreground_window_title() -> str:
    """Title of the current foreground window, or "" on any failure."""
    if not available():
        return ""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        return (buf.value or "").strip()
    except Exception:
        return ""


# ─── Hotkey / modifier state (zero-allocation polling) ────────────
# `GetAsyncKeyState` is a single syscall with no allocation, which makes it the
# right primitive for a 25 Hz poll loop. The `keyboard` library's is_pressed()
# path re-parses the hotkey string and defensively copies the entire pressed-key
# set on every call — ~120 string parses and set copies per second from the
# daemon's main loop alone.
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
VK_SPACE = 0x20
VK_TAB = 0x09
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# token -> VK codes, where a tuple means "any of these satisfies the token".
_HOTKEY_VKS = {
    "shift": (VK_SHIFT,),
    "control": (VK_CONTROL,),
    "ctrl": (VK_CONTROL,),
    "alt": (VK_MENU,),
    "win": (VK_LWIN, VK_RWIN),
    "super": (VK_LWIN, VK_RWIN),
    "windows": (VK_LWIN, VK_RWIN),
    "cmd": (VK_LWIN, VK_RWIN),
    "esc": (VK_ESCAPE,),
    "escape": (VK_ESCAPE,),
    "enter": (VK_RETURN,),
    "return": (VK_RETURN,),
    "space": (VK_SPACE,),
    "tab": (VK_TAB,),
}


def is_key_down(vk: int) -> bool:
    """True while the given virtual key is physically held."""
    return _physically_down(vk)


def is_any_down(vks) -> bool:
    """True when at least one of the given virtual keys is held."""
    return any(_physically_down(vk) for vk in vks)


def keys_all_down(groups) -> bool:
    """True when every group has at least one key held.

    `groups` is a sequence of VK tuples produced by `resolve_hotkey_vks`.
    """
    for group in groups:
        if not any(_physically_down(vk) for vk in group):
            return False
    return True


def resolve_hotkey_vks(tokens):
    """Map hotkey tokens (['control', 'win']) to VK groups for polling.

    Unknown tokens resolve to an empty tuple which never reports as down, so a
    caller can detect an unsupported combo by checking for any empty group.
    """
    groups = []
    for token in tokens or ():
        name = str(token).strip().lower().replace("left ", "").replace("right ", "")
        if name in _HOTKEY_VKS:
            groups.append(_HOTKEY_VKS[name])
        elif len(name) == 1 and name.isalpha():
            groups.append((ord(name.upper()),))
        elif len(name) == 1 and name.isdigit():
            groups.append((0x30 + int(name),))
        elif name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
            groups.append((0x70 + int(name[1:]) - 1,))
        else:
            groups.append(())
    return groups


# ─── Windows RegisterHotKey (system-level global hotkey) ──────────
# THE ROBUST PATH (BUG-22). Everything that relies on receiving the raw keystroke
# — GetAsyncKeyState, WH_KEYBOARD_LL, the `keyboard` package, Raw Input — fails
# when the process cannot see another process's input (UIPI / integrity level,
# which is exactly what a sandboxed agent-launched daemon hits). RegisterHotKey
# does not observe input at all: the SYSTEM owns the chord and posts WM_HOTKEY
# straight to our thread. Consequences that matter here:
#   * works regardless of which window is in front;
#   * works for a Low-integrity process;
#   * needs a real key (a bare modifier chord like Ctrl+Alt cannot be registered);
#   * the chord is exclusive while registered;
#   * it is press-to-fire, not hold — use it for toggle semantics.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

# NOTE (2026-11-18 incident): there is deliberately NO "modifier as the main
# key" support here anymore. RegisterHotKey accepts such chords and then never
# fires them; the daemon also suppresses its keyboard hook while a system
# hotkey is active, so a bogus registration kills the hotkey entirely.

# token -> MOD_* flags
_HOTKEY_MOD_BITS = {
    "control": MOD_CONTROL, "ctrl": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "windows": MOD_WIN, "cmd": MOD_WIN,
}


def resolve_register_hotkey(tokens):
    """['ctrl','alt','space'] -> (modifiers, vk, error).

    Returns error is not None when the chord cannot be registered with
    RegisterHotKey (notably: no non-modifier key present).
    """
    modifiers = 0
    vk = None
    for raw in tokens or ():
        name = str(raw).strip().lower().replace("left ", "").replace("right ", "")
        if not name:
            continue
        if name in _HOTKEY_MOD_BITS:
            modifiers |= _HOTKEY_MOD_BITS[name]
            continue
        if name == "space":
            vk = VK_SPACE
        elif name in ("enter", "return"):
            vk = VK_RETURN
        elif name in ("esc", "escape"):
            vk = VK_ESCAPE
        elif name == "tab":
            vk = VK_TAB
        elif len(name) == 1 and name.isalpha():
            vk = ord(name.upper())
        elif len(name) == 1 and name.isdigit():
            vk = 0x30 + int(name)
        elif name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
            vk = 0x70 + int(name[1:]) - 1
        else:
            return modifiers, None, f"不认识的按键: {raw!r}"
    if vk is None:
        # Pure modifier chord (Ctrl+Alt / Win+Ctrl / Ctrl+Shift): REFUSE it.
        #
        # 2026-11-18 incident: RegisterHotKey ACCEPTS "mods + modifier-as-vk"
        # (the call returns success), but the system never dispatches WM_HOTKEY
        # for it — a modifier arriving is treated as modifier-state change, not
        # as the chord's main key. Verified the hard way: Ctrl+Alt "registered
        # successfully" at 19:30, the user pressed it four times at 19:33 and
        # not one WM_HOTKEY arrived. Worse, while RegisterHotKey is active the
        # daemon suppresses the hook path, so the hotkey was COMPLETELY dead.
        # Never register a chord the system cannot fire.
        return modifiers, None, (
            "纯修饰键组合不能注册系统热键（RegisterHotKey 会接受注册但永不触发）；"
            "如需系统热键请加一个真实按键，或留空 register_combo 走键盘钩子")
    return modifiers, vk, None


class HotkeyRegistry:
    """One global hotkey via RegisterHotKey, owned by a dedicated message thread.

    The thread registers the chord and pumps messages; every WM_HOTKEY bumps
    `fire_count`, which the daemon polls in its main loop. No callback runs on the
    message thread, so a slow consumer can never block the system.

        reg = HotkeyRegistry()
        reg.start(MOD_CONTROL | MOD_ALT, 0x20)     # Ctrl+Alt+Space
        reg.fire_count                               # increments per press
        reg.stop()
    """

    _HOTKEY_ID = 0xB01

    def __init__(self, name="vox-hotkey-registry"):
        self.name = name
        self._thread = None
        self._thread_id = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._modifiers = 0
        self._vk = 0
        self.combo = ""
        self.error = ""
        self.fire_count = 0
        self._msg = None

    # ── lifecycle ────────────────────────────────────────────────
    def start(self, modifiers, vk, combo="", timeout=2.0):
        if not available():
            self.error = "Win32 不可用"
            return False
        self.stop()
        self._modifiers = int(modifiers)
        self._vk = int(vk)
        self.combo = combo
        self.error = ""
        self.fire_count = 0
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return not self.error

    def stop(self):
        self._stop.set()
        tid = self._thread_id
        if tid:
            try:
                _user32.PostThreadMessageW(tid, WM_QUIT, None, None)
            except Exception:
                pass
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        self._thread = None
        self._thread_id = 0

    def is_alive(self):
        t = self._thread
        return bool(t is not None and t.is_alive())

    # ── internals ────────────────────────────────────────────────
    def _run(self):
        try:
            self._thread_id = int(_kernel32.GetCurrentThreadId())
            ok = _user32.RegisterHotKey(
                None, self._HOTKEY_ID, self._modifiers, self._vk)
            if not ok:
                err = ctypes.get_last_error()
                self.error = (f"RegisterHotKey 失败 (err={err})："
                              f"该组合可能已被其它程序占用")
                self._ready.set()
                return
            self._msg = ctypes.create_string_buffer(64)
            msg_ptr = ctypes.cast(self._msg, ctypes.c_void_p)
            self._ready.set()
            while not self._stop.is_set():
                r = _user32.GetMessageW(msg_ptr, None, 0, 0)
                if r == 0 or r == -1:
                    break
                # MSG.message is the 3rd field: offset 8 on x64 after HWND.
                try:
                    message = ctypes.cast(
                        ctypes.byref(self._msg, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4),
                        ctypes.POINTER(ctypes.c_uint))[0]
                except Exception:
                    message = 0
                if message == WM_HOTKEY:
                    self.fire_count += 1
        except Exception as exc:
            self.error = f"热键线程异常: {exc}"
            self._ready.set()
        finally:
            try:
                _user32.UnregisterHotKey(None, self._HOTKEY_ID)
            except Exception:
                pass


# ─── Self-healing global keyboard watcher (WH_KEYBOARD_LL) ────────
# WHY THIS EXISTS (2026-11-18, the "hotkey only works when the Vox window is in
# front" bug): the daemon used the `keyboard` package's global hook. That package
# dispatches user callbacks from ONE `processing_thread` whose loop is:
#
#     while True:
#         event = self.queue.get()
#         if self.pre_process_event(event): self.invoke_handlers(event)
#
# and whose error path is `except Exception: traceback.print_exc()`
# (keyboard/_generic.py:19-26). Under pythonw.exe `sys.stderr` is None, so the
# moment ANY handler raises, print_exc() itself raises AttributeError, that
# escapes the un-guarded loop, and the processing thread dies permanently. The
# OS hook keeps running and keeps filling the queue, but nobody consumes it, so
# the held-key set freezes and the hotkey stops responding FOREVER — with no
# error, no log line, and no recovery. That is exactly the reported symptom, and
# it is why it started failing "after some change".
#
# The watcher below removes that dependency on the critical path:
#   * the hook callback ONLY mutates a set (microseconds, far below the default
#     300 ms LowLevelHooksTimeout — exceeding that makes Windows silently
#     remove the hook);
#   * no file I/O, no logging, no allocation of Python objects beyond ints, and
#     every branch is exception-guarded, so it can never kill the pump thread;
#   * the message pump lives on its own thread and is supervised by a watchdog
#     that reinstalls the hook if the pump ever dies;
#   * events are passed through (CallNextHookEx), never suppressed, so normal
#     typing and Win-key chords keep working.
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

# Low-level hooks deliver VK_LCONTROL/VK_RCONTROL (0xA2/0xA3) for the real
# physical keys rather than VK_CONTROL (0x11), so collapse them onto the
# canonical code the hotkey groups are built from.
_CANONICAL_VK = {
    0xA0: 0x10, 0xA1: 0x10,          # L/R shift -> shift
    0xA2: 0x11, 0xA3: 0x11,          # L/R ctrl  -> control
    0xA4: 0x12, 0xA5: 0x12,          # L/R alt   -> menu
}


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_ulong),
        ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KeyboardWatcher:
    """Global keyboard state from a self-owned WH_KEYBOARD_LL hook.

    Focus-independent (the hook is system-wide), self-healing, and safe under
    pythonw (the callback can never raise into the pump).

        watcher = KeyboardWatcher()
        watcher.start()
        watcher.keys_all_down([(0x11,), (0x5B, 0x5C)])   # Ctrl + either Win
        watcher.stop()
    """

    _POOL_SIZE = 4000   # bound memory if the pump ever wedges

    def __init__(self, name="vox-keyboard-watch"):
        self.name = name
        self._down = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._hook = None
        self._thread_id = 0
        self._alive = False
        self._installs = 0
        self._proc_ref = None       # keep the CFUNCTYPE alive or the hook crashes
        self._msg = None
        # WH_KEYBOARD_LL callbacks are delivered on the installing thread while it
        # pumps messages, so the hook is only USABLE once the pump is inside
        # GetMessageW. Without this gate start() returned "ready" while the thread
        # had not reached the loop yet, and any key pressed in that window was lost.
        self._pumping = threading.Event()
        # Diagnostic: how many raw key events the hook has seen. If this stops
        # growing while the user is typing, the OS is not delivering input to
        # this process (UIPI / elevated-foreground / different desktop).
        self.event_count = 0
        # Diagnostic ring of the most recent key codes we saw (vk + up/down).
        self.recent = []

    # ── lifecycle ────────────────────────────────────────────────
    def start(self, timeout=2.0):
        if not available():
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._pumping.clear()
        self._thread = threading.Thread(
            target=self._pump_loop, name=self.name, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Ready == hook installed AND the pump thread is actually looping, so
            # no keystroke can be lost between start() and the first GetMessageW.
            if self._alive and self._pumping.is_set():
                return True
            time.sleep(0.005)
        return bool(self._alive)

    def stop(self):
        self._stop.set()
        tid = self._thread_id
        if tid:
            try:
                _user32.PostThreadMessageW(tid, WM_QUIT, None, None)
            except Exception:
                pass
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        self._thread = None
        self._alive = False
        with self._lock:
            self._down.clear()

    def is_alive(self):
        t = self._thread
        return bool(self._alive and t is not None and t.is_alive())

    def reinstall(self):
        """Force a fresh hook (called by the watchdog when the pump looks dead)."""
        self.stop()
        return self.start()

    def watchdog(self, max_age_s=2.0):
        """One-shot health check used by the daemon's main loop. Returns True if
        the hook was reinstalled. Cheap: a thread-liveness check."""
        if self.is_alive():
            return False
        return bool(self.start() or self.is_alive())

    # ── state access ─────────────────────────────────────────────
    def snapshot(self):
        with self._lock:
            return set(self._down)

    def is_down(self, vk):
        with self._lock:
            return int(vk) in self._down

    def keys_all_down(self, groups):
        """True when every group has at least one held key (same contract as the
        module-level keys_all_down, but served from the hook's own state)."""
        try:
            with self._lock:
                down = set(self._down)
            for group in groups or ():
                if not any(int(vk) in down for vk in group):
                    return False
            return True
        except Exception:
            return False

    def install_count(self):
        return self._installs

    # ── internals ────────────────────────────────────────────────
    def _pump_loop(self):
        """Own thread: install the hook, then pump messages until stopped.

        Any failure is contained here (never propagates to the daemon) and leaves
        _alive False so the watchdog retries.
        """
        try:
            if not self._install():
                return
            self._msg = ctypes.create_string_buffer(64)   # MSG is <64 bytes
            msg_ptr = ctypes.cast(self._msg, ctypes.c_void_p)
            self._pumping.set()      # hook is live and the loop is about to block
            while not self._stop.is_set():
                # Blocks until this thread receives a message.
                r = _user32.GetMessageW(msg_ptr, None, 0, 0)
                if r == 0 or r == -1:      # WM_QUIT or error
                    break
                _user32.TranslateMessage(msg_ptr)
                _user32.DispatchMessageW(msg_ptr)
        except Exception:
            pass
        finally:
            self._uninstall()
            self._pumping.clear()
            self._alive = False

    def _install(self):
        self._thread_id = int(_kernel32.GetCurrentThreadId())
        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p)
        # Keeping a reference is mandatory: if the ctypes thunk is collected the
        # hook callback points at freed memory and the process crashes.
        self._proc_ref = HOOKPROC(self._hook_proc)
        hmod = _kernel32.GetModuleHandleW(None)
        self._hook = _user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, ctypes.cast(self._proc_ref, ctypes.c_void_p), hmod, 0)
        self._alive = bool(self._hook)
        if self._alive:
            self._installs += 1
        return self._alive

    def _uninstall(self):
        h = self._hook
        self._hook = None
        if h:
            try:
                _user32.UnhookWindowsHookEx(ctypes.c_void_p(h))
            except Exception:
                pass

    def _hook_proc(self, nCode, wParam, lParam):
        """Runs INSIDE the OS hook. Must never raise, never do I/O, never block.

        Deliberately free of logging: writing to vox.log here would risk the
        300 ms LowLevelHooksTimeout, and Windows responds to that by silently
        removing the hook — the exact failure this class exists to prevent.
        """
        if nCode == HC_ACTION and lParam:
            try:
                vk = int(ctypes.cast(
                    lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents.vkCode)
                vk = _CANONICAL_VK.get(vk, vk)
                ev = wParam
                if ev == WM_KEYDOWN or ev == WM_SYSKEYDOWN:
                    with self._lock:
                        if len(self._down) < self._POOL_SIZE:
                            self._down.add(vk)
                elif ev == WM_KEYUP or ev == WM_SYSKEYUP:
                    with self._lock:
                        self._down.discard(vk)
                # Plain int increment; no lock needed for a diagnostic counter.
                self.event_count += 1
                # Bounded diagnostic ring so a single press tells us WHICH keys
                # the hook actually received (e.g. "did the Win key arrive?").
                try:
                    if len(self.recent) < 64:
                        self.recent.append(("%d" % vk) + ("v" if wParam in
                                                          (WM_KEYUP, WM_SYSKEYUP) else "^"))
                    else:
                        del self.recent[0]
                        self.recent.append(("%d" % vk) + ("v" if wParam in
                                                          (WM_KEYUP, WM_SYSKEYUP) else "^"))
                except Exception:
                    pass
            except Exception:
                pass
        try:
            return _user32.CallNextHookEx(None, nCode, wParam, lParam)
        except Exception:
            return 0

