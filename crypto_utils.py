#!/usr/bin/env python3
"""
Windows credential-at-rest helpers (audit B12 / complaint: Groq key plaintext in git).

The Groq API key must never sit in plaintext on disk: config.json is committed to git
and already appeared in history -> treat the key as leaked and rotate it. We protect the
key with the Windows Data Protection API (DPAPI) via ``crypt32.CryptProtectData``, which
binds the ciphertext to the logging-in Windows user account — the same guarantee Electron's
safeStorage provides on this platform. The stored form is base64 with a ``dpapi:`` marker so
it survives JSON encoding; it is transparently decrypted on load.

These functions are deliberately tiny and dependency-free. If DPAPI is unavailable (non-
Windows / stripped build) ``protect_secret`` raises and callers fall back to env vars.
"""

import base64
import ctypes

_API = None
try:
    _API = ctypes.windll.LoadLibrary("crypt32.dll")
except Exception:
    _API = None

_MARKER = "dpapi:"


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _free(blob):
    try:
        if blob and blob.pbData:
            ctypes.windll.kernel32.LocalFree(blob.pbData)
    except Exception:
        pass


def protect_secret(plain):
    """Encrypt bytes/str to a JSON-safe ``dpapi:<base64>`` string bound to the
    current Windows user. Raises OSError/RuntimeError if DPAPI is unavailable/fails."""
    if not _API:
        raise OSError("DPAPI (crypt32) unavailable; cannot encrypt secret")
    src = plain if isinstance(plain, bytes) else str(plain).encode("utf-8")
    in_buf = ctypes.create_string_buffer(src)
    in_b = _BLOB(len(src), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_b = _BLOB()
    if not _API.CryptProtectData(ctypes.byref(in_b), None, None, None, None, 0, ctypes.byref(out_b)):
        raise RuntimeError("CryptProtectData failed")
    try:
        ptr = ctypes.cast(out_b.pbData, ctypes.POINTER(ctypes.c_char * out_b.cbData))
        blob = ptr.contents[:]
    finally:
        _free(out_b)
    return _MARKER + base64.b64encode(blob).decode("ascii")


def unprotect_secret(value):
    """Inverse of protect_secret. Raises on malformed input or credential mismatch."""
    if not isinstance(value, str) or not value.startswith(_MARKER):
        raise ValueError("not a dpapi-protected secret")
    raw = base64.b64decode(value[len(_MARKER):])
    if not _API:
        raise OSError("DPAPI (crypt32) unavailable; cannot decrypt secret")
    in_buf = ctypes.create_string_buffer(raw)
    in_b = _BLOB(len(raw), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_b = _BLOB()
    if not _API.CryptUnprotectData(ctypes.byref(in_b), None, None, None, None, 0, ctypes.byref(out_b)):
        raise RuntimeError("CryptUnprotectData failed (wrong Windows user or corrupted data)")
    try:
        ptr = ctypes.cast(out_b.pbData, ctypes.POINTER(ctypes.c_char * out_b.cbData))
        return ptr.contents[:]
    finally:
        _free(out_b)


def maybe_encrypt(value):
    """Return `value` encrypted at rest when it is a live plaintext secret; otherwise
    returns it unchanged (empty or already protected). Safe to call idempotently."""
    if not value or not str(value).strip():
        return value
    s = str(value)
    if s.startswith(_MARKER):
        return s
    return protect_secret(s)


def maybe_decrypt(value):
    """Decrypt a stored secret to plaintext for in-memory use. On any failure
    (corruption / wrong user) returns "" rather than crashing — the documented env-var
    path (VOX_GROQ_KEY / GROQ_API_KEY) then remains the source of truth."""
    if not isinstance(value, str) or not value.startswith(_MARKER):
        return value if isinstance(value, str) else ""
    try:
        return unprotect_secret(value).decode("utf-8")
    except Exception:
        return ""
