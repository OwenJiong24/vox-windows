# Third-Party Notices

Vox itself is MIT-licensed (see [LICENSE](LICENSE)). It depends on the following
third-party packages, each under its own license. This file is informational and
does not replace the license texts shipped with those packages.

## Dependency licenses (verified from installed package metadata, 2026-11-18)

| Package | Version | License | Notes |
|---|---|---|---|
| sounddevice | 0.5.6 | MIT | PortAudio audio capture |
| numpy | 2.5.2 | BSD-3-Clause (+ bundled components) | Array/DSP |
| groq | 1.7.0 | Apache-2.0 | Primary cloud ASR SDK |
| requests | 2.34.2 | Apache-2.0 | MiMo cloud ASR HTTP client |
| soundfile | 0.14.0 | BSD-3-Clause | In-process FLAC encoding (libsndfile) |
| keyboard | 0.13.5 | MIT | Global low-level hotkey hook |
| pystray | 0.19.5 | **LGPLv3** | System-tray icon (native Windows backend) |
| Pillow | 12.3.0 | MIT-CMU | Tray / icon rendering |
| zhconv | 1.4.3 | **GPLv2+** | Traditional→Simplified Chinese normalization |
| pywin32 | 312 | PSF | Optional; only used by one legacy test |
| funasr | 1.4.16 | MIT | Optional local engine (SenseVoice); pulls in torch |
| torch | 2.11.0 | BSD-3-Clause | Optional; local-engine inference |

## License-compatibility notes (important)

- **GPLv2+ (`zhconv`)**: Vox calls `zhconv.convert()` as a library. Distributing a
  binary bundle that includes `zhconv` may trigger GPL obligations. The project ships
  as source and imports `zhconv` lazily/opt-in; if you redistribute a packaged build,
  review GPLv2+ compliance or provide an alternative normalizer.
- **LGPLv3 (`pystray`)**: dynamically imported for the tray icon only. If you ship a
  packaged binary, preserve LGPLv3 notices and allow relinking, or drop the tray feature.
- These two dependencies are the reason Vox is **not** advertised as "pure MIT":
  the project code is MIT, while the aggregated work has the obligations above.

## Sample audio

No third-party audio samples are redistributed. `test_suite.py` generates synthetic
tone data in memory; the optional benchmark accepts a user-supplied WAV path.

## Fonts / icons

`vox.ico` is generated programmatically by `vox_core.ensure_vox_ico()` (original work,
no third-party artwork). The GUI uses the system font stack (Microsoft YaHei UI / Segoe UI).
