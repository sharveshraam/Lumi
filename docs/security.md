# Security and privacy

LUMI can see your files, your screen and your applications, and it can type on
your behalf. That is the point of it — and it is also why the rules below are
enforced in code, not by asking the model nicely.

The short version:

1. Answers come from a model on your own machine.
2. The model never runs code. It can only ask for one of 69 registered tools.
3. Every tool call is checked against your permissions and written to an audit log.
4. Consequential actions always ask first.
5. Anything you ask LUMI to remember is checked for secrets and shown to you.

---

## 1. Local by default

* Ollama listens on `127.0.0.1:11434`; that is the only address LUMI calls.
* The HTTP client **refuses a non-loopback host** unless you explicitly turn on
  `ollama.allow_non_loopback_host`. A typo in the URL cannot silently send your
  prompts to a stranger's server.
* Cloud providers exist in the settings and are all `enabled: false`. There is
  **no cloud provider client in this build**.
* No telemetry, no crash reporting, no update check, no analytics.
* `privacy.telemetry` defaults to `false` and there is no code that would use it.

You can verify the first point with a packet capture or by unplugging the
network: LUMI keeps working.

## 2. The model cannot run code

There is no `eval`, no `exec`, no `os.system` and no dynamic import anywhere in
the tool path. A model answer that says "run this Python" is displayed to you
and nothing else.

What can happen is that the model asks for a **tool**, by name, with JSON
arguments. The executor then:

1. looks the name up in the registry (an unknown name is an error, not a guess);
2. validates the arguments against that tool's JSON schema — `additionalProperties`
   is `false`, so an unexpected field is rejected;
3. checks the turn's budget (max 3 tool calls per answer on default settings)
   and argument size (8000 characters);
4. asks the policy (section 4);
5. asks *you*, if the tool says it needs it;
6. runs the tool in a worker thread with a timeout, a stop flag and every
   failure caught;
7. writes the call and its result to `tool_audit`.

Tool calls are parsed from four formats (`<tool_call>`, fenced JSON, bare JSON,
JSON in prose) so a chatty model still works — but parsing only ever produces a
*request*. Nothing runs because the text looked plausible.

## 3. Reads are safe, writes are not

Tools are graded:

| Risk | Meaning | Example |
| --- | --- | --- |
| `safe` | no side effects | `current_datetime`, `list_tasks`, `find_files` |
| `moderate` | changes LUMI's own state, or a small reversible change | `add_task`, `write_text_file`, `open_application` |
| `consequential` | hard to undo, or leaves your machine | `send_email`, `delete_files`, `pull_model`, `clear_logs` |

`safe` tools run whenever automation is enabled. Everything else needs its
permission group switched on, and `consequential` tools always confirm.

The same grading exists on the permission switches themselves: **every
automation group except file *reading* is off in a fresh install.** A new user
has a chat assistant with tasks and notes, and nothing that can touch the
desktop until they say so.

## 4. The permission policy

Every call goes through `PermissionPolicy.evaluate(tool, arguments)`:

* `automation.enabled` is the master switch — off means every non-safe tool is
  refused with a clear reason.
* The tool's group must be on (`allow_app_launch`, `allow_keyboard_mouse`,
  `allow_screenshots`, `allow_file_writes`, `allow_browser_automation`,
  `allow_bulk_file_operations`).
* Anything with `permission_action` in `require_confirmation_for` is confirmed
  even if it is allowed.
* **Bulk operations** — more than one file in one call, or a folder-wide
  operation (`plan_file_organisation`) — need `allow_bulk_file_operations`,
  which is off by default. Moving one file is allowed; moving ten is not.
* Paths are checked separately:
  * writes must be inside `workspace_folders`;
  * `search_folders` are readable but not writable;
  * `blocklist_folders` is never touched, and beats everything else;
  * protected locations are refused even for reading: `~/.ssh`, `~/.aws`,
    `~/.config/gcloud`, browser profile folders, `id_rsa`, `.git-credentials`,
    `credentials.json`, `secrets.json`, `Login Data`, `key4.db`, `logins.json`,
    `C:\Windows`, `/etc`, `/usr/bin` and friends.
* Blocked **names** match anywhere, so a copy of `id_rsa` in your workspace is
  still refused.

Refusals are visible: the tool panel shows the call, the decision and the
reason. LUMI does not silently drop a denied action, and it is instructed never
to claim it did something that was refused.

## 5. Confirmations

A confirmation dialog shows the tool, what it will do, the exact paths or
recipients involved, and the risk. You can answer:

* **Allow once** (default),
* **Always allow this session** — remembered until LUMI exits, never written to
  disk by accident,
