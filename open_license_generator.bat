@echo off
REM ────────────────────────────────────────────────────────────────
REM NodeCraft License Generator — double-click launcher
REM
REM What this does:
REM   1. cd into license_webapp/
REM   2. Run Flask server via venv's Python
REM   3. Auto-open http://localhost:5555 in default browser
REM
REM Stop the server: close this terminal window or press Ctrl+C
REM ────────────────────────────────────────────────────────────────

setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo ERROR: venv not found at venv\Scripts\python.exe
    echo Please run from the TRFS project root.
    pause
    exit /b 1
)

REM Open browser 3 seconds AFTER server starts (gives Flask time to bind port)
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start "" http://localhost:5555"

REM Run Flask (blocking — keeps console window open)
cd license_webapp
"..\venv\Scripts\python.exe" app.py

REM If Flask exits with error, keep window open so user can read the message
if errorlevel 1 (
    echo.
    echo Flask exited with an error. Press any key to close.
    pause >nul
)
