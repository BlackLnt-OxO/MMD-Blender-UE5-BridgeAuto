@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup.ps1"
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" "scripts\desktop_entry.py" %*
if errorlevel 1 (
    echo Startup failed. Run scripts\setup.ps1 and see docs\INSTALL_WINDOWS.md.
    pause
    exit /b 1
)
