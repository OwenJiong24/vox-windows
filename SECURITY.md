# Security Policy

## Supported versions

The current `main` branch is the only supported version.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Use GitHub's private vulnerability reporting on this repository
("Security" tab → "Report a vulnerability"), or contact the maintainers
through the contact method listed in the repository's About section.

Include: affected version/commit, reproduction steps, impact, and a suggested fix
if you have one. You should receive an acknowledgement within 7 days.

## Security model (what is and is not in scope)

Vox is a desktop dictation tool that sends short audio clips to third-party ASR
APIs and injects the resulting text via synthetic keyboard input.

- **API keys**: stored at rest via Windows DPAPI (`dpapi:` prefix in `config.json`),
  which binds the ciphertext to the current Windows user and machine. `config.json`
  is git-ignored and must never be committed. Environment variables
  (`VOX_GROQ_KEY`, `VOX_MIMO_KEY`) take precedence over stored keys.
- **Clipboard**: Vox writes text to the clipboard and injects Ctrl+V. It restores the
  previous clipboard text only when it held plain text (never files/images).
- **Audio**: audio is uploaded only to the ASR endpoint configured in `config.json`.
  No audio is sent anywhere else; there is no telemetry.
- **Local engine**: SenseVoice (FunASR) is opt-in and loads models on demand.

Out of scope: vulnerabilities in third-party ASR providers, the Windows OS, or the
keyboard hooks that Vox necessarily uses for global hotkeys.

## Known design constraints (not vulnerabilities)

- Global hotkey injection requires a low-level keyboard hook; some endpoint-security
  products flag such hooks. This is expected for a system-wide dictation tool.
- The floating overlay creates a layered, no-activate window by design; it cannot
  receive keyboard focus, which is intentional.
