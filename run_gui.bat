@echo off
REM Launch TRFS GUI using the project venv python (which has playwright installed).
REM If you run `python src\gui_app.py` with system Python, playwright will be
REM missing and ENM screenshots will be skipped with:
REM   [ENM] Playwright package is not importable in this environment
REM This launcher guarantees the venv interpreter is used.

SETLOCAL
cd /d "%~dp0"

IF NOT EXIST "venv\Scripts\python.exe" (
    echo [run_gui] ERROR: venv not found at venv\Scripts\python.exe
    echo [run_gui] Create it with: python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

echo [run_gui] Using venv python: %cd%\venv\Scripts\python.exe
"venv\Scripts\python.exe" -c "import playwright, sys; print('[run_gui] playwright', playwright.__file__)" || (
    echo [run_gui] playwright not installed in venv. Installing...
    "venv\Scripts\python.exe" -m pip install playwright
    "venv\Scripts\python.exe" -m playwright install chromium
)

"venv\Scripts\python.exe" src\gui_app.py %*
ENDLOCAL
