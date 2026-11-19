# Vox — global voice dictation for Windows

> Press a hotkey, speak, release. Your words appear at the cursor.

Vox is a Windows desktop dictation tool in the spirit of macOS/Wispr Flow: a single
global hotkey drives recording, cloud transcription, and clipboard-safe paste into
whatever application you are using. It is designed to be **fast** (~0.3–0.5 s to text),
**quiet** (a no-activate floating pill that never steals focus), and **honest**
(every failure that affects output is visible, never silently swallowed).

## Features

- **Adaptive hotkey**: hold to dictate and release to transcribe; tap for hands-free
  recording (tap again or press Enter to finish). `Esc` cancels mid-recording.
- **Three cloud ASR engines with fallback**: Groq Whisper (large-v3 / turbo) and
  MiMo ASR, with a typed fallback chain you can reorder in the GUI.
- **Optional local engine**: SenseVoice (FunASR) on CPU or CUDA, loaded on demand.
- **Smart output**: writes CF_UNICODETEXT and injects Ctrl+V via `SendInput`, with
  stray-modifier handling, terminal detection, and clipboard-history preservation.
- **Correction pipeline**: hallucination filtering, Traditional→Simplified conversion,
  a hot-reloadable replacement dictionary (literal + regex), and Chinese punctuation
  restoration.
- **Zero-focus overlay**: a layered, no-activate floating pill showing recording level,
  timer, transcribing state, result, and latency — draggable and resizable.
- **System tray**: status icon plus menu (start/stop, calibrate mic, reload config, quit).
- **Hot reload**: edit `config.json` in the manager GUI; changes apply within ~1 s
  without restarting the daemon.
- **Reliability instrumentation**: per-attempt outcomes, error classification
  (auth/quota/timeout/...), and pre-AGC mic level attribution are recorded so silent
  degradation is visible.

## Who is Vox for?

Vox is deliberately narrow. It is **not** a meeting notetaker, AI assistant, or note app.
It is a system-level dictation *input method* for people who speak short phrases and want
the text at the cursor fast.

**Good fit**
- Windows 10/11 users who dictate **short Chinese phrases mixed with English technical
  terms** into code editors, chats, emails, or terminals.
- Developers and technical writers who want **sub-second** text-at-cursor (~0.3–0.5 s
  with Groq) and a **domain vocabulary** (custom prompt + replacement dictionary) so
  terms like API names come out right.
- People who need dictation that **never steals focus** from the app they are typing in.
- Anyone burned by dictation tools that "transcribe fine but paste nothing" — reliability
  and visible failure are a first-class design goal here.
- Users who already have a Groq / MiMo API key and prefer cloud speed; the optional local
  SenseVoice engine stays cold until you put it in the fallback chain.

