# What LUMI does — and what it does not

This is the honest inventory: everything in the "implemented" table is real code
covered by the test suite; the "not implemented" section is not a roadmap
promise, it is what is missing today.

---

## 1. Chat and local AI

| Feature | State |
| --- | --- |
| Chat with a local Ollama model, streamed token by token | ✅ |
| Model selector in the header, live status (running / loading / offline) | ✅ |
| **Local only** badge whenever a local model is answering | ✅ |
| Stop button and global hotkey that cancel the stream mid-sentence | ✅ |
| Conversation history in SQLite, new chat (`Ctrl+N`), search, rename, delete | ✅ |
| Tool-activity panel showing every tool call and its result as it happens | ✅ |
| Model recommendations from your actual RAM/VRAM | ✅ |
| Pull a model from inside the app (with confirmation and progress) | ✅ |
| Unload the idle model to give memory back | ✅ |
| Cloud providers | ❌ not implemented — every provider ships disabled and no client exists |
| Image/vision input (send a photo to `llava`) | ❌ not wired into the UI |

**Instal a model you already have?** LUMI lists what Ollama has; anything not in
its catalogue still works if you type the model name in manually.

## 2. Instant, deterministic answers (no model needed)

These are answered by LUMI's own code in microseconds, and they work even with
Ollama stopped:

| You type | What happens |
| --- | --- |
| `add task: submit the lab record tomorrow 6pm` | task created, with the date it understood *and* what it assumed |
| `what's due today?` / `show overdue` | the task list, from the database |
| `remind me to call home at 8pm` | reminder created, fires with a Windows notification |
| `notes: op-amp gain = 1 + Rf/Ri` | note created |
| `complete task 3`, `postpone 2 by 2 days`, `delete task 5` | the task changes |
| `how many tasks are left?` | counts |

Ambiguity is surfaced, not guessed at: if two tasks match "report", LUMI asks.

## 3. Tasks, reminders and focus

* Tasks: title, notes, due date, priority (`low`…`urgent`), category, tags,
  status (pending / in progress / done / cancelled), completion timestamps.
* Reminders: one-off or repeating (`daily`, `weekdays`, `weekly`, `monthly`,
  `yearly`, `every N minutes/hours/days`), snooze, cancel, and a delivery log.
* **Restart recovery:** reminders that came due while LUMI was closed are
  delivered at the next start, marked as missed, and one-offs are disabled after
  firing (this was a real bug until it was fixed — see the test suite).
* Windows notifications with an optional sound, plus a tray balloon.
* Daily briefing from your own data: today, overdue, next N days, reminders,
  pinned notes, focus stats and a suggested order — and a flat statement that
  no calendar is connected rather than invented meetings.
* Focus sessions: a labelled timer with planned minutes, break nudges, today's
  and last-7-days statistics.

## 4. Notes and memory

* Quick capture ("notes: …") and a full notes page: search, pin, edit, delete.
* Long-term memory in four kinds: **preference**, **fact**, **project**,
  **session**; add, edit, delete, search, export/import JSON.
* LUMI **asks before remembering** you by default, and prints what it would
  store.
* Secrets are refused: API-key shapes, `password=`, private-key blocks and
  card-like numbers never enter the memory table.
* Session context (what happened recently) is kept separately and can be cleared.
* Semantic/embedding search | ❌ not implemented (full-text search is) |

## 5. Files

Read and inspect (safe, no permission needed beyond *file operations*):

* `find_files` by name/extension/date/size, `search_in_files` (content search
  with regex), `list_folder`, `read_text_file`, `read_document`.
* `read_document` reads `.txt/.md/.csv/.json/.py/…` always; `.docx` and `.pdf`
  when the optional readers are installed, and says so plainly when they are not.

Change (needs *Write files* on, plus a permitted folder):

* `write_text_file`, `append`, `move_files`, `plan_file_organisation`,
  `delete_files` — each planned first and confirmed, with the exact paths shown.
* **Duplicate detection** groups identical files by size and hash, reports what
  is wasted, and never deletes anything.
* Reads outside your permitted folders are allowed except for protected
  locations; writes outside them are refused. Deleting is always confirmed.

## 6. Computer control (Windows)

| Capability | State |
| --- | --- |
| Open an application by name (Start menu, `PATH`, known apps) | ✅ (opt-in) |
| List open windows, focus a window, read its title | ✅ |
| Read the text of a window and its controls through UI Automation | ✅ |
| Type text, press keys, click a named control (accessibility first) | ✅ (opt-in) |
| Click coordinates, scroll | ✅ last resort, flagged as fragile |
| Screenshots | ✅ (opt-in) |
| Every step verified, stoppable, step-limited, and audited | ✅ |
| Linux/macOS desktop control | ❌ portable backend records the plan only |

