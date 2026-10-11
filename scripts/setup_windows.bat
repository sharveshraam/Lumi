@echo off
REM ---------------------------------------------------------------------------
REM  LUMI - first-time setup for Windows.
REM
REM  Creates a virtual environment, installs the requirements, checks for Ollama
REM  and (optionally) pulls a small model.  Safe to run again: it only installs
REM  what is missing.
REM ---------------------------------------------------------------------------
setlocal EnableDelayedExpansion
cd /d "%~dp0\.."

echo.
echo  ============================================
echo   LUMI setup
echo  ============================================
echo.

REM --- 1. Python ------------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python is not on your PATH.
    echo     Install Python 3.11 from https://www.python.org/downloads/windows/
    echo     and tick "Add python.exe to PATH" during the install.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [1/4] Found Python !PYVER!

REM --- 2. Virtual environment ----------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [X] Could not create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [2/4] .venv already exists - reusing it.
)

REM --- 3. Requirements ------------------------------------------------------
echo [3/4] Installing requirements (this can take a few minutes) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] pip could not install the requirements. Scroll up for the error.
    pause
    exit /b 1
)

REM --- 4. Ollama ------------------------------------------------------------
where ollama >nul 2>nul
if errorlevel 1 (
    echo [4/4] Ollama was not found on your PATH.
    echo       LUMI works without it - tasks, notes, reminders, memory and files.
    echo       For chat, install it from https://ollama.com/download and run this
    echo       script again (or just start Ollama once you have installed it).
) else (
    echo [4/4] Ollama found. Pulling a small model (llama3.2:3b, about 2 GB) ...
    echo       Press Ctrl+C now if you would rather choose another model - see
    echo       docs\models.md for the options.
    ollama pull llama3.2:3b
)

echo.
echo  --------------------------------------------
echo   Checking the installation
echo  --------------------------------------------
".venv\Scripts\python.exe" main.py --check

echo.
echo  Setup finished.
echo.
echo   Start LUMI with:   run_lumi.bat
echo   Or directly with:  .venv\Scripts\python.exe main.py
echo.
pause
