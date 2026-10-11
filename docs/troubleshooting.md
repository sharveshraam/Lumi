# Troubleshooting

Start here:

```bat
python main.py --check
```

It prints your hardware, whether Ollama answers, how many tools loaded and where
the data folder is. Most problems are visible in that output.

Logs are in `<data folder>\logs\lumi.log` (rotating, 1 MB × 3). Raise the
detail with Settings → Privacy → Log level = `DEBUG`, or
`set LUMI_LOG_LEVEL=DEBUG` for one run.

---

## The window

### Nothing happens when I run `python main.py`

Run it from a terminal so you can see the error, instead of double-clicking.
Common causes:

| Message | Fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'PySide6'` | the virtual environment is not active: `.venv\Scripts\activate` |
| `Qt platform plugin "windows" could not be loaded` | a broken PySide6 install: `pip install --force-reinstall PySide6` |
| `ImportError: DLL load failed` | install the [VC++ 2015-2022 redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) |

### The window opens but is blank

A Qt style/plugin problem. Try `set QT_QUICK_BACKEND=software`, then delete
`settings.json` (LUMI will recreate it) — a bad `appearance.font_scale` or
colour value is the usual cause.

### I closed the window but LUMI is still running

That is the tray. Settings → Startup → *Close to tray*. Right-click the tray
icon → Quit, or turn off *Minimise to tray*.

### The tray icon is missing

Windows 11 hides new tray icons: click the `^` arrow next to the clock and drag
the LUMI icon onto the taskbar.

---

## Ollama and models

### "I cannot reach Ollama"

1. Is Ollama running? Check the tray/menu-bar icon, or run `ollama list` — if
   that fails, start Ollama.
2. Is it on the expected address? `curl http://127.0.0.1:11434/api/version`
   should print a version.
3. Behind a proxy? `NO_PROXY=127.0.0.1,localhost` fixes most of it.

LUMI keeps working without Ollama — tasks, notes, reminders, memory and file
search never need a model.

### "No models are installed"

```bat
ollama pull llama3.2:3b
```

Then press ↻ in the chat header.

### The model is very slow

* First answer after starting: the model is loading. Subsequent answers are
  several times faster.
* Check Task Manager → Performance → GPU. If the GPU is idle, the model is
  running on the CPU: it is too big for your VRAM. Move down a size
  (`llama3.2:3b` instead of `qwen2.5:7b`).
* Shorten the context (`generation.num_ctx`) and the answer limit
  (`generation.num_predict`), or pick the `fast` quality profile.
* Close other memory-hungry apps; a 7B model needs ~5 GB free.

### "model requires more system memory"

The model does not fit. Use a smaller one, or unload it when idle
(Settings → Model → *Unload after idle*).

### Answers stop in the middle

The context window filled up. Start a new chat (`Ctrl+N`) or raise `num_ctx` —
at the cost of memory.

### Answers are wrong or invented

That is a small local model's honest failure mode. LUMI's prompt asks it to say
"not sure", but it still guesses. Ask narrower questions, give it the source
text ("summarise this: …"), or use a bigger model. Anything important should be
checked — LUMI never claims local output is authoritative.

---

## Tools, permissions and the stop button

### "I am not allowed to do that"

By design. Settings → Permissions: turn on the switch for that group
(*Open applications*, *Keyboard and mouse*, *Screenshots*, *Write files*,
*Browser automation*, *Bulk file operations*). Nothing is enabled on a fresh
install.

### LUMI asks for confirmation constantly

Settings → Permissions → *Always ask me before* — untick what you are happy
with. Answering **Always allow** in the dialog grants it for that one session;
the tick boxes in the dialog are what change it permanently.

### Stop does nothing

The Stop button cancels between steps: a tool that is already inside a blocking
Windows API call finishes that call first. Long operations (a model download, a
big file scan) check the stop flag themselves and stop within a second.

If even the button is unresponsive, use the hotkey (`Ctrl+Alt+Shift+S` by
default). On Windows LUMI registers it with the operating system, so it works
from inside any application; if Windows refused the combination (another program
already uses it) the tooltip on the Stop button says so, and the shortcut still
works while LUMI is focused. If the whole UI is frozen, quit from the tray icon.

### "That application is not available"

The app name was not found. Ask LUMI to *list open windows*, or use the exact
name from the Start menu. Some Store apps cannot be started by name.

### Typing goes to the wrong window

LUMI checks which window has focus before typing. If the target app refuses to
take focus (some do), click it yourself first, then ask again.

---

## Files

### "LUMI only writes inside its workspace folders"

Settings → Permissions → Permitted folders: add the folder you want LUMI to
work in. Reads work anywhere except protected locations; writes need a
permitted folder and *Write files* switched on.

### A delete or overwrite was refused

Overwriting an existing file and deleting anything always ask for confirmation,
and deleting is logged. LUMI will not delete outside a permitted folder, and it
never deletes as part of a duplicate scan.

### Duplicate scan finds nothing

By default it ignores files smaller than 4 KB (raise/lower `min_size_kb`) and it
skips protected locations. It never deletes: it lists identical files and waits
for you.

---

## Voice

### The microphone is not picked up

`pip install sounddevice`, then Settings → Voice → *Input device* and pick your
microphone. Windows 11 may also need Settings → Privacy → Microphone → *Let
desktop apps access your microphone*.

### Transcription is slow or inaccurate

`base` is the default. `tiny` is faster and rougher; `small` is more accurate
and slower. The first run downloads the model. Speak clearly, and keep clips
short.

### No speech output

Windows has a built-in voice, so try `tts_engine: sapi`. For better quality,
install Piper and set `piper_exe` and `piper_voice`. Ubuntu/Debian users need
`espeak-ng` in addition to `pyttsx3`.

---

## Reminders and notifications

### A reminder did not appear

* Windows Focus Assist / Do Not Disturb can silence notifications.
* Settings → Notifications → *Reminders enabled*.
* Closing LUMI stops reminders — they fire when LUMI runs again, and missed
  ones are delivered at startup with a *missed* marker
  (`notifications.catch_up_missed_reminders`).
* Check the Reminders list; a reminder that already fired stays in the history
  with its timestamp.

### Notifications never appear on Windows

Settings → System → Notifications: make sure notifications are on for Python
(or for LUMI.exe). In a packaged build the app name comes from the shortcut.

---

## Data

### Where is my data, and how do I move it?

`%LOCALAPPDATA%\LUMI` (change with `--home`). Copy the whole folder; nothing
lives anywhere else.

### How do I back up or export?

* Memory page → **Export** writes `lumi-memory-export.json`.
* Conversations can be exported from the same file format.
* Or copy `lumi.db` while LUMI is closed — that is the entire database.

### I want to start over

Close LUMI, delete `lumi.db` (keeps your settings) or the whole data folder
(removes everything).

### The database is locked

Only one LUMI can use a data folder at a time. Check for a second copy in the
tray, and make sure you did not point `--home` at a synced folder
(OneDrive/Dropbox can lock `lumi.db` mid-write).

---

## Still stuck

Collect this before asking for help:

```bat
python main.py --check > lumi-check.txt
type "%LOCALAPPDATA%\LUMI\logs\lumi.log" > lumi-log.txt
```

The log is redacted (no API keys, passwords or tokens), but skim it anyway
before sharing — it can contain prompts and file names.
