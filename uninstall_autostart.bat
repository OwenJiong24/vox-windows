@echo off
setlocal

rem Mirror of install_autostart.bat: removes the Startup-folder entry, and also
rem the legacy HKCU Run value in case an older install created one.

set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Vox Daemon.bat"

if exist "%TARGET%" (
    del /f /q "%TARGET%"
    echo [Vox] Removed "%TARGET%"
) else (
    echo [Vox] No Startup entry found.
)

reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v VoxDaemon /f >nul 2>&1

echo [Vox] Autostart disabled.
endlocal