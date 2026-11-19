# Adapting Vox with a local AI coding agent

> This project is maintained and iterated with AI coding agents. If Vox is not a perfect
> fit for you out of the box, you do not have to give up on it: hand this repository to a
> local coding agent (Claude Code, Codex, Cursor, Copilot, etc.) and ask it to make the
> specific change you need. The codebase is deliberately structured to be modified that
> way. This document is the hand-off brief for that agent.

## 0. Read this first (agent onboarding)

Before changing anything, have the agent read, in order:

1. `README.md` — what Vox is and how it is configured.
2. `CONTRIBUTING.md` — the ground rules and the change protocol.
3. `SECURITY.md` — what must never be committed or exposed.
4. This file — the module map and the customization recipes below.

The repository is small (about 9,000 lines across 9 Python modules), single-purpose, and
has an offline regression gate. That combination makes an AI agent's job tractable: make
one focused change, run the gate, show the diff.

## 1. Non-negotiable rules (tell your agent these)

- **Output reliability first.** Every failure that affects getting text to the cursor must
  be visible (log + user-facing hint + on-disk fallback). Never introduce a silent
  `return False`.
- **One implementation per capability.** Process liveness, single-instance lock, JSON
  writes, clipboard access, logging, and hotkey state each have exactly one canonical
  helper in `vox_core.py` / `vox_win32.py`. Reuse them; do not write a second copy.
- **Code changes need a daemon restart** to take effect; only `config.json` is hot-reloaded.
- **Never commit `config.json`** (it may hold encrypted API keys) or runtime state
  (`*.log`, `overlay_state.json`, `latest.json`, `vox_*.json`). `.gitignore` already covers
  these.

## 2. Module map (where to change what)

| File | Owns | Typical edit |
|---|---|---|
| `vox_win32.py` | Win32 ctypes primitives: clipboard, `SendInput`, key state | Change Ctrl+V injection or hotkey polling |
| `vox_core.py` | Audio pipeline, ASR engines, post-processing, smart output, config | Add/replace an engine, change correction rules |
| `vox_daemon.py` | FSM, hotkeys, tray, worker threads | Change trigger behaviour, add a workflow |
| `vox_overlay.py` | No-activate Tk overlay | Change visual feedback / status pill |
| `vox_gui.py` | Settings & diagnostics console | Add a config control |
| `vox_theme.py` | DPI / palette / font helpers | Restyle without touching layout |
| `vox_vad.py` | Pure-numpy speech segmentation | Change streaming preview segmentation |
| `vox_tuning.py` | BLAS/OpenMP thread caps | Tune local-engine performance |
| `crypto_utils.py` | DPAPI at-rest key encryption | Change secret storage |

## 3. Verification gate (the agent must run this)

```bat
.venv\Scripts\python.exe -m py_compile vox_win32.py vox_core.py vox_daemon.py vox_gui.py vox_overlay.py check_env.py
.venv\Scripts\python.exe test_regression.py
```

`test_regression.py` is offline and safe by default (115 checks: clipboard, paste-path
failure visibility, engine chain, overlay, VAD, hot-backup, and more). For hotkey or
daemon changes, start with a failing assertion for the new behaviour, then make it pass.
The `--inject` and `--daemon` flags are opt-in and affect the live desktop; only use them
unattended. `test_suite.py` calls live ASR APIs and consumes quota.

## 4. Customization recipes

These are the common "Vox is almost right, but…" cases and the exact places to change them.

### Change the hotkey or trigger style
- GUI presets: `vox_gui.py` → `HOTKEY_PRESETS`.
- Parsing / VK groups: `vox_daemon.py` → `_parse_hotkey`; `vox_win32.py` → `_HOTKEY_VKS`.
- Hold-vs-tap behaviour: `vox_daemon.py` → `watch_loop()` FSM and `_toggle_armed`.

### Add or replace an ASR engine
- Implement a `transcribe()` returning `(text, latency, cost)` in `vox_core.py`.
- Register it in `EngineChain.transcribe()` (add a slot branch) and in
  `ConfigManager.DEFAULT_CONFIG["engines"]`.
- Map it in the GUI fallback slots (`SPEC_TO_SLOT` / `SLOT_TO_SPEC` in `vox_gui.py`).
- Add its failure signatures to `classify_engine_error()` so degradation stays visible.

### Emphasize offline / local-only
- Put a `sensevoice-cpu` slot first in `engine_priority`, or add a new local engine.
- Optional live preview: set `streaming.interim_engine` to a local slot.
- Expect a large dependency (torch + funasr); `vox_tuning.py` caps threads, and heavy
  imports are lazy so the idle path stays light.

### Change the output destination (e.g. serial, HID, webhook, another app)
- The output seam is `SmartOutput.output()` / `vox_win32.send_paste()` in `vox_core.py`
  and `vox_win32.py`. Replace the paste step but preserve the failure-visibility contract
  (log + hint + disk fallback).

### Change the language / vocabulary
- `whisper_prompt` in config; `PostProcessor` in `vox_core.py` for punctuation and
  hallucination patterns; `replacements.json` for domain corrections.
- For another language, review the Chinese-specific filters in `PostProcessor` and the
  `language` handling in `EngineChain` — those are intentionally Chinese-focused.

### Other platforms (macOS / Linux)
- This is the largest change: Win32 primitives in `vox_win32.py` and the tray/overlay
  styles are Windows-specific. Treat it as a port, not a patch, and budget accordingly.

### Remove the GUI / tray / overlay entirely
- These are separate processes. Set `overlay.enabled: false` and stop spawning the GUI;
  the daemon can run headless. Keep the visible-failure contract (log + disk fallback).

## 5. Project status and staying in sync

Vox is **actively maintained and iterated**. It is a small, focused project whose scope is
deliberately narrow, so changes tend to be incremental and reviewable.

- See `CHANGELOG.md` for the change history and current release.
- Watch / star the repository to follow updates: https://github.com/OwenJiong24/vox-windows
- Bugs and ideas are tracked in GitHub Issues; focused pull requests are welcome.
- If your customization is broadly useful, consider opening a PR so it can be merged back
  instead of maintaining a private fork forever.

When you fork for a local adaptation, rebase on upstream periodically: the reliability
fixes and regression guards are the parts most worth keeping current.

## 6. One-line brief you can paste to your agent

> "This is Vox, a Windows voice-dictation tool. Read README.md, CONTRIBUTING.md,
> SECURITY.md and ADAPTING.md first. Make <your change>. Keep the rule that any output
> failure must be visible, reuse the single implementations in vox_core/vox_win32, then
> run py_compile and test_regression.py and show me the diff."
