@echo off
REM Start LUMI from a checkout.  Creates nothing - run setup_windows.bat first.
cd /d "%~dp0\.."
if not exist ".venv\Scripts\python.exe" (
    echo LUMI is not set up yet. Run scripts\setup_windows.bat first.
    pause
    exit /b 1
)
".venv\Scripts\pythonw.exe" main.py %*
if errorlevel 1 ".venv\Scripts\python.exe" main.py %*
