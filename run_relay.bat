@echo off
REM workmom - start relay in background (called by workmom_guard when relay is unreachable)
setlocal
set "BASE=%~dp0"

REM Prefer the silent VBS launcher so no console window flashes on screen.
if exist "%BASE%run_relay.vbs" (
    start "" /b wscript.exe //nologo "%BASE%run_relay.vbs"
    exit /b 0
)

REM Fallback: plain python launcher, port comes from config.json (do not hardcode).
set "PY=%BASE%.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=pythonw.exe"
start "" /b "%PY%" "%BASE%server.py"
exit /b 0
