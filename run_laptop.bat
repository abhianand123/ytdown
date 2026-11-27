@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_laptop.py %*
) else (
    python run_laptop.py %*
)
if errorlevel 1 (
    echo.
    echo [!] OmniDownloader exited with an error.
    pause
)
