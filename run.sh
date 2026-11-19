#!/usr/bin/env bash
# Vox launcher for WSL environments
# Seamlessly starts Vox on Windows host from WSL terminal

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

if grep -qi microsoft /proc/version 2>/dev/null; then
    echo "[Vox] Detected WSL environment. Starting Vox on Windows host..."
    cmd.exe /c run.bat
else
    python3 vox_daemon.py
fi
