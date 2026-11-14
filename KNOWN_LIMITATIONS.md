# Known limitations

## By design

- **No wake word.** Lumi starts only from the assistant gesture, a headset button or its icon. A wake word needs a continuously open microphone.
- **English only** for the offline command parser. Other languages reach the AI provider if one is configured.
- **Android 8.0 or later.**

## Limits set by Android or other apps

- **WhatsApp messages cannot be sent automatically.** WhatsApp offers no API for it. Lumi opens the chat with the text filled in and you tap send. Replies through a WhatsApp notification are sent directly.
- **Email is handed to your mail app** for you to review and send.
- **Alarms and timers are passed to the clock app.** Lumi cannot confirm they were set and says "I've asked your clock app".
- **Call status is unknown.** Lumi can start a call but cannot tell whether it connected.
- **Playing a named song depends on the music app.** Some apps show search results instead of playing. Lumi checks whether audio started and tells you if it did not.
- **Reading messages uses the notification shade.** A message whose notification was dismissed cannot be read. Lumi does not read the SMS inbox or chat history.
- **Location reminders need "Allow all the time" location and Google Play services.** Arrival can be detected a few minutes late, and being at the place already when the reminder is created does not trigger it.
- **Exact reminders need the "Alarms and reminders" permission** on Android 12+. Without it they can be late.
- **Some phones block alarms of unused apps.** See BATTERY_OPTIMIZATION.md.
- **Screen awareness cannot read** games, video, images, or apps that hide their content from accessibility. It never reads password fields.
- **Speech recognition is the phone's own service.** Accuracy and offline support depend on it.
- **On the lock screen** the panel can appear, but personal information and calls wait for unlock.

## Not built

- Flashlight, Wi-Fi, Bluetooth and brightness toggles. "Open Wi-Fi settings" opens the page instead.
- Reading or searching the SMS inbox and email.
- Creating calendar events (calendar is read-only).
- Editing a task's fields in the Tasks screen. Use a command such as "move it to Friday".
- Choosing which apps Lumi may read notifications from, in the app. Use "ignore this app" by voice.
- Cloud sync or backup. Uninstalling Lumi deletes its data.
- Instrumented (on-device) automated tests.

## Verification status

See LUMI_PROGRESS.md for what has and has not been compiled and run.