* **Deny**.

Rules:

* The dialog defaults to *Deny* and Enter does not confirm. Closing it denies.
* If the dialog cannot be shown, the action is **denied** — including when a
  tool is called from a background thread and the UI is unavailable.
* Sending an email is a two-step flow: LUMI fills the draft, shows you exactly
  what is in the To/Cc/Subject/Body fields, and only sends after you confirm
  that specific draft. It never sends silently, never adds a recipient you did
  not see, and it reads the page back after sending to verify.
* Deleting is previewed first (`plan_delete`) and reported afterwards.
* A model instruction inside a web page is **data, not an order**: content read
  from a page or a file is wrapped and marked as untrusted, and tool use is
  driven by your settings, not by anything a page says.

## 6. Stop

* The red **STOP** button in the header stops the current tool and the model
  stream. A tool that is already inside a blocking Windows call finishes that
  call; anything with a loop checks the flag (and the file scanner, model
  puller and recorder check it often).
* The Stop hotkey (`Ctrl+Alt+Shift+S` by default) is registered **with Windows
  itself** (`RegisterHotKey`), so it stops everything even when another
  application has the keyboard. If Windows refuses the combination - another
  program already owns it - LUMI says so in the button's tooltip and keeps the
  in-application shortcut as the fallback. Change it in Settings →
  Permissions → Emergency stop.
* Stopping is remembered per turn: a new message starts a fresh, unstoppable
  state.

## 7. Secrets

* API keys are never in `settings.json`. They come from an environment variable
  named in the provider config, or from the Windows credential manager
  (`keyring` if installed), which stores `secrets.json` with owner-only
  permissions as a fallback.
* Keys never appear in logs. The logger runs every message through a redactor
  that replaces `sk-…`, `AIza…`, `ghp_…`, `Bearer …`, `password=…`, private-key
  blocks and anything matching the `*_API_KEY` shape with `[redacted]`.
* The redactor also runs over tool arguments before they are written to the
  audit log, so a key pasted into a chat message does not land in the database.
* The memory service **refuses** to store anything that looks like a secret
  (API-key shapes, `password=`, card-like 16-digit numbers, private-key blocks)
  unless you explicitly say so, and the Memory page shows exactly what will be
  stored before it is.
* LUMI asks before remembering a fact about you (`ask_before_remembering`).

## 8. Audit log

Every tool call writes a row: time, tool name, redacted arguments, outcome,
duration, whether you confirmed it, and the session id. See it in
Settings → Activity, or ask *"what have you done recently?"*
(`read_activity`). `clear_logs` is `consequential` and asks first.

## 9. Files and data on disk

| File | Protection |
| --- | --- |
| `settings.json` | atomic writes; a corrupt file is kept, not lost |
| `lumi.db` | normal user permissions; no encryption (see below) |
| `secrets.json` | `0600`, only if the credential manager is unavailable |
| `logs\lumi.log` | redacted, rotating, 1 MB × 3 |
| `cache\screenshots` | only if you take screenshots; they are yours to delete |

**LUMI does not encrypt `lumi.db`.** Anyone with access to your Windows account
can read your tasks, notes and memory. If that matters to you, use BitLocker or
EFS — LUMI's data is a normal file in your profile, and it will be protected by
the same thing that protects everything else there.

## 10. Speech

Audio is captured in memory, transcribed, and **deleted** — unless you turn on
`speech.retain_audio`, in which case clips are kept in the data folder. Speech
recognition runs locally through faster-whisper: your voice is not uploaded.

## 11. What LUMI still trusts

Honest limitations:

* **The local model.** A model can be confused by text in a file or a web page.
  The defences are the policy, the confirmation dialogs and the risk grading —
  not the model's judgement.
* **Ollama.** LUMI trusts the local Ollama process. Anyone who can talk to port
  11434 can talk to your models.
* **Your Windows account.** LUMI runs with your user's rights. It does not
  elevate, which is why it cannot touch `C:\Windows` — and also why a
  compromised LUMI could do anything you can do.
* **Screenshots.** They can contain anything on screen, and they are written to
  your disk. That is why `allow_screenshots` is off by default.
* **The browser.** Automation drives *your* logged-in browser profile, because
  that is what makes Gmail work without storing your password. Anything that
  profile can reach (banking, mail) is technically reachable — so browser
  automation is off by default and every navigation is logged.

## Reporting a problem

Open an issue at <https://github.com/sharveshraam/Lumi/issues>. For anything
that looks like a security bug, please say so in the title, and avoid attaching
your `lumi.db` or unredacted logs.
