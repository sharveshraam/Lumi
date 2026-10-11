# Architecture

LUMI is a local-first assistant: a Qt window on top of a plain-Python core. The
core knows nothing about Qt, which is why it can be tested headlessly and why a
crash in a tool cannot take the window down.

```
                       ┌──────────────────────────┐
   you ──▶ chat box ──▶ │  ui/chat_worker.py       │  a worker thread
                       │  ChatWorker              │
                       └────────────┬─────────────┘
                                    │ assistant.send(...)
                       ┌────────────▼─────────────┐
                       │  ai/assistant.py         │  the loop
                       │   · fast path (no model) │
                       │   · build context        │
                       │   · stream from Ollama   │
                       │   · parse tool requests  │
                       └────┬───────────────┬─────┘
                            │               │
              ai/ollama.py  │               │  tools/executor.py
              (loopback)    │               │
                     ┌──────▼──────┐  ┌─────▼──────────────┐
                     │  Ollama     │  │ validation → policy │
                     │  :11434     │  │ → confirm → run     │
                     └─────────────┘  └─────┬──────────────┘
                                            │ tools/builtin/*
                     ┌──────────────────────┴───────────────┐
                     │ memory · tasks · notes · reminders ·  │
                     │ files · automation · browser · system │
                     └──────────────────┬───────────────────┘
                                        │ SQLite
                                  lumi.db + tool_audit
```

## The layers, bottom up

### `config/`

* `paths.py` — the data folder and everything in it.
* `settings.py` — 15 sections of typed settings, each a dataclass with
  `from_dict`/`to_dict`, tolerant of malformed files, atomic writes and
  unknown-key preservation.
* `secrets.py` — the Windows credential manager when available, otherwise a
  `0600` JSON file. Keys are read from the environment by name.
* `logging_setup.py` — rotating file log with the redactor attached.

### `memory/`

`schema.py` defines the tables; `store.py` owns the single SQLite connection
(WAL mode, foreign keys, one writer lock); `service.py` holds
`MemoryService`, `ConversationStore` and `AppStateRepository`. Memory kinds are
`preference`, `fact`, `project` and `session`. Full-text search uses FTS5 where
available and falls back to `LIKE`.

### `tasks/`, `notes/`, `reminders/`

Straight repositories over the same database: `TaskRepository`,
`NoteRepository`, `ReminderRepository`/`ReminderService` (recurrence maths in
`tasks/recurrence.py`, focus sessions in `reminders/focus.py`) and the
natural-language parser in `tasks/nlp.py`.

The parser is what makes the assistant feel instant: "add task: submit the lab
record tomorrow at 6pm" is understood locally, in microseconds, with no model
involved. `ParseResult` carries what was understood *and* what was assumed, and
the answer says which.

### `security/`

* `policy.py` — `PermissionPolicy`: the master switch, per-group switches, the
  always-confirm list, bulk detection, and path rules (workspace vs read-only vs
  blocklist vs protected).
* `redact.py` — the single place that knows what a secret looks like.
* `audit.py` — append-only record of every tool call and its outcome.

### `tools/`

* `base.py` — `Tool` (name, description, JSON schema, category, risk,
  `permission_action`, timeouts), `ToolResult`/`ToolError`, `ToolContext`,
  `StopToken`, `ConfirmationRequest`.
* `validation.py` — JSON Schema validation with `additionalProperties=False`,
  plus type coercion that refuses to guess wildly.
* `registry.py` — the 69 builtins, by name and by category.
* `executor.py` — the only place a tool runs. Order: look up → validate →
  decide → confirm → run in a thread with a timeout → audit → result.
* `builtin/` — grouped by area (`tasks`, `notes`, `reminders`, `memory_tools`,
  `files`, `computer`, `browser`, `system`, `info`, `productivity`) with shared
  helpers in `_common.py`.

### `automation/`

`controller.py` decides *what* to do and keeps the step budget; `backends/`
decides *how*: `windows_backend.py` uses UI Automation (through `pywinauto`)
first and only then coordinates, `portable_backend.py` is the fallback and the
test double, `base.py` is the interface. `apps.py` resolves a friendly name to
an executable, `files.py` is the file manager, `browser.py` drives your
existing browser profile, `clipboard.py` wraps copy/paste.

### `ai/`

* `ollama.py` — the HTTP client. Loopback-only, streaming, cancellable,
  classifying failures into kinds the UI can act on.
