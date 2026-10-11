# Building LUMI.exe

The recommended way is a **folder build**: PyInstaller produces `dist\LUMI\`
containing `LUMI.exe` plus its libraries. One-folder starts faster and is far
easier to diagnose than one-file, and the folder can be zipped and copied to
another Windows machine.

Everything below runs **on Windows**, in the checkout, with the virtual
environment already set up (`scripts\setup_windows.bat`).

## Quick build

```bat
scripts\build_exe.bat
```

That installs PyInstaller, builds, and leaves the result in `dist\LUMI\`. Run
`dist\LUMI\LUMI.exe`. The script is documented and safe to read; the rest of
this page explains what it does and what to do when it goes wrong.

## What the build includes

| Item | Why |
| --- | --- |
| `main.py` through `packaging\lumi_entry.py` | the same entry point as running from source |
| `lumi\assets\lumi.ico` | the window and taskbar icon |
| `lumi.ui.pages.*` as hidden imports | the pages are imported dynamically by the window |
| `PySide6.QtSvg` | pull it in explicitly; Qt loads it late for icons |
| `--windowed` | no console window behind the app |

Excluded: `tkinter`, `matplotlib`. LUMI never imports them.

## Manual build

```bat
.venv\Scripts\activate
pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --name LUMI ^
  --icon lumi\assets\lumi.ico ^
  --add-data "lumi\assets;lumi\assets" ^
  --hidden-import PySide6.QtSvg ^
  --hidden-import lumi.ui.pages.chat ^
  --hidden-import lumi.ui.pages.tasks ^
  --hidden-import lumi.ui.pages.notes ^
  --hidden-import lumi.ui.pages.memory ^
  --hidden-import lumi.ui.pages.settings ^
  --exclude-module tkinter --exclude-module matplotlib ^
  packaging\lumi_entry.py
```

A one-file build (`--onefile` and drop the `--add-data` target separator
instead of `;`) works too, but takes 5–15 seconds to start while it unpacks.

## What the user needs on their machine

| Requirement | Notes |
| --- | --- |
| Windows 10/11 64-bit | |
| [Ollama](https://ollama.com/download) | for chat; everything else works without it |
| A model | `ollama pull llama3.2:3b` |
| Microsoft Visual C++ 2015–2022 redistributable | most machines already have it; the error names `VCRUNTIME140.dll` if not |

No Python install, no `.venv`, no admin rights. Data goes to the same place as
the source build (`%LOCALAPPDATA%\LUMI`), so tasks, notes and memory carry over
between a source run and the packaged one.

## First-run check of the build

```bat
dist\LUMI\LUMI.exe --version
dist\LUMI\LUMI.exe --check
dist\LUMI\LUMI.exe --briefing
```

`--check` prints the data folder, hardware, Ollama status and the tool count
(69). Then start it without arguments and run through
[acceptance.md](acceptance.md) items 1–4.

## Optional extras in a build

`faster-whisper`, `pyautogui` and `pywinauto` are not bundled by default: they
are large and optional. To include them, install them in the build environment
and add

```
--collect-all faster_whisper --collect-all pywinauto --collect-all pyautogui
```

Voice also needs the Whisper model file, which downloads on first use unless you
pre-seed the cache.

## Making a shortcut and an installer

* **Shortcut:** right-click `LUMI.exe` → *Send to* → *Desktop (create shortcut)*.
  A shortcut makes Windows group notifications under "LUMI" instead of "Python".
* **Installer:** [Inno Setup](https://jrsoftware.org/isinfo.php) with a script
  that copies `dist\LUMI\*` to `{autopf}\LUMI`, adds a Start-menu entry, and
  offers a desktop icon. LUMI itself needs no registry keys, so the installer is
  a file copy plus shortcuts.
* **Portable:** zip `dist\LUMI\` and copy it anywhere. Data still lives in
  `%LOCALAPPDATA%` unless the user passes `--home`.

## Troubleshooting the build

| Symptom | Cause and fix |
| --- | --- |
| `ModuleNotFoundError: lumi.ui.pages.chat` | a hidden import is missing — add it to the list above |
| The app starts without a window | a Qt plugin did not get bundled: build on the same 64-bit architecture as the target, and check the `PyInstaller\warn-LUMI.txt` file |
| `Failed to execute script 'lumi_entry'` | run the build with `--console` (or run `LUMI.exe` from a Command Prompt) to see the traceback |
| Antivirus flags the exe | unsigned PyInstaller binaries are often flagged. Sign the exe, or distribute the source + `scripts\setup_windows.bat` |
| `VCRUNTIME140.dll was not found` | install the VC++ redistributable on the target machine |
| The icon is blank | build from the repository root so `lumi\assets\lumi.ico` resolves |
| It is slow to start (~10 s) | one-file builds unpack to a temporary folder; use the folder build |

## Size and performance notes

A folder build is roughly 80–140 MB, most of it Qt. Startup takes about a
second on an SSD. The frozen app behaves like the source app: the model runs in
Ollama (a separate process), which is what keeps the memory footprint sane.
