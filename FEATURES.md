# Features

Phrases are examples; the parser accepts many variations. Items marked *AI* need an AI provider; everything else works offline (speech recognition itself may use the network, depending on the phone's recogniser).

## Tasks and reminders

| Say | Result |
| --- | --- |
| "Remind me to call mom tomorrow at 5" | Task with an alarm at 5 PM tomorrow |
| "Remind me to take medicine every day at 8 am" | Recurring reminder |
| "Remind me to submit the form when I reach college" | Location reminder (needs a saved place) |
| "Add a task to buy milk" | Task with no alarm |
| "Remind me to pay rent" | Lumi asks "When should I remind you?" |
| "What's pending", "what's due today" | Reads the list |
| "Mark buy milk as done", "delete the rent task" | Completes or deletes (delete asks first) |
| "Postpone it by an hour", "move it to Friday" | Reschedules |
| "Keep reminding me until I finish" | Repeats the reminder until completed, up to 5 more times |

Reminder notifications have **Done**, **Snooze 10 min** and **In 1 hour** buttons. Reminders are re-registered after reboot, app update and time or time-zone changes; any that were missed are shown as missed.

Defaults: "tomorrow" means 9:00 AM (changeable in Settings), "tonight" 9 PM, "morning" 8 AM, and "at 3" means the next 3 PM.

## Calls and messages

| Say | Result |
| --- | --- |
| "Call Dad" | Asks "Call Ramesh Kumar?" then calls |
| "Call back the last caller" | Uses the call log |
| "Call Priya on WhatsApp" | Starts a WhatsApp voice call if the contact supports it, otherwise opens the chat |
| "Message Priya I'm running late" | Drafts, reads back, sends on "send it" |
| "Reply I'll be there in ten minutes" | Replies through the notification |
| "Change it to ...", "make it more formal" (*AI*) | Edits the draft |
| "Email Arun about the report" | Opens your mail app with the draft filled in |
| "Dad is Ramesh Kumar in my contacts" | Saves a nickname |

## Notifications

"Read my notifications", "summarise my notifications", "anything important", "read the message from Arun", "ignore this app", "pause notifications". Reading is off until you enable it and grant notification access. Announcing notifications as they arrive is a separate switch and is off by default.

## Phone control

"Open Spotify", "open Wi-Fi settings", "play my workout playlist", "pause", "next song", "what's playing", "volume up", "set volume to 40 percent", "set an alarm for 6:30", "set a timer for 10 minutes", "navigate to the airport", "search for train times", "what's my battery".

## Memory and learning

"Remember that ...", "what do you know about ...", "forget that", "I prefer Spotify for music", "stop learning". After the same reminder is created three times Lumi offers to make it recurring; a declined offer is not repeated. The Memory screen lists everything and has a switch per category.

Saying "set me up" starts a short optional set of questions (name, schedule, preferred apps).

## Screen awareness (optional)

"What's on this screen", "what does this error mean" (*AI* for explanations), "is there a submit button", "scroll down", "go back", "tap continue", "type hello there". Typing into a form field always asks first. Password fields are skipped.

## Daily briefing (optional)

One notification at the time you choose with today's tasks, overdue tasks, calendar events, a notification summary and optionally battery level. Tap it to hear it, or say "give me my briefing".

## Conversation controls

"Stop", "repeat that", "speak slower", "speak faster", "never mind", "open my tasks", "open settings".

## App screens

Home, Tasks and reminders, Memory, Permissions, Places, Settings, Diagnostics.
