# Changelog

All notable changes to this project are documented here. This file follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
