# Permissions

Lumi requests a permission only when a feature needs it. Each one can be granted or revoked from the Permissions screen, which also shows what it is for.

| Permission | Used for | Without it |
| --- | --- | --- |
| Default digital assistant role | Start Lumi with the assistant gesture or headset button | Start Lumi from its icon |
| `RECORD_AUDIO` | Hearing you while the panel shows "Listening" | Type in the panel instead |
| `POST_NOTIFICATIONS` (Android 13+) | Showing reminders and the briefing | Reminders cannot be shown; Lumi says so when you create one |
| `SCHEDULE_EXACT_ALARM` (Android 12+) | Reminders at the exact minute | Reminders may arrive some minutes late; Lumi tells you |
| `RECEIVE_BOOT_COMPLETED` | Re-registering reminders after a restart | Reminders would be lost on reboot |
| `INTERNET` | Requests to the AI provider you configured | Not used if no provider is set |
| `READ_CONTACTS` | Resolving names for calls and messages | Dictate a number instead |
| `CALL_PHONE` | Placing a call directly | Lumi opens the dialer and you tap call |
| `READ_CALL_LOG` | "Call back the last caller" | That command is unavailable |
| `SEND_SMS` | Sending a text after you approve it | Lumi opens your messaging app with the text filled in |
| `READ_CALENDAR` | Events in the briefing and "what's on my calendar" | Calendar section is left out |
| `ACCESS_FINE_LOCATION`, `ACCESS_COARSE_LOCATION` | One location fix when you save a place | Places cannot be saved |
| `ACCESS_BACKGROUND_LOCATION` | Required by Android for geofence reminders | Location reminders stay pending; time reminders are unaffected |
| `MODIFY_AUDIO_SETTINGS` | Using a Bluetooth headset microphone while listening | The phone microphone is used |
| Notification access | Reading and replying to notifications on request | Notification features are unavailable |
| Accessibility service | Screen awareness on request | Screen features are unavailable |
| Unrestricted battery | Only for phones whose battery manager blocks alarms | Nothing on most phones |

## Sensitive access

**Accessibility service.** Declared with no event types, so Android delivers no screen events to Lumi. Lumi reads the active window only inside a `read_screen`, `screen_action` or `fill_field` request, skips password fields, and stores nothing. It is gated twice: the Android accessibility switch and Lumi's own "Use screen awareness" switch, which shows a disclosure before it turns on.

**Notification access.** Lumi reads active notifications only when asked. The optional "announce important notifications" setting is the single case where an incoming notification is inspected as it arrives, and it is off by default. Notification content is never written to the database or log and never sent to an AI provider.

**Calls and SMS.** A call needs a spoken confirmation unless you turn that off. A message is sent only after the draft was read back and you said "send it".

**Locked phone.** While the phone is locked Lumi will not read tasks, memories, messages, notifications or calendar, and will not call or send. Media controls, volume, alarms, timers and battery level work.

## Distribution note

`SEND_SMS`, `READ_CALL_LOG` and `ACCESS_BACKGROUND_LOCATION` are restricted on Google Play and need a policy declaration. The APK built here is intended for direct installation.
