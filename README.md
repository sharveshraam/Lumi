# LUMI — a local-first AI desktop assistant

LUMI is a Windows desktop assistant that runs on your own laptop. It answers
questions with a local model through [Ollama](https://ollama.com), keeps your
tasks, notes, reminders and long-term memory in a SQLite file you own, and can
drive your own applications and files — always with a permission model, an audit
log and a Stop button that works.

Nothing here is a mock-up: the model really runs locally, the tools really touch
the filesystem, and the tests really run (see [Testing](#testing)).

```
┌──────────────────────────────────────────────────────────────┐
│ LUMI   local-first assistant    [model] [Local only] [■ STOP]│
├────────────┬─────────────────────────────────────────────────┤
│ Chat       │  transcript, streaming answers, tool activity   │
│ Tasks      │  tasks · reminders · focus sessions             │
│ Notes      │  quick capture                                  │
│ Memory     │  what LUMI remembers (editable, exportable)     │
│ Settings   │  model · permissions · voice · privacy          │
└────────────┴─────────────────────────────────────────────────┘
```

## Status

Phase 2 of 7 is complete: the core, the tools, the security layer and the
desktop window all work, with **335 automated tests passing** (1 skipped:
a Windows-only notification test).  See [docs/features.md](docs/features.md)
for what is implemented and what is not.

Commit: `1e4faa4` on `arena/9996b087-lumi`.

## Why it is safe to leave running

* **Local by default.** Answers come from Ollama on `127.0.0.1`. The client
  refuses non-loopback hosts unless you explicitly allow them. No cloud account
  or API key is needed for anything, and no telemetry exists.
* **Tools are a whitelist.** The model never runs Python or shell. It can only
  ask for one of the 69 registered tools, each with a strict JSON schema; the
  executor validates the arguments before anything happens.
* **Permissions are off until you turn them on.** Opening apps, mouse/keyboard
  control, screenshots, file writes, browser automation and bulk file work are
  all off in a fresh install.
* **Consequential actions ask first** — sending mail, deleting files, overwriting
  a file, installing a model, submitting a form, bulk changes.
* **A stop that works.** The Stop button and a global hotkey
  (`Ctrl+Alt+Shift+S` by default) cancel the running tool and the model stream.
* **Everything is audited.** Each tool call is written to SQLite with redacted
  arguments, and the chat page shows it live.
* **Secrets stay out.** Passwords, API keys and card-like numbers are refused by
  the memory service and stripped from logs.

## Requirements

| Component | Notes |
| --- | --- |
| Windows 10/11 (64-bit) | Developed and tested for Windows; the core also runs on Linux/macOS for development |
| Python 3.10+ | 3.11 recommended |
| [Ollama](https://ollama.com/download) | for chat. Everything else works without it |
| 8 GB RAM minimum | 16 GB recommended; a 6 GB GPU (e.g. RTX 3050) runs a 3B model comfortably |

Optional, only for the features that need them:

| Feature | Package |
| --- | --- |
| Speech input | `faster-whisper` (+ `sounddevice`, `numpy`) |
| Speech output | Piper (`piper.exe` + a voice) or `pyttsx3` |
| Driving Windows apps | `pyautogui`, `pywinauto` (shipped in the `desktop` extra) |
| Screenshots from tools | `Pillow` |
| Hardware report | `psutil` |

## Install

```bat
git clone https://github.com/sharveshraam/Lumi.git
cd Lumi
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On Windows the one-shot script does all of that for you:

```bat
scripts\setup_windows.bat
```

Then pull a model (once):

```bat
ollama pull llama3.2:3b
```

Check everything without opening a window:

```bat
python main.py --check
```

## Using it

Type in the chat box, or press `Ctrl+N` for a new conversation.

Things that work **without any model installed** — LUMI answers these from its
own task/note/reminder engine, instantly:

```
add task: finish the lab record tomorrow 6pm
remind me to call home at 8pm
what's due today?
complete task 3
notes: op-amp gain = 1 + Rf/Ri
```

Everything else goes to the local model, which may use tools. The right-hand
panel shows each tool call and its result as it happens.

### The keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+N` | new conversation |
| `Ctrl+Alt+Shift+S` | emergency stop (configurable) |
| `Ctrl+Alt+Space` | push-to-talk (configurable, needs voice extras) |
| `Enter` / `Shift+Enter` | send / new line |

## Data and privacy

Everything lives in one folder — `%LOCALAPPDATA%\LUMI` by default, or wherever
you point `--home`:

```
settings.json      your preferences (never contains credentials)
lumi.db            tasks, notes, reminders, memory, conversations, audit log
secrets.json       only if you add a cloud key and the credential manager is absent (0600)
logs\lumi.log      rotating log, secrets redacted
cache\screenshots\ screenshots LUMI took, if you asked for that
```

LUMI never sends your data anywhere. The cloud section exists so that a future
provider can be added deliberately; every provider ships **disabled** and the
answer provider defaults to `local`.

See [docs/security.md](docs/security.md) for the full model.

## Documentation

| Document | What is in it |
| --- | --- |
| [docs/setup.md](docs/setup.md) | install Ollama, pick a model, first run, troubleshooting the install |
| [docs/models.md](docs/models.md) | which local model to use on which hardware, and how to pull/switch them |
| [docs/configuration.md](docs/configuration.md) | every settings section explained |
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptoms → causes → fixes |
| [docs/security.md](docs/security.md) | permissions, confirmations, secrets, audit log |
| [docs/architecture.md](docs/architecture.md) | project layout and how a message flows through the app |
| [docs/features.md](docs/features.md) | what is implemented, and what is not yet |
| [docs/acceptance.md](docs/acceptance.md) | the manual acceptance checklist |
| [docs/packaging.md](docs/packaging.md) | building a Windows `.exe` with PyInstaller |

## Testing

```bat
.venv\Scripts\python -m pytest
```

The suite is offline and needs no model: **335 passed, 1 skipped** on the
development machine. The skip is a Windows-only notification test.

```bat
.venv\Scripts\python -m pytest -q                # everything
.venv\Scripts\python -m pytest lumi/tests/test_security.py -v
```

On Linux, Qt tests need a display or `QT_QPA_PLATFORM=offscreen`.

## Project layout

```
main.py                 entry point (python main.py)
lumi/
  app.py                builds every service in one place (no Qt imports)
  config/               paths, settings, secrets, logging
  memory/               SQLite schema, store, memory + conversation services
  tasks/ notes/         repositories and the natural-language task parser
  reminders/            reminder scheduling + focus sessions
  security/             permission policy, redaction, audit log
  tools/                the tool registry, executor, validation and 69 builtins
  automation/           desktop control, files, browser, clipboard
  ai/                   Ollama client, hardware probe, model catalogue, the
                        assistant loop, prompts, tool-call parsing
  ui/                   PySide6 window, pages, widgets, workers
  voice/                optional speech input/output
docs/ scripts/          documentation and helper scripts
```

## Limitations

Read [docs/features.md](docs/features.md) for the full list. The important ones:

* LUMI is not a cloud model. A 3B model on a laptop is useful for study help,
  drafting and driving tools — it is not a substitute for a frontier model, and
  it will sometimes be wrong. It is instructed to say "I am not sure".
* Windows automation depends on the accessibility tree; some apps expose almost
  nothing to it, and those fall back to keyboard control or refuse.
* Cloud providers are not implemented in this build (the settings exist and
  nothing is enabled).
* Voice needs extra packages and a downloaded Whisper model.
* Linux/macOS are supported for development and testing; the automation and
  notification paths are Windows-first.

## License

See [LICENSE](LICENSE).
