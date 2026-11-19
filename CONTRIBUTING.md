# Contributing to Vox

Thanks for your interest. This document is the source of truth for the development
workflow. Please read it fully before opening a pull request.

## Ground rules

1. **Reliability of "text reaches the cursor" is the top priority.** Any failure that
   affects output must be visible (log + user-facing hint + on-disk fallback). Never
   introduce a silent `return False`.
2. **Single implementations only.** Process-liveness, single-instance locking, JSON
   writes, clipboard access, logging, and hotkey state each have exactly one canonical
   helper in `vox_core.py` / `vox_win32.py`. Reuse them; do not grow a second copy.
3. **Code changes require a daemon restart** to take effect; only `config.json` is
   hot-reloaded. Do not claim code hot-reload in docs or UI.
4. **Never commit `config.json`** (it may contain DPAPI-encrypted keys) or runtime
   state files (`*.log`, `overlay_state.json`, `latest.json`, `vox_*.json`).

## Development setup

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config.example.json config.json      :: then fill in your own API keys
run.bat
```

Python 3.10+ on Windows 10/11 is required. The project uses Windows-specific APIs
(ctypes to user32/kernel32, `winreg`, `winsound`) and does not run on macOS/Linux.

## Before you open a PR

```bat
.venv\Scripts\python.exe -m py_compile vox_win32.py vox_core.py vox_daemon.py vox_gui.py vox_overlay.py check_env.py
.venv\Scripts\python.exe test_regression.py
```

`test_regression.py` is the offline gate (clipboard, paste-path failure visibility,
hot-plug attribution, engine chain, overlay, VAD, hot-backup). It is safe by default.
Two opt-in flags exist: `--inject` (sends a real Ctrl+V to the focused window) and
`--daemon` (starts the daemon and grabs the global hotkey). Only use them in an
unattended environment.

`test_suite.py` exercises the cloud path and **consumes API quota**; run it only when
you intend to test against a live key.

## Change protocol

1. Reproduce the bug first; record the log line and the relevant state file.
2. Write a failing regression assertion when possible.
3. Make the smallest fix that addresses the root cause. One topic per PR.
4. Run the gates above.
5. Update docs (`README.md`, `THIRD_PARTY_NOTICES.md` if dependencies change).

## Commit message format

```
<area>: <what changed>

Symptom: ...
Root cause: ...
Fix: ...
Verified: <commands + results>
```

## Scope notes

This is a personal-origin project now opened for community maintenance. Features that
are deliberately out of scope: full-duplex voice conversations, TTS, wake words,
browser/WebAssembly ASR, and cloud agent tooling. See `docs/` (if present) or the
repository wiki for the current product boundary.
