# Battery

Lumi is built so that it uses no power when you are not using it.

## What Lumi does not do

- No wake word and no background microphone. The microphone opens when the panel shows "Listening" and closes on the result, on silence, and whenever the panel leaves the screen.
- No foreground service and no persistent notification.
- No polling, no periodic network requests, no wake locks held by Lumi's code.
- No location tracking. Location is read once when you save a place.
- No screen monitoring. The accessibility service receives no events.

## How the background features work

| Feature | Mechanism | Cost |
| --- | --- | --- |
| Timed reminders | One `AlarmManager` alarm per reminder (`setExactAndAllowWhileIdle`, or the inexact variant without the exact-alarm permission) | The process starts for a moment at the reminder time |
| Repeating reminders | The next occurrence is scheduled when the current one fires | Same as above |
| Location reminders | Google Play services geofences | Detection is shared with the system; Lumi is woken only on arrival or departure |
| Restore after reboot or time change | One broadcast receiver | Runs once per event |
| Safety net | One WorkManager job every 12 hours that re-registers alarms | Deferrable and batched by Android |
| Daily briefing | One inexact alarm per day, only if enabled | One short wake-up |
| Notification reading | Listener bound by Android only if you grant access; does nothing per notification unless announcements are enabled | A boolean check per notification |

## Conversation timeouts

The panel closes 6 seconds after silence, or 8 seconds after an answer when follow-up listening is off, so the screen is not kept on.

## Phones with aggressive battery managers

Some manufacturers (Xiaomi, Oppo, Vivo, Realme, OnePlus, Samsung in some modes) stop alarms of apps they consider inactive. If reminders arrive late or not at all:

1. Open **Diagnostics** and check "Exact alarms" and "Reminder notifications".
2. In **Permissions**, open "Unrestricted battery" and choose Unrestricted or "Don't optimise".
3. On Xiaomi, Oppo, Vivo and Realme also enable "Autostart" for Lumi in the phone's security app.

This does not make Lumi use more power; it only stops the phone from cancelling its alarms.
