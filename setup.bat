@echo off
rem workmom setup wrapper
rem Keep this file ASCII-only + CRLF (see .gitattributes).
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ============================================================
echo  workmom  -  one click setup
echo ============================================================
echo.
echo  Steps: locate WorkBuddy, write config.json, install
echo  python deps, verify the debug port, then self test.
echo  Idempotent - safe to run again anytime.
echo.

set "PYEXE="
where python >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
)
if not defined PYEXE (
    where py >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
    )
)

if not defined PYEXE (
    echo [X] Python not found.
    echo.
    echo     Install Python 3.9+ first:
    echo       https://www.python.org/downloads/
    echo     During installation tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

echo Using Python: %PYEXE%
echo.

"%PYEXE%" "%~dp0setup.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [OK] setup finished.
) else (
    echo [X] setup finished with problems, exit code = %RC%
    echo     Scroll up and read the [FAIL] lines.
)
echo.
pause
exit /b %RC%
