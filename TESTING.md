# Testing

## Automated

```
./gradlew :core:test :app:testDebugUnitTest
```

`:core` has 130 JUnit tests that run on a plain JVM:

| File | Covers |
| --- | --- |
| `TimeTest.kt` | Date and time phrases, durations, recurrence, month-end clamping |
| `TaskServiceTest.kt` | Create, complete, postpone, snooze, nagging, scheduler failure, restore after reboot, location reminders |
| `LocalIntentParserTest.kt` | Mapping of spoken commands to tool calls |
| `ConversationTest.kt` | Confirmations, clarifying questions, locked-phone behaviour, drafts, pattern offers, onboarding |
| `MemoryAndAiTest.kt` | Memory policy, provider wire formats, AI errors, private content never reaching the AI |
| `NotificationAndBriefingTest.kt` | Notification filtering, sensitive content, quiet hours, briefing text |

`:app` has a small unit test (`DescribeTest.kt`). There are no instrumented tests; the Android layer is covered by the checklist below.

CI also runs Android lint and uploads the report in the `lumi-reports` artifact. Lint findings do not fail the build.

## On-device checklist

Run after installing a new build. Use a physical phone; emulators lack a speech recogniser by default.

**Setup**
- [ ] Lumi appears under Settings > Apps > Default apps > Digital assistant app and can be selected.
- [ ] The assistant gesture opens the Lumi panel and it shows "Listening".
- [ ] Denying the microphone shows the message and typing still works.

**Reminders**
- [ ] "Remind me to test Lumi in 2 minutes" fires on time with the screen off.
- [ ] Done, Snooze and In 1 hour on the notification work.
- [ ] Create a reminder 5 minutes ahead, reboot, and confirm it still fires.
- [ ] Create a reminder 2 minutes ahead, power off for 5 minutes, and confirm it shows as missed after boot.
- [ ] Revoke "Alarms and reminders" and confirm Lumi says the reminder may be late.
- [ ] Block notifications and confirm Lumi warns when creating a reminder.

**Location**
- [ ] Save a place, create "remind me to ... when I reach <place>", leave and return.

**Calls and messages**
- [ ] "Call <contact>" asks for confirmation; "no" cancels.
- [ ] "Message <contact> ..." reads the draft back and sends only on "send it".
- [ ] With SMS permission revoked the messaging app opens with the text filled in.

**Locked phone**
- [ ] "What are my tasks" is refused while locked.
- [ ] "Call <contact>" asks to unlock and continues after unlocking.
- [ ] "Pause" and "volume up" work while locked.

**Notifications and screen**
- [ ] "Read my notifications" reads them; a one-time code is not read.
- [ ] With screen awareness on, "scroll down" and "tap <label>" work in another app.

**Battery**
- [ ] After a day of normal use, Lumi's entry in Settings > Battery shows negligible background use.
- [ ] No microphone indicator appears while the panel is closed.

**Accessibility**
- [ ] With TalkBack on, every control on each screen is announced with its name and state.
- [ ] At the largest font size, no text is cut off on the Home, Tasks and Settings screens.
