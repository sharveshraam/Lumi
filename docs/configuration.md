# Configuration

Everything is in `settings.json` inside your LUMI data folder
(`%LOCALAPPDATA%\LUMI` by default). The Settings window edits the same values;
the file is there so you can inspect, back up or script it.

The file is written atomically (a temporary file, then a rename), so a crash
cannot leave it half-written. If it ever becomes unreadable, LUMI keeps a copy
as `settings.corrupt-<timestamp>.json` and starts from the defaults. Unknown
keys are preserved, which means a setting written by a newer version survives a
downgrade.

Values that are out of range or the wrong type are replaced with the default
rather than raising: LUMI always starts.

## `ollama` — the local engine

| Key | Default | Meaning |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:11434` | where Ollama listens. Non-loopback hosts are refused unless `allow_non_loopback_host` is true. A value that is not a URL falls back to the default. |
| `connect_timeout_s` | `3.0` | how long to wait for a connection |
| `request_timeout_s` | `180.0` | how long an answer may take |
| `keep_alive` | `10m` | how long Ollama keeps the model in memory |
| `allow_non_loopback_host` | `false` | allow a server on another machine |
| `unload_on_idle` / `idle_unload_after_s` | `true` / `600` | release memory when idle |
| `auto_pull_models` | `false` | never download a model without being asked |

## `model` — which model and how it behaves

| Key | Default | Meaning |
| --- | --- | --- |
| `selected_model` | `""` | the model LUMI asks. Empty means "pick one for me". |
| `quality_profile` | `balanced` | `fast`, `balanced` or `quality` |
| `use_profile_limits` | `true` | apply the profile's context/answer limits |
| `hidden_models` | `[]` | installed models to keep out of the menu |
| `system_prompt_extra` | `""` | extra instructions added openly to the system prompt |
| `enable_tools` | `true` | master switch for tool use |
| `max_tool_iterations` | `3` | how many tool steps one answer may take |

## `generation` — the sampler

`temperature` (0.6), `top_p` (0.9), `top_k` (40), `repeat_penalty` (1.1),
`num_ctx` (4096), `num_predict` (512), `seed`, `stop`.

Lower `temperature` makes answers more predictable; `num_ctx` and `num_predict`
are the two that change memory use and speed (see [models.md](models.md)).

## `appearance`

`theme` (`dark`), `accent`, `font_scale`, `compact_mode`, `window_mode`,
window geometry, `always_on_top`, `show_timestamps`.

## `speech` — voice

`enabled`, `tts_enabled`, `tts_engine` (`auto`/`piper`/`sapi`/`pyttsx3`/`espeak`/`none`),
`piper_exe`, `piper_voice`, `rate`, `volume`, `stt_engine`, `stt_model`
(`tiny`/`base`/`small`/`medium`), `stt_device`, `stt_language`,
`push_to_talk_enabled`, `push_to_talk_hotkey` (`Ctrl+Alt+Space`),
`wake_word_enabled`, `wake_word`, `microphone_device`, `input_gain`,
`silence_timeout_s`, `max_record_seconds`, **`retain_audio` (false)**,
`speak_replies`.

`retain_audio` is off by default: the recording is deleted the moment the
transcription finishes.

## `automation` — what LUMI may do to your computer

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | master switch; when off, every non-safe tool is refused |
| `allow_app_launch` | **false** | open and switch applications |
| `allow_keyboard_mouse` | **false** | move the mouse and type |
| `allow_screenshots` | **false** | capture the screen |
| `allow_file_operations` | `true` | read and search files (reading is safe; writing is separate) |
| `allow_file_writes` | **false** | create and change files |
| `allow_browser_automation` | **false** | search, read pages, Gmail drafts |
| `allow_bulk_file_operations` | **false** | more than one file in a single action, or a whole folder |
| `require_confirmation_for` | see below | actions that always ask, even if allowed |
| `workspace_folders` | `[]` | folders with full access |
| `search_folders` | `[]` | read-only folders |
| `blocklist_folders` | `[]` | never opened, whatever else says |
| `emergency_stop_hotkey` | `Ctrl+Alt+Shift+S` | global stop |
| `action_delay_ms`, `typing_interval_ms` | 60, 12 | pauses between automated steps |
| `step_timeout_s`, `max_steps_per_sequence` | 20, 60 | per-step and per-action limits |

Default `require_confirmation_for`: `send_email`, `submit_form`, `delete_file`,
`purchase`, `install_software`, `change_security_settings`,
`bulk_file_operation`.

The policy always applies the **stricter** of the switch and the confirmation
list: an allowed action that is also listed still asks.

## `privacy`

| Key | Default | Meaning |
| --- | --- | --- |
| `save_conversations` | `true` | keep chat history in `lumi.db` |
| `conversation_retention_days` | `30` | delete older chats at startup (`0` = forever) |
| `remember_from_chat` | `false` | offer to remember things you say |
| `ask_before_remembering` | `true` | never store a memory without a yes |
| `telemetry` | `false` | there is no telemetry code at all |
| `log_level` | `INFO` | `DEBUG` writes much more, including tool arguments |
| `log_prompts` | `false` | write prompts to the log (off; prompts can contain personal data) |
| `allow_file_indexing` | `false` | reserved for content indexing |

## `notifications`

`reminders_enabled`, `sound_enabled`, `tray_notifications`,
`focus_length_minutes` (25), `break_length_minutes` (5), `break_notifications`,
`daily_briefing_enabled` (false), `daily_briefing_time` (`08:30`),
`catch_up_missed_reminders` (true).

## `startup`

`launch_on_login` (false), `start_minimised`, `minimize_to_tray` (true),
`close_to_tray` (true), `check_ollama_on_start`, `show_briefing_on_start`.

## `productivity`

`default_category`, `default_priority`, `show_completed_tasks`,
`overdue_first`, `deterministic_commands` (true — the instant answers for
tasks, notes and reminders that need no model), `task_categories`.

## `browser`

`search_engine` (`duckduckgo`), `preferred_browser`, `confirm_before_navigating`,
`read_page_method`, `gmail_compose_url`, `max_page_chars`,
`open_links_in_new_tab`, `home_page`.

## `profile`

`display_name`, `email`, `college`, `course`, `signature` — used when drafting
messages. Nothing here is sent anywhere.

## `cloud`

`preferred_provider` (`local`), plus a table of providers
(`gemini`, `openai`, `anthropic`, …), each with `enabled: false`,
`api_key_env`, `model`, `base_url`, `max_requests_per_day` and
`allow_automatic_fallback`.

**No provider is implemented in this build and none is enabled.** If you set
one up later, the key comes from an environment variable or the Windows
credential manager — never from `settings.json`, and never from the log.

## Environment variables

| Variable | Effect |
| --- | --- |
| `LUMI_HOME` | the data folder (same as `--home`) |
| `LUMI_LOG_LEVEL` | overrides `privacy.log_level` for one run |

No API key is read unless you configure a cloud provider yourself.
