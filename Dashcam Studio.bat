@echo off
REM Double-click to launch Dashcam Studio (native window) on Windows.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\app.py
) else (
    python scripts\app.py
)
pause