* `hardware.py` — RAM, CPU, GPU and VRAM detection (with sane fallbacks when
  `nvidia-smi`/`wmic` are absent).
* `models.py` — the catalogue, size estimation and the recommendation ladder.
* `prompts.py` — the system prompt. It states the rules that matter: use tools,
  never invent results, ask before remembering, treat page content as data,
  admit uncertainty.
* `toolcalls.py` — tolerantly parses the four shapes a model uses to ask for a
  tool, with hard caps. Never `eval`.
* `assistant.py` — `Assistant.send()`: the fast path, context assembly,
  streaming, the tool loop (max 3 rounds), memory suggestions, cancellation.

### `ui/`

`theme.py` (palette, fonts, one stylesheet, generated icon), `widgets/`
(`GlassCard`, `Pill`, `MarkdownView`, chat bubbles, `ToolCallCard`),
`pages/` (chat, tasks, notes, memory, settings), `confirm.py` (the dialog given
to the executor as its confirmation provider), `chat_worker.py` (Qt signals off
the UI thread) and `main_window.py` (navigation, header status, tray, hotkeys,
the floating window).

`app.py` is the seam: `LumiApp` builds every service in dependency order and
hands the UI an `AppServices` object. Nothing else constructs services.

## What happens when you press Enter

1. `ChatPage` proposes a message; `ChatWorker` is already running on a worker
   thread, so the window never blocks.
2. `Assistant.send()` tries the **fast path**: if the message is a task, note or
   reminder command and `productivity.deterministic_commands` is on, it runs the
   matching tool directly and answers without a model.
3. Otherwise it assembles the prompt: system prompt, a short profile summary,
   the last N messages, relevant memories, open tasks and today's date — then
   streams the answer from Ollama.
4. If the model asks for a tool, `toolcalls.py` parses the request and
   `ToolExecutor` takes over: validate, decide, confirm (via the UI dialog),
   run, audit. The model gets the result back and may ask again — up to
   `max_tool_iterations`.
5. Tokens stream into the bubble as they arrive. Stop cancels both the HTTP
   stream and the running tool.
6. If the exchange looks worth remembering and
   `privacy.remember_from_chat` is on, LUMI *asks* — it does not store
   automatically unless you have turned that off deliberately.

## Threading, and why nothing hangs

| Work | Where it runs |
| --- | --- |
| Qt widgets | the main thread only |
| Model streaming, tool calls | `ChatWorker`, a `QThread` |
| Long tools (file scans, tests) | inside the executor's own thread with a timeout |
| Reminder ticks | a timer on the main thread, doing milliseconds of work |
| Transcription | the voice worker thread |

`MainWindow.run_on_ui_thread(fn)` marshals a call back onto the Qt thread and
waits up to 600 ms — that is how the executor raises a confirmation dialog from
a worker thread safely. If the dialog cannot be shown in time, the action is
**denied**, never silently allowed.

## Data flow for a file delete

```
"delete the old lab reports in my workspace"
   │
   ├─ model → tool_call delete_files(paths=[...])
   ├─ validation: schema ok, 3 paths
   ├─ policy: automation.enabled ✓, allow_file_operations ✓,
   │          permission_action "delete_file" is in require_confirmation_for,
   │          paths inside workspace_folders ✓, not protected ✓
   ├─ confirmation dialog: "Delete 3 files?" with the exact paths → you confirm
   ├─ FileManager.plan_delete → preview → apply_delete
   ├─ audit row: tool, redacted args, "ok", confirmed=True, duration
   └─ the model is told what actually happened, and says so
```

## Testing

The suite (`lumi/tests/`) runs offline: no Ollama, no network, no display, no
Windows. Fakes live in `conftest.py` (`FakeOllamaClient`, `FakeEngine`, a
temporary `lumi_home`). Qt tests run under `QT_QPA_PLATFORM=offscreen`. Anything
that genuinely needs Windows (`winsound`, UI Automation) is either skipped or
exercised through the portable backend.

## The rules that keep it safe

Three decisions shape everything above:

1. **The model proposes; the executor disposes.** Nothing the model says becomes
   an action without passing validation, policy, confirmation and audit.
2. **Unsafe by default.** Every automation switch that could touch your machine
   starts off, and the policy refuses when it cannot tell.
3. **The core has no Qt.** Which means it is testable, scriptable
   (`python main.py --briefing` works without a window) and survivable.