**Not a good fit**
- You need **meeting transcription, speaker diarization, AI chat, notes, or team spaces** —
  those are features of larger platforms such as
  [OpenWhispr](https://github.com/OpenWhispr/openwhispr), not Vox.
- You need **macOS or Linux**. Vox is Windows-only (it depends on Win32 APIs).
- You need **fully offline / no API key** dictation as the default path — Vox is
  cloud-first; local SenseVoice is opt-in and is not a full offline product.
- You want **translation while dictating** or **audio-file import / batch transcription**.
- You want **zero setup** with no key to configure.

**Not quite a fit? Hand it to a local AI agent.** Vox is MIT-licensed, small, has a clear
module map, strict reliability rules, and an offline regression gate — which makes it an
unusually good base for a coding agent (Claude Code, Codex, Cursor, Copilot) to customize
and debug for your exact situation. [ADAPTING.md](ADAPTING.md) is a ready-made hand-off
brief: it lists the files to read, the rules not to break, the test gate to run, and recipes
for changing the hotkey, ASR engine, output destination, language, or platform. The project
is actively maintained and iterated; broadly useful changes are welcome as pull requests.

## How Vox differs from other dictation tools

| Dimension | Vox | Typical open-source alternatives |
|---|---|---|
| Scope | One job: hotkey → text at cursor | Often a platform: notes, meetings, AI agent, transcription library |
| Focus | Never activates; no-activate layered pill | Many use normal windows that take focus |
| Latency target | ~0.3–0.5 s via cloud ASR | Local/offline-first tools trade speed for privacy |
| Trigger | Adaptive hold-vs-tap with `Esc` cancel | Usually single hold-to-talk |
| Chinese | Hallucination filter + punctuation + domain lexicon | Often generic multilingual |
| Failure model | Every output failure is visible + logged + disk fallback | Commonly silent `False` |
| Footprint | Pure Python, lazy imports, ~30 MB idle | Electron apps are much heavier |
| Platform | Windows only | Cross-platform (mac/win/linux) |
| Secret storage | DPAPI at rest, env-var override | Varies (often plaintext config) |
| License | MIT source (+ GPLv2+/LGPLv3 dependency notices) | Usually MIT |

If you want an all-in-one dictation + meetings + notes platform, use OpenWhispr or a
commercial tool. If you want one small, fast, reliable thing that puts spoken Chinese at
the cursor without interrupting you, Vox is that thing.

## Quick start

Requirements: Windows 10/11, Python 3.10+.

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config.example.json config.json
```

Edit `config.json` and put your own API key(s) in `engines.groq.api_key` and/or
`engines.mimo.api_key`. Keys are encrypted at rest with Windows DPAPI on first save
(the on-disk value becomes `dpapi:...`). You may also use environment variables
`VOX_GROQ_KEY` / `VOX_MIMO_KEY`, which take precedence.

```bat
run.bat          :: preflight + start the daemon (tray + floating pill)
manager.bat      :: open the settings & diagnostics console
```

Default hotkey in `config.example.json` is `Control+Win` (configurable; the shipped
example uses `control+win`, a real-key chord). Highlights of the config:

| Key | Meaning |
|---|---|
| `hotkey.combo` | Hotkey tokens for the keyboard-hook path, e.g. `control+alt` |
| `hotkey.register_combo` | Optional system-level (`RegisterHotKey`) chord; **must contain a real key** (e.g. `control+win+z`). Pure-modifier chords are rejected because Windows registers them but never dispatches them. Empty = hook path. |
| `hotkey.mode` | `adaptive` (hold/tap), `hold_only`, or `toggle_only` |
| `engine_priority` | Ordered fallback slots: `groq-l3`, `groq-turbo`, `mimo`, `sensevoice-cpu`, `sensevoice-cuda` |
| `output_method` | `smart_paste` (Ctrl+V) or `clipboard_only` |
| `whisper_prompt` | Domain vocabulary bias for ASR |
| `streaming.interim_engine` | Optional local slot for live preview (empty = off) |

## Architecture (short version)

Four cooperating pieces, communicating through small JSON state files rather than sockets:

```
run.bat → check_env.py ──spawns──> vox_daemon.py (state machine, hotkeys, tray, workers)
                                        │ writes overlay_state.json (+ health/perf/history)
                                        ▼
                                  vox_overlay.py (Tk, no-activate layered window, file poller)

tray / launcher ──spawns──> vox_gui.py (settings, calibration, history, perf panel)
```

Core logic lives in `vox_core.py` (audio pipeline, engines, post-processing, smart
output, config) and Win32 primitives in `vox_win32.py` (ctypes-only: clipboard,
`SendInput`, key state). `vox_vad.py` provides pure-numpy speech segmentation for the
opt-in streaming preview. `vox_theme.py` holds shared visual tokens and DPI helpers.

The daemon is the single source of truth for interaction state; the overlay only
renders what it reads from `overlay_state.json`.

## Tests

```bat
.venv\Scripts\python.exe test_regression.py
```

Offline and safe by default (134 assertions, including a guard that refuses to
register pure-modifier system hotkeys). `--inject` tests real Ctrl+V (types a space
into the focused window) and `--daemon` runs a daemon smoke test (grabs the global
hotkey); both are opt-in. `test_suite.py` is an end-to-end suite that calls live ASR
APIs and consumes quota.

## Troubleshooting

**Hotkey only works while a Vox window is focused / "pasted" but nothing appears at
the cursor.** Both symptoms share one cause: the daemon is running at **Low
integrity**. A low-integrity process cannot receive keyboard-hook events destined
for normal (medium-integrity) applications, and its injected Ctrl+V is silently
dropped by User Interface Privilege Isolation — while `SendInput` still reports
success. Check `integrity` in the daemon's health file: it must be `Medium`. If it
says `Low`, the Python interpreter in `.venv` carries a Low mandatory label;
relabel it (and the project directory) to Medium, then restart the daemon:

```bat
icacls .venv\Scripts\python.exe  /setintegritylevel Medium
icacls .venv\Scripts\pythonw.exe /setintegritylevel Medium
icacls .venv\Scripts             /setintegritylevel "(OI)(CI)Medium"
icacls .venv                     /setintegritylevel "(OI)(CI)Medium"
icacls .                         /setintegritylevel "(OI)(CI)Medium"
```

Integrity is determined by the *lower* of the launcher's token and the executable's
own label — so this can affect a daemon started by hand or by an agent just as much
as one started by `run.bat`.

**System hotkey registered "successfully" but never fires.** `RegisterHotKey`
accepts pure-modifier chords such as `Ctrl+Alt` and then never dispatches them.
Use a chord with a real key (`control+win+z`) in `hotkey.register_combo`, or leave it
empty to use the default keyboard-hook path. Vox now rejects impossible chords with
an explicit warning instead of silently falling back.

**No text / garbled output.** Check the `mic_fallback` field in the health file and
the daemon log: when the configured `audio.preferred_device` is unavailable, Vox
warns loudly and falls back to another microphone. Very short or silent takes are
deliberately discarded to prevent Whisper hallucination artifacts being pasted.

**Config changes not applied.** `config.json` hot-reloads in about a second; code
changes require a daemon restart. Make sure only one daemon instance is running.

**`run.bat` fails with "command not found" style errors.** The batch file must stay
pure ASCII with CRLF line endings — a single non-ASCII character can make `cmd`
misparse the whole script.

## Security & privacy

- API keys are never stored in plaintext on disk; `config.json` is git-ignored.
- Audio is sent only to the ASR provider you configure. No telemetry, no analytics.
- The floating pill never activates or captures focus.
- See [SECURITY.md](SECURITY.md) for the disclosure process and the security model.

## License

MIT for the Vox source code — see [LICENSE](LICENSE). This project depends on
third-party packages with their own licenses, including **GPLv2+ (`zhconv`)** and
**LGPLv3 (`pystray`)**; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before
redistributing a packaged build.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Background

The story behind this open-source release — the secret scan, the zero-history repo,
and the dependency-license discovery — is written up on OrangeKnows:
["I open-sourced my dictation tool. The hardest part was not the code."](https://orangeknows.com/essays/2026-11-18-vox-open-source/)
(中文: [「我把语音输入工具开源了。最难的不是代码。」](https://orangeknows.com/zh/essays/2026-11-18-vox-open-source/)).