Everything here is off until you enable its group in Settings → Permissions, and
keyboard/mouse actions show a "computer control is active" indicator in the
header.

## 7. Browser and email

* Open a site, search the web (DuckDuckGo by default), read the current page.
* Fill a field, manage tabs.
* Gmail flow: LUMI opens your logged-in browser, fills the draft, and shows you
  the exact To/Subject/Body before anything is sent. Sending is a separate,
  confirmed step, followed by a read-back to verify it went.
* Page content is treated as **untrusted data**: it is wrapped and marked, and
  instructions found in a page are never executed as commands.

## 8. Study and coding help

* The system prompt puts engineering study help on the Socratic route: a hint
  or the next step first, the full worked solution when you ask for it.
* Code help: LUMI reads the file before commenting on it (`read_text_file`,
  `search_in_files`) and is told never to claim code was run when it was not.
* Symbolic maths, plotting and running code | ❌ out of scope — LUMI has no
  Python execution tool, deliberately |

## 9. Voice

* Push-to-talk (`Ctrl+Alt+Space`) is a toggle: the first press starts listening,
  the second stops, transcribes locally and drops the text into the message box
  for you to edit or send.
* Speech output through Piper (best quality), the Windows voice (SAPI, no extra
  package), `pyttsx3`, or `espeak`, chosen by `speech.tts_engine`.
* "Read every reply out loud" speaks the answer on a background thread, so the
  window never freezes while it talks.
* Recordings are deleted after transcription unless you turn on
  `speech.retain_audio`.
* Needs the optional packages (`faster-whisper`, `sounddevice`, `numpy`); the
  first transcription downloads a model.
* Wake-word listening | ❌ setting exists, listening does not — use push-to-talk |
* **Not verified on this machine:** there is no audio device here, so the
  recorder, Piper and Whisper paths are the least-tested part of LUMI.

## 10. Security and privacy

See [security.md](security.md). In short: loopback-only model traffic, a strict
tool whitelist with JSON-schema validation, no `eval` anywhere, permission
switches that all start off, confirmation before consequential actions,
confirmations that fail closed, a redactor over logs and audit rows, secrets in
the credential manager, and a full audit trail you can read and clear.

## 11. Resource discipline

* No model is loaded until you ask something.
* Idle models are released on request (Settings → Model).
* The model list is cached, not polled; the status strip polls every 5 seconds
  and only that.
* All long work (model streaming, tool calls, scans) happens off the UI thread.
* Hardware assumptions are not baked in: the RAM/VRAM probe drives the advice.

## 12. Errors it survives

Tested paths include: Ollama not installed, not running, or answering with an
error; a model missing or deleted mid-session; a model that returns garbage or
an invalid tool call; a tool that times out, raises, or is refused; a
confirmation dialog that cannot be shown; the database being unreadable or
half-written settings; a file that has been deleted or is locked; a user who
presses Stop mid-stream; and a network pull that is cancelled.

## 13. Testing and documentation

* 351 automated tests (1 skipped: a Windows-only notification test), run
  offline with no model, no network and no display.
* `pytest -q` → `351 passed, 1 skipped`.
* Manual acceptance checklist: [acceptance.md](acceptance.md).
* Build instructions: [packaging.md](packaging.md).

---

## Known limitations, collected

1. **Small local models are small.** Quality is a 3B–7B model's, not a frontier
   model's. LUMI labels answers as local and is instructed to admit uncertainty.
2. **Cloud providers are not implemented.** The settings and the "prefer local"
   logic exist; no provider client ships.
3. **No calendar.** The briefing says it cannot see meetings instead of guessing.
4. **No image understanding in the UI** and no OCR for scanned PDFs.
5. **No code execution.** LUMI will not run Python for you; it reads, explains
   and writes files.
6. **Empty result sets are explicit.** "Duplicate scan found nothing" is always a
   statement about a folder LUMI could read.
7. **Windows-only paths are tested but not run on Windows here.** The
   accessibility, notification and Startup-folder paths are covered by unit
   tests on Linux; they have not been exercised on a real Windows machine in
   this development environment.
8. **Voice is unverified here** (no microphone, no speaker, no model downloaded).
9. **Desktop automation on Linux/macOS** is a no-op backend with a clear status.
10. **The database is not encrypted.** Protect it with BitLocker/EFS if it
    matters; it is a normal file in your profile.
11. **`wake_word` and `show_timestamps`-style cosmetic settings**: timestamps now
    work; the wake word does not listen yet. Checkboxes that do nothing are a bug
    in this project's eyes — this one is listed so it is not a surprise.
