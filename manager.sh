#!/usr/bin/env bash
# Vox Manager launcher for WSL environments
# Seamlessly opens Vox Manager on Windows host from WSL terminal

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

if grep -qi microsoft /proc/version 2>/dev/null; then
    echo "[Vox] Detected WSL environment. Opening Manager on Windows host..."
    cmd.exe /c manager.bat
else
    python3 vox_gui.py
fi
