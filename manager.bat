@echo off
setlocal
cd /d "%~dp0"
title Vox Manager

if not exist ".venv\Scripts\python.exe" goto :NO_VENV

rem Prefer the venv (where the dependencies actually live) over any global Python.
set "PYW=.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "Vox Manager" "%PYW%" vox_gui.py
goto :EOF

:NO_VENV
echo [Vox] Environment not initialized. Please run run.bat first.
pause
exit /b 1
