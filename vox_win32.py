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
