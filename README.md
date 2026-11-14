# Lumi

Lumi is a voice assistant for Android that runs only when you call it. It manages tasks and reminders, places calls, drafts messages, reads notifications on request, controls media and opens apps. There is no wake word and no background listening.

## What it does

- **Tasks and reminders**: "Remind me to call mom tomorrow at 5", "every Monday at 9", "when I reach college". Snooze, postpone and complete from the notification.
- **Calls and messages**: "Call Dad", "Message Priya I'm running late". Drafts are read back and sent only after you say "send it".
- **Notifications**: "Read my notifications", "what did Arun say". One-time codes and banking details are not read aloud.
- **Phone control**: open apps, play music, volume, alarms, timers, navigation, web search.
- **Memory**: "Remember that my locker code hint is blue". Everything stays on the phone and can be viewed and deleted.
- **Screen awareness** (optional): "What does this error mean", "scroll down", "tap continue".
- **Daily briefing** (optional): one notification a day with tasks, calendar and a notification summary.
- **AI provider** (optional): OpenAI, Anthropic, Gemini, OpenRouter, Groq or any OpenAI-compatible endpoint, for open questions and rewriting. All of the above works without it.

See [FEATURES.md](FEATURES.md) for the full list with example phrases.

## Install

1. Download `lumi-debug-apk` from the latest run of the **Android Build** workflow (Actions tab), or a release APK from Releases. See [GITHUB_ACTIONS.md](GITHUB_ACTIONS.md).
2. Install the APK and open Lumi.
3. In **Permissions**, set Lumi as the default digital assistant and allow the microphone and notifications.
4. Hold the power or home button (whichever opens your assistant) and speak.

## Build

Requires JDK 17 and the Android SDK (platform 34).

```
./gradlew :core:test :app:testDebugUnitTest
./gradlew :app:assembleDebug
```

The APK is written to `app/build/outputs/apk/debug/`.

## Documentation

| File | Contents |
| --- | --- |
| [LUMI_PROGRESS.md](LUMI_PROGRESS.md) | What is built, what has been verified, what is left |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Modules, turn pipeline, data storage |
| [FEATURES.md](FEATURES.md) | Features and example phrases |
| [PERMISSIONS.md](PERMISSIONS.md) | Every permission, why it exists, what happens without it |
| [BATTERY_OPTIMIZATION.md](BATTERY_OPTIMIZATION.md) | How Lumi avoids background work |
| [GITHUB_ACTIONS.md](GITHUB_ACTIONS.md) | CI setup, artifacts, signing, releases |
| [TESTING.md](TESTING.md) | Automated tests and the on-device checklist |
| [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) | What Lumi cannot do and why |
