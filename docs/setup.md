# Setup

This is the long version. If you just want it working, run
`scripts\setup_windows.bat` and skip to *First run*.

## 1. Install Python

LUMI needs **Python 3.10 or newer** (3.11 is what it is developed against).

1. Download the Windows installer from <https://www.python.org/downloads/windows/>.
2. On the first screen, tick **Add python.exe to PATH**.
3. Choose "Install Now".

Verify in a new Command Prompt:

```bat
python --version
```

## 2. Get LUMI and its packages

```bat
git clone https://github.com/sharveshraam/Lumi.git
cd Lumi
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If you do not use git, download the ZIP from GitHub and extract it, then run the
same `python -m venv` and `pip install` lines inside the extracted folder.

### What those packages are for

| Package | Needed for |
| --- | --- |
| `PySide6` | the window itself |
| `httpx` | talking to Ollama |
| `jsonschema` | checking tool arguments before anything runs |
| `python-dateutil` | "next Tuesday", "in 2 hours" |
| `psutil` | the hardware report in Settings → About |
| `Pillow` | screenshots |
| `pyautogui`, `pywinauto`, `pyperclip` | Windows app control (installed automatically on Windows) |

Everything else in LUMI is the Python standard library.

## 3. Install Ollama (for chat)

LUMI runs entirely without Ollama — tasks, notes, reminders, memory, notes and
file search all work — but "chat" needs a model somewhere, and Ollama is the
one LUMI talks to.

1. Download from <https://ollama.com/download> and install it.
2. Ollama starts a small server on `http://127.0.0.1:11434`. LUMI only ever
   talks to that address (it refuses non-loopback hosts unless you change it in
   Settings → Model).
3. Pull a model:

```bat
ollama pull llama3.2:3b
```

`llama3.2:3b` is about 2 GB and runs comfortably on 8 GB of RAM with no GPU.
See [models.md](models.md) for what fits your machine.

## 4. First run

```bat
python main.py
```

You should see the window, and in the top-right: the model menu, the
**Local only** badge and the red **STOP** button.

Start with something that needs no model at all:

```
add task: submit the lab record tomorrow 6pm
what's due today?
```

Then try the model:

```
explain Thevenin's theorem like I'm new to it
```

The first answer takes a few seconds while the model loads into memory; after
that it is much faster.

### Check without a window

```bat
python main.py --check
```

Prints your hardware, whether Ollama is running, how many tools are registered
and where your data lives. Exit code 0 means Ollama answered.

```bat
python main.py --briefing        print today's briefing and exit
python main.py --version         print the version
python main.py --home D:\LumiData   use a different data folder
```

## 5. Where things are kept

`%LOCALAPPDATA%\LUMI` (Windows) or `~/.local/share/lumi` (Linux). Change it with
`--home`, or by setting the `LUMI_HOME` environment variable.

Delete that folder and LUMI is gone: no registry keys, no services, nothing
else is installed.

## Optional extras

### Voice

```bat
pip install faster-whisper sounddevice numpy
```

Speech output is best with [Piper](https://github.com/rhasspy/piper): download
`piper.exe` and a voice `.onnx` file, then point Settings → Voice at them.
`pyttsx3` also works and uses the built-in Windows voice.

The first transcription downloads a Whisper model (~150 MB for `base`), which
is stored in the normal Hugging Face cache (`%USERPROFILE%\.cache`).

### Running at login

Settings → Startup → *Start LUMI when I sign in to Windows* writes a small
`LUMI.cmd` launcher into your personal Startup folder
(`shell:startup`), which is also how you remove it by hand. Nothing is written
to the registry and no administrator rights are needed.

## Uninstall

1. Close LUMI (tray menu → Quit).
2. Delete the LUMI folder and your `.venv` inside it.
3. Delete `%LOCALAPPDATA%\LUMI` if you also want your tasks, notes and memory
   removed — **that is the only place your data lives**.
4. Remove the Startup entry if you enabled it (untick it in Settings → Startup,
   or delete `LUMI.cmd` from `shell:startup`).
