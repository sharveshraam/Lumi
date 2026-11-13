# Architecture

## Modules

| Module | Type | Contents |
| --- | --- | --- |
| `:core` | Kotlin/JVM library, no Android dependency | Date and recurrence parsing, task and reminder logic, memory, learning, intent parser, tool API, orchestrator, AI wire formats, notification filtering, briefing text |
| `:app` | Android application (`com.lumi.assistant`) | Storage, alarms, geofences, speech, assistant panel, notification and accessibility services, Android tools, Compose screens |

All decision logic lives in `:core` so it can be unit-tested on a plain JVM. `:app` supplies implementations of the interfaces that `:core` defines (`TaskRepository`, `ReminderScheduler`, `PlaceStore`, `MemoryStore`, `MessagingBackend`, `PermissionGate`, `AssistantSettings`).

## One turn

```
speech or typed text
  -> Orchestrator.handle(utterance, TurnEnv(deviceLocked, voice))
       1. pending state: confirmation, clarifying question, pattern offer, onboarding, open draft
       2. LocalIntentParser: regular commands become tool calls with no network use
       3. AI planner (only if the parser returns nothing and a provider is configured)
       4. per tool call: lock check -> permission check -> required parameters
          -> preview (resolve contact, build confirmation) -> confirm if needed -> execute
       5. Reply(text, expectReply, directive, success, suggestScreen)
  -> AssistActivity shows and speaks the reply, then listens again, closes, or opens a screen
```

Rules enforced by the orchestrator, each covered by tests in `core/src/test`:

- A tool with `allowedWhenLocked = false` is deferred until the phone is unlocked.
- Calls, deletions and "forget" need a spoken yes. Drafts are sent only by the user's own "send it".
- Tools marked `privateOutput` (notifications, messages, screen text, calendar) are never sent to the AI provider and never stored in history.
- A tool reports `verified = true` only when it read the result back (volume level, audio playing, SMS sent broadcast). Hand-offs to another app are described as hand-offs.
- A multi-step plan stops at the first failure.

## Android side

| Area | Files | Notes |
| --- | --- | --- |
| Object graph | `LumiApp.kt` | `Graph` builds everything once per process; nothing starts a thread or opens the microphone |
| Assistant entry | `assistant/VoiceServices.kt`, `assistant/AssistActivity.kt` | `VoiceInteractionService` makes Lumi selectable as the assistant; the session opens the compact panel |
| Speech | `voice/Voice.kt` | Platform `SpeechRecognizer` and `TextToSpeech` behind `SpeechToText` and `SpeechSynth` interfaces |
| Storage | `data/Db.kt`, `data/Settings.kt` | `SQLiteOpenHelper` database `lumi.db`; settings in SharedPreferences; API key encrypted with an Android Keystore AES-GCM key |
| Reminders | `reminders/Reminders.kt`, `reminders/Geofences.kt` | `AlarmManager` exact alarms with inexact fallback, Play services geofences, boot and time-change restore, 12-hour WorkManager safety net |
| Tools | `tools/PhoneTools.kt`, `tools/DeviceTools.kt`, `tools/Support.kt` | Calls, contacts, notifications, message delivery, apps, media, clock, navigation, screen, briefing, places |
| Services | `services/LumiNotificationListener.kt`, `services/LumiAccessibilityService.kt` | Both answer on-demand requests only; the accessibility service subscribes to no events |
| Screens | `ui/MainActivity.kt`, `ui/Screens.kt`, `ui/UiModel.kt` | Home, Tasks, Memory, Permissions, Places, Settings, Diagnostics (Jetpack Compose, Material 3) |

## AI providers

`AiClient.chat(messages, tools)` is the only interface the rest of the code sees. `HttpAiClient` implements it for three wire formats (`OPENAI_COMPATIBLE`, `ANTHROPIC`, `GEMINI`) using `HttpURLConnection`, https only. Adding a provider with one of these formats needs only a new entry in `AiPresets`.

## Data

Everything is stored locally in `lumi.db` (tasks, reminders, places, memories, patterns, conversation history, action log). Backup and device transfer are disabled in the manifest. The only network traffic Lumi itself produces is the request to the AI provider the user configured.
