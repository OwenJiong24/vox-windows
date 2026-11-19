@echo off
setlocal
cd /d "%~dp0"

rem Canonical autostart mechanism: a .bat in the user's Startup folder.
rem
rem This used to write HKCU\...\Run instead - a SECOND mechanism the GUI knew
rem nothing about. The two could be active at once (two daemons at every login)
rem and each uninstaller left the other one running. One mechanism, one place.

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\Vox Daemon.bat"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "DAEMON=%~dp0vox_daemon.py"

if not exist "%PYW%" (
    echo [Vox] Virtual environment missing. Run run.bat first to create it.
    pause
    exit /b 1
)

>"%TARGET%" echo @echo off
>>"%TARGET%" echo start "" "%PYW%" "%DAEMON%"

if not exist "%TARGET%" (
    echo FAILED to write "%TARGET%"
    pause
    exit /b 1
)

rem Remove the legacy registry entry so exactly ONE mechanism is active.
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v VoxDaemon /f >nul 2>&1

echo [Vox] Autostart configured:
echo   "%TARGET%"
echo Daemon will start automatically when you log into Windows.
endlocal