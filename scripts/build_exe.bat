@echo off
REM Build LUMI.exe with PyInstaller (see docs\packaging.md).
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo Run scripts\setup_windows.bat first.
    pause
    exit /b 1
)

echo Installing PyInstaller ...
".venv\Scripts\python.exe" -m pip install --quiet pyinstaller

echo Building ...
".venv\Scripts\python.exe" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --name LUMI ^
    --icon lumi\assets\lumi.ico ^
    --add-data "lumi\assets;lumi\assets" ^
    --hidden-import PySide6.QtSvg ^
    --hidden-import lumi.ui.pages.chat ^
    --hidden-import lumi.ui.pages.tasks ^
    --hidden-import lumi.ui.pages.notes ^
    --hidden-import lumi.ui.pages.memory ^
    --hidden-import lumi.ui.pages.settings ^
    --exclude-module tkinter ^
    --exclude-module matplotlib ^
    packaging\lumi_entry.py

if errorlevel 1 (
    echo Build failed - see the output above.
    pause
    exit /b 1
)

echo.
echo Done. The program is in dist\LUMI\LUMI.exe
echo Copy the whole dist\LUMI folder to move it to another machine.
pause
