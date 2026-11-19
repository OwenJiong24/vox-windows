@echo off
setlocal
cd /d "%~dp0"
title Vox

if exist ".venv\Scripts\python.exe" goto :RUN_VOX

echo [Vox] Initializing Python virtual environment...
python -m venv .venv
if errorlevel 1 goto :VENV_ERROR

echo [Vox] Installing required dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :DEP_WARNING

:RUN_VOX
rem The project venv is authoritative: it is where requirements.txt was installed.
rem This used to prefer a global system interpreter, which lacked the project's
rem dependencies — so clipboard writes and Ctrl+V failed silently while
rem transcription kept working. Never prefer a global interpreter over the venv.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" check_env.py %*
if errorlevel 1 goto :APP_ERROR
goto :EOF

:VENV_ERROR
echo.
echo ============================================================
echo [Error] Failed to initialize virtual environment.
echo Please ensure Python 3.10+ is installed and on your PATH.
echo ============================================================
echo.
pause
exit /b 1

:DEP_WARNING
echo.
echo [Warning] Dependency installation warning. Continuing...
goto :RUN_VOX

:APP_ERROR
echo.
echo ============================================================
echo [Vox] Process exited with error. Window kept open for inspection.
echo ============================================================
echo.
pause
exit /b 1
