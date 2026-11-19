# Changelog

All notable changes to this project are documented here. This file follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **Root cause of "hotkey only works with Vox focused" and fake paste success**:
  an inherited Low mandatory-integrity label on the project tree made the daemon
  run at Low integrity regardless of launcher. Low integrity blocks the global
  keyboard hook from seeing other applications' keystrokes and causes UIPI to
  silently drop injected Ctrl+V. Relabeling the interpreter and project directories
  to Medium restores both. Added a self-reported `integrity` health field and a
  paste-side warning when running below Medium.
- **System hotkey dead after "successful" registration**: pure-modifier chords
  (e.g. `Ctrl+Alt`) were accepted by `RegisterHotKey` but never dispatched, and
  suppressed the hook path while active. They are now rejected with an explicit
  error; the previous silent substitution of different chords was removed.
- **Wrong microphone silently used**: an unavailable `audio.preferred_device` now
  emits a warning and is reflected in the `mic_fallback` health field instead of
  quietly recording from an unintended device.
- Desensitization sync: removed a machine-specific interpreter path from `run.bat`
  that the fail-closed scan correctly flagged.

### Added
- Initial public release of Vox: global hotkey dictation with cloud ASR (Groq, MiMo),
  optional local SenseVoice engine, no-focus overlay, system tray, manager GUI,
  correction pipeline, and reliability instrumentation.
- Open-source governance: MIT license, third-party notices, security policy,
  contributing guide, code of conduct.

### Security
- API keys are encrypted at rest with Windows DPAPI (`dpapi:` prefix) and may be
  supplied via environment variables; `config.json` is git-ignored.
- All personal configuration, sample prompts, and hardware-specific strings were
  removed before the public release; the release repository has no prior git history.
