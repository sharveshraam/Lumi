package com.lumi.assistant.tools

import android.Manifest
import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.os.Build
import android.provider.CallLog
import android.provider.ContactsContract
import android.provider.ContactsContract.CommonDataKinds.Phone
import android.telephony.SmsManager
import com.lumi.assistant.data.Settings
import com.lumi.assistant.services.LumiAccessibilityService
import com.lumi.assistant.services.LumiNotificationListener
import com.lumi.core.ai.ToolParam
import com.lumi.core.memory.MemoryService
import com.lumi.core.notify.NotificationIntel
import com.lumi.core.notify.NotificationItem
import com.lumi.core.tools.Args
import com.lumi.core.tools.Confirmation
import com.lumi.core.tools.Directive
import com.lumi.core.tools.Draft
import com.lumi.core.tools.MessagingBackend
import com.lumi.core.tools.Preview
import com.lumi.core.tools.Recipient
import com.lumi.core.tools.ReplyTarget
import com.lumi.core.tools.SessionState
import com.lumi.core.tools.Tool
import com.lumi.core.tools.ToolIds
import com.lumi.core.tools.ToolResult
import com.lumi.core.tools.ToolSpec
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume

/**
 * Calling, contacts, notifications and message delivery. Every reply states what Android
 * actually did: handing something to another app is never described as having sent it.
 */
class PhoneTools(
    private val s: ToolSupport,
    private val settings: Settings,
    private val memory: MemoryService,
    private val session: SessionState,
) {
    private val context: Context get() = s.context

    fun all(): List<Tool> = listOf(call, findContactTool, readMessages, notifications, ignoreApp, pauseNotifications)

    // ------------------------------------------------------------------ contacts

    data class ContactHit(val contactId: Long, val name: String, val number: String)

    private sealed class ContactResult {
        data class Found(val hit: ContactHit) : ContactResult()
        data class Many(val hits: List<ContactHit>) : ContactResult()
        object None : ContactResult()
        object NoPermission : ContactResult()
    }

    /** Looks a spoken name up in contacts, applying the user's saved nicknames first. */
    private suspend fun findContact(spoken: String): ContactResult {
        if (!s.has(Manifest.permission.READ_CONTACTS)) return ContactResult.NoPermission
        val cleaned = spoken.trim().removePrefix("my ").trim()
        val name = memory.resolveAlias(cleaned) ?: cleaned
        val hits = withContext(Dispatchers.IO) { queryContacts(name) }
        if (hits.isEmpty()) return ContactResult.None
        val exact = hits.filter { it.name.equals(name, ignoreCase = true) }
        val firstName = hits.filter { it.name.substringBefore(' ').equals(name, ignoreCase = true) }
        return when {
            exact.size == 1 -> ContactResult.Found(exact[0])
            hits.size == 1 -> ContactResult.Found(hits[0])
            exact.isEmpty() && firstName.size == 1 -> ContactResult.Found(firstName[0])
            else -> ContactResult.Many((exact.ifEmpty { firstName.ifEmpty { hits } }).take(5))
        }
    }

    private fun queryContacts(name: String): List<ContactHit> {
        val uri = Uri.withAppendedPath(Phone.CONTENT_FILTER_URI, Uri.encode(name))
        val best = LinkedHashMap<Long, Pair<Int, ContactHit>>()
        try {
            context.contentResolver.query(uri, arrayOf(Phone.CONTACT_ID, Phone.DISPLAY_NAME, Phone.NUMBER, Phone.IS_SUPER_PRIMARY, Phone.TYPE), null, null, null)?.use { c ->
                while (c.moveToNext()) {
                    val id = c.getLong(0)
                    val display = c.getString(1) ?: continue
                    val number = c.getString(2) ?: continue
                    val score = if (c.getInt(3) != 0) 0 else if (c.getInt(4) == Phone.TYPE_MOBILE) 1 else 2
                    val existing = best[id]
                    if (existing == null || score < existing.first) best[id] = score to ContactHit(id, display, number)
                }
            }
        } catch (e: Exception) {
            return emptyList()
        }
        return best.values.map { it.second }
    }

    private fun lastCaller(): ContactHit? = try {
        var hit: ContactHit? = null
        context.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            arrayOf(CallLog.Calls.NUMBER, CallLog.Calls.CACHED_NAME),
            "${CallLog.Calls.TYPE} IN (${CallLog.Calls.INCOMING_TYPE}, ${CallLog.Calls.MISSED_TYPE})",
            null,
            "${CallLog.Calls.DATE} DESC",
        )?.use { c ->
            while (hit == null && c.moveToNext()) {
                val number = c.getString(0)
                if (!number.isNullOrBlank()) hit = ContactHit(-1, c.getString(1)?.takeIf { it.isNotBlank() } ?: number, number)
            }
        }
        hit
    } catch (e: Exception) {
        null
    }

    // ------------------------------------------------------------------ calls

    private val call: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.CALL,
            description = "Call a contact by name or nickname, a phone number, or the last person who called. Supports WhatsApp voice calls.",
            params = listOf(
                ToolParam("contact", "string", "Contact name or nickname, e.g. 'Dad'."),
                ToolParam("number", "string", "Phone number, if the user dictated one."),
                ToolParam("last_caller", "boolean", "True to call back the most recent incoming or missed call."),
                ToolParam("via", "string", "How to call.", enumValues = listOf("phone", "whatsapp")),
            ),
            confirmation = Confirmation.IF_ENABLED,
            verification = "Reports whether the call was placed directly or only handed to the dialer. Lumi cannot observe whether the call connected.",
        ),
        pre = { args, _ -> previewCall(args) },
        run = { args, _ ->
            val number = args.string("number")
            val name = args.string("display") ?: number.orEmpty()
            if (number == null) {
                ToolResult.fail("I don't have a number to call.")
            } else if (args.string("via") == "whatsapp") {
                whatsappCall(args.long("contact_id"), number, name)
            } else if (s.has(Manifest.permission.CALL_PHONE)) {
                if (s.launch(Intent(Intent.ACTION_CALL, Uri.fromParts("tel", s.digits(number), null)))) {
                    ToolResult(true, "Calling $name.", verified = false, directive = Directive.Dismiss)
                } else {
                    ToolResult.fail("Android wouldn't let me place the call.")
                }
            } else if (s.launch(Intent(Intent.ACTION_DIAL, Uri.fromParts("tel", s.digits(number), null)))) {
                ToolResult(true, "I've opened the dialer with $name's number. Tap call to start it. To let me place calls directly, grant the phone permission.", verified = false, suggestScreen = "permissions")
            } else {
                ToolResult.fail("I couldn't open the dialer on this phone.")
            }
        },
    )

    private suspend fun previewCall(args: Args): Preview {
        val suffix = if (args.string("via") == "whatsapp") " on WhatsApp" else ""
        val number = args.string("number")
        if (number != null && args.string("display") != null) return Preview.Confirm("Call ${args.string("display")}$suffix?", args.json)
        if (number != null) return Preview.Confirm("Call ${s.spokenNumber(number)}?", args.with("display", s.spokenNumber(number)))
        if (args.bool("last_caller") == true) {
            if (!s.has(Manifest.permission.READ_CALL_LOG)) {
                return Preview.Fail("I need the call log permission to find who called you last. You can grant it in Lumi's Permissions screen.", "permissions")
            }
            val hit = withContext(Dispatchers.IO) { lastCaller() } ?: return Preview.Fail("I couldn't find a recent incoming call.")
            val spoken = if (hit.name == hit.number) s.spokenNumber(hit.number) else hit.name
            return Preview.Confirm("Call back $spoken?", Args(args.with("number", hit.number)).with("display", spoken))
        }
        val contact = args.string("contact") ?: return Preview.Ask("contact", "Who should I call?")
        return when (val r = findContact(contact)) {
            is ContactResult.Found -> {
                val a = Args(Args(args.with("number", r.hit.number)).with("display", r.hit.name)).with("contact_id", r.hit.contactId.toString())
                Preview.Confirm("Call ${r.hit.name}$suffix?", a)
            }
            is ContactResult.Many -> Preview.Ask("contact", "I found ${r.hits.joinToString(", ") { it.name }}. Which one?")
            ContactResult.None -> Preview.Fail("I couldn't find \"$contact\" in your contacts. You can teach me a nickname by saying, for example, \"Dad is Ramesh Kumar in my contacts\".")
            ContactResult.NoPermission -> Preview.Fail("I need the contacts permission to look up names. You can grant it in Lumi's Permissions screen.", "permissions")
        }
    }

    private suspend fun whatsappCall(contactId: Long?, number: String, name: String): ToolResult {
        val mime = "vnd.android.cursor.item/vnd.com.whatsapp.voip.call"
        if (contactId != null && contactId > 0 && s.has(Manifest.permission.READ_CONTACTS)) {
            val dataId = withContext(Dispatchers.IO) {
                try {
                    context.contentResolver.query(
                        ContactsContract.Data.CONTENT_URI,
                        arrayOf(ContactsContract.Data._ID),
                        "${ContactsContract.Data.CONTACT_ID} = ? AND ${ContactsContract.Data.MIMETYPE} = ?",
                        arrayOf(contactId.toString(), mime),
                        null,
                    )?.use { c -> if (c.moveToFirst()) c.getLong(0) else null }
                } catch (e: Exception) {
                    null
                }
            }
            if (dataId != null) {
                val intent = Intent(Intent.ACTION_VIEW).setDataAndType(Uri.parse("content://com.android.contacts/data/$dataId"), mime).setPackage(WHATSAPP)
                if (s.launch(intent)) return ToolResult(true, "Starting a WhatsApp call with $name.", verified = false, directive = Directive.Dismiss)
            }
        }
        // WhatsApp has no public call API; opening the chat is the honest fallback.
        val chat = Intent(Intent.ACTION_VIEW, Uri.parse("https://wa.me/${s.digits(number).removePrefix("+")}")).setPackage(WHATSAPP)
        return if (s.launch(chat)) {
            ToolResult(true, "I couldn't start the WhatsApp call directly, so I opened your chat with $name. Tap the call button there.", verified = false)
        } else {
            ToolResult.fail("WhatsApp doesn't seem to be installed, so I couldn't call $name there.")
        }
    }

    private val findContactTool: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.FIND_CONTACT,
            description = "Look up a contact's phone number by name or nickname.",
            params = listOf(ToolParam("name", "string", "Contact name or nickname.", required = true, prompt = "Whose number do you need?")),
            permissions = listOf(Manifest.permission.READ_CONTACTS),
            privateOutput = true,
        ),
        run = { args, _ ->
            when (val r = findContact(args.string("name").orEmpty())) {
                is ContactResult.Found -> ToolResult.ok("${r.hit.name}'s number is ${s.spokenNumber(r.hit.number)}.", verified = true)
                is ContactResult.Many -> ToolResult.ok("I found ${r.hits.joinToString("; ") { "${it.name}, ${s.spokenNumber(it.number)}" }}.")
                ContactResult.None -> ToolResult.fail("I couldn't find \"${args.string("name")}\" in your contacts.")
                ContactResult.NoPermission -> s.needs(Manifest.permission.READ_CONTACTS, "to look up contacts")
            }
        },
    )

    // ------------------------------------------------------------------ notifications

    private class Shade(val items: List<NotificationItem>?, val problem: ToolResult?)

    /** Current notifications, or the reason they cannot be read. */
    private fun shade(): Shade {
        val prefs = settings.notificationPrefs
        if (!prefs.enabled) {
            return Shade(null, ToolResult.fail("Notification reading is switched off. You can turn it on in Lumi's settings, under Notifications.", suggestScreen = "settings"))
        }
        if (!LumiNotificationListener.isEnabled(context)) {
            return Shade(null, ToolResult.fail("I need notification access for that. You can grant it in Lumi's Permissions screen.", suggestScreen = "permissions"))
        }
        if (prefs.paused) return Shade(null, ToolResult.fail("Notification reading is paused. Say \"resume notifications\" to turn it back on."))
        val listener = LumiNotificationListener.instance
            ?: return Shade(null, ToolResult.fail("Android hasn't connected Lumi's notification listener yet. Try again in a moment, or switch notification access off and on."))
        return Shade(NotificationIntel.usable(listener.items(), prefs), null)
    }

    private val readMessages: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.READ_MESSAGES,
            description = "Read the latest message notification aloud, optionally from a specific sender or app. Only reads what is currently in the notification shade.",
            params = listOf(
                ToolParam("from", "string", "Sender name to look for."),
                ToolParam("app", "string", "App name, e.g. WhatsApp."),
                ToolParam("again", "boolean", "True to repeat the message that was just read."),
            ),
            privateOutput = true,
            verification = "Reads directly from the notification; nothing is stored.",
        ),
        run = { args, _ -> readMessage(args) },
    )

    private suspend fun readMessage(args: Args): ToolResult {
        val again = session.lastSpokenMessage
        if (args.bool("again") == true && again != null) return ToolResult.ok(again)
        val shade = shade()
        val items = shade.items ?: return shade.problem!!
        val prefs = settings.notificationPrefs
        val from = args.string("from")?.let { memory.resolveAlias(it.removePrefix("my ")) ?: it }
        val item = NotificationIntel.latestMessage(items, prefs, from, args.string("app"))
            ?: return ToolResult.ok(if (from != null) "I don't see a message from $from in your notifications." else "I don't see any message notifications right now.")
        session.lastNotificationKey = item.key
        val spoken = NotificationIntel.speak(item, prefs)
        session.lastSpokenMessage = spoken
        return ToolResult(true, if (item.canReply) "$spoken Say \"reply\" followed by your message to answer." else spoken, verified = true)
    }

    private val notifications: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.NOTIFICATIONS,
            description = "Read, summarise, or list the important ones among the notifications currently showing. Only on request.",
            params = listOf(
                ToolParam("mode", "string", "What to do.", enumValues = listOf("read", "summary", "important")),
                ToolParam("app", "string", "Limit to one app by name."),
            ),
            privateOutput = true,
        ),
        run = { args, _ -> readNotifications(args) },
    )

    private fun readNotifications(args: Args): ToolResult {
        val shade = shade()
        val all = shade.items ?: return shade.problem!!
        val prefs = settings.notificationPrefs
        val app = args.string("app")
        val items = if (app == null) all else all.filter { it.appLabel.contains(app, ignoreCase = true) }
        return when (args.string("mode") ?: "read") {
            "summary" -> ToolResult.ok(NotificationIntel.summary(items, prefs))
            "important" -> {
                val important = items.filter { NotificationIntel.isImportant(it) }
                if (important.isEmpty()) ToolResult.ok("Nothing looks important right now. You have ${items.size} ${if (items.size == 1) "notification" else "notifications"} in total.")
                else ToolResult.ok(important.take(MAX_READ).joinToString(" ") { NotificationIntel.speak(it, prefs) })
            }
            else -> {
                if (items.isEmpty()) {
                    ToolResult.ok(if (app != null) "You have no notifications from $app." else "You have no notifications.")
                } else {
                    items.firstOrNull { it.isMessage }?.let { session.lastNotificationKey = it.key }
                    val shown = items.take(MAX_READ)
                    val more = if (items.size > shown.size) " And ${items.size - shown.size} more." else ""
                    ToolResult.ok(shown.joinToString(" ") { NotificationIntel.speak(it, prefs) } + more)
                }
            }
        }
    }

    private val ignoreApp: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.IGNORE_APP,
            description = "Stop reading notifications from an app. Use app='current' for the app of the notification that was just read.",
            params = listOf(ToolParam("app", "string", "App name, or 'current'.", required = true, prompt = "Which app should I ignore?")),
            verification = "Confirms the app was added to the ignore list.",
        ),
        run = { args, _ ->
            val spoken = args.string("app").orEmpty()
            val items = LumiNotificationListener.instance?.items().orEmpty()
            val hit: AppHit? = if (spoken == "current") {
                items.firstOrNull { it.key == session.lastNotificationKey }?.let { AppHit(it.packageName, it.appLabel) }
            } else {
                items.firstOrNull { it.appLabel.contains(spoken, ignoreCase = true) }?.let { AppHit(it.packageName, it.appLabel) } ?: s.findApp(spoken)
            }
            if (hit == null) {
                ToolResult.fail(if (spoken == "current") "I'm not sure which app you mean. Tell me its name." else "I couldn't find an app called $spoken.")
            } else {
                val prefs = settings.notificationPrefs
                settings.notificationPrefs = prefs.copy(ignoredApps = prefs.ignoredApps + hit.packageName, selectedApps = prefs.selectedApps - hit.packageName)
                ToolResult.ok("Okay, I'll ignore notifications from ${hit.label}. You can undo this in Notification settings.", verified = true)
            }
        },
    )

    private val pauseNotifications: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.PAUSE_NOTIFICATIONS,
            description = "Pause or resume Lumi's notification reading and announcements.",
            params = listOf(ToolParam("paused", "boolean", "True to pause, false to resume.")),
            allowedWhenLocked = true,
        ),
        run = { args, _ ->
            val paused = args.bool("paused") ?: true
            settings.notificationPrefs = settings.notificationPrefs.copy(paused = paused)
            ToolResult.ok(if (paused) "Notification reading is paused." else "Notification reading is back on.", verified = true)
        },
    )

    // ------------------------------------------------------------------ message delivery

    /** Delivery side of the dictation workflow. */
    val messaging: MessagingBackend = object : MessagingBackend {
        override suspend fun resolveRecipient(name: String, app: String?): Recipient = when (val r = findContact(name)) {
            is ContactResult.Found -> Recipient.Found(r.hit.name, r.hit.number, app)
            is ContactResult.Many -> Recipient.Ambiguous(r.hits.map { it.name })
            ContactResult.None -> if (s.digits(name).length >= 7) Recipient.Found(name, s.digits(name), app) else Recipient.NotFound
            ContactResult.NoPermission -> Recipient.Unavailable("I need the contacts permission to find who to message. You can grant it in Lumi's Permissions screen.", "permissions")
        }

        override suspend fun replyTarget(key: String?): ReplyTarget? {
            val listener = LumiNotificationListener.instance ?: return null
            val prefs = settings.notificationPrefs
            if (!prefs.enabled) return null
            val items = NotificationIntel.usable(listener.items(), prefs).filter { it.canReply }
            val item = items.firstOrNull { it.key == key } ?: items.firstOrNull { it.isMessage } ?: return null
            return ReplyTarget(item.key, item.title.ifBlank { item.appLabel }, item.appLabel)
        }

        override suspend fun send(draft: Draft): ToolResult = when (draft.kind) {
            Draft.Kind.REPLY -> sendReply(draft)
            Draft.Kind.MESSAGE -> sendMessage(draft)
            Draft.Kind.EMAIL -> sendEmail(draft)
            Draft.Kind.TYPE -> typeDraft(draft)
        }
    }

    private fun sendReply(draft: Draft): ToolResult {
        val key = draft.replyKey
        val listener = LumiNotificationListener.instance
        return if (key != null && listener != null && listener.reply(key, draft.text)) {
            ToolResult(true, "Sent your reply to ${draft.recipientName ?: "them"} through ${draft.app ?: "the app"}'s notification.", verified = false)
        } else {
            ToolResult.fail("That notification is gone or no longer accepts replies, so nothing was sent. Open the app to reply there.")
        }
    }

    private fun sendEmail(draft: Draft): ToolResult {
        val intent = Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:" + Uri.encode(draft.recipientAddress.orEmpty())))
            .putExtra(Intent.EXTRA_SUBJECT, draft.subject.orEmpty())
            .putExtra(Intent.EXTRA_TEXT, draft.text)
        if (draft.recipientAddress != null) intent.putExtra(Intent.EXTRA_EMAIL, arrayOf(draft.recipientAddress))
        return if (s.launch(intent)) {
            val note = if (draft.recipientAddress == null) " I only have a name, so add the address there." else ""
            ToolResult(true, "I've put the email into your mail app.$note Review it and tap send.", verified = false, directive = Directive.Dismiss)
        } else {
            ToolResult.fail("I couldn't find an email app to hand this to.")
        }
    }

    private fun typeDraft(draft: Draft): ToolResult {
        val service = LumiAccessibilityService.instance
        return when {
            !settings.screenAwareness || service == null ->
                ToolResult.fail("Typing into other apps needs screen awareness, which is off. You can enable it in Lumi's Permissions screen.", suggestScreen = "permissions")
            service.setText(draft.text, append = true) -> ToolResult.ok("Typed.", verified = true)
            else -> ToolResult.fail("I couldn't find a text field to type into. Tap the field first, then ask again.")
        }
    }

    private suspend fun sendMessage(draft: Draft): ToolResult {
        val number = draft.recipientAddress ?: return ToolResult.fail("I don't have a number for ${draft.recipientName ?: "that contact"}.")
        val name = draft.recipientName ?: number
        if (draft.app.equals("whatsapp", ignoreCase = true)) {
            val uri = Uri.parse("https://wa.me/${s.digits(number).removePrefix("+")}?text=${Uri.encode(draft.text)}")
            return if (s.launch(Intent(Intent.ACTION_VIEW, uri).setPackage(WHATSAPP))) {
                ToolResult(true, "I've opened WhatsApp with your message to $name filled in. Tap send to deliver it; WhatsApp doesn't let other apps send for you.", verified = false, directive = Directive.Dismiss)
            } else {
                ToolResult.fail("WhatsApp doesn't seem to be installed, so nothing was sent.")
            }
        }
        if (s.has(Manifest.permission.SEND_SMS)) {
            val error = sendSms(s.digits(number), draft.text)
            return if (error == null) ToolResult.ok("Sent your text to $name.", verified = true) else ToolResult.fail("The text to $name wasn't sent: $error.")
        }
        val intent = Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(s.digits(number)))).putExtra("sms_body", draft.text)
        return if (s.launch(intent)) {
            ToolResult(true, "I've opened your messaging app with the text to $name filled in. Tap send to deliver it. To let me send texts myself, grant the SMS permission.", verified = false, directive = Directive.Dismiss, suggestScreen = "permissions")
        } else {
            ToolResult.fail("I couldn't find a messaging app to hand this to.")
        }
    }

    /** Sends an SMS and waits for the radio's verdict. Returns null on success, otherwise the reason. */
    private suspend fun sendSms(number: String, text: String): String? {
        val action = "com.lumi.assistant.SMS_SENT." + System.nanoTime()
        val app = context.applicationContext
        return withTimeoutOrNull(20_000) {
            suspendCancellableCoroutine<String?> { cont ->
                val receiver = object : BroadcastReceiver() {
                    override fun onReceive(c: Context?, intent: Intent?) {
                        try { app.unregisterReceiver(this) } catch (e: Exception) { /* already gone */ }
                        if (cont.isActive) cont.resume(if (resultCode == Activity.RESULT_OK) null else "the network rejected it (code $resultCode)")
                    }
                }
                try {
                    if (Build.VERSION.SDK_INT >= 33) {
                        app.registerReceiver(receiver, IntentFilter(action), Context.RECEIVER_NOT_EXPORTED)
                    } else {
                        app.registerReceiver(receiver, IntentFilter(action))
                    }
                    cont.invokeOnCancellation { try { app.unregisterReceiver(receiver) } catch (e: Exception) { /* already gone */ } }
                    @Suppress("DEPRECATION")
                    val sms: SmsManager = if (Build.VERSION.SDK_INT >= 31) app.getSystemService(SmsManager::class.java) else SmsManager.getDefault()
                    val parts = sms.divideMessage(text)
                    val sent = PendingIntent.getBroadcast(app, 0, Intent(action).setPackage(app.packageName), PendingIntent.FLAG_ONE_SHOT or PendingIntent.FLAG_IMMUTABLE)
                    // Only the last part reports back, so one broadcast means the whole message went out.
                    val intents = ArrayList<PendingIntent?>(parts.size)
                    for (i in parts.indices) intents.add(if (i == parts.lastIndex) sent else null)
                    sms.sendMultipartTextMessage(number, null, parts, intents, null)
                } catch (e: Exception) {
                    try { app.unregisterReceiver(receiver) } catch (ignored: Exception) { /* already gone */ }
                    if (cont.isActive) cont.resume(e.message ?: e.javaClass.simpleName)
                }
            }
        } ?: "the phone didn't confirm it was sent in time"
    }

    companion object {
        const val WHATSAPP = "com.whatsapp"
        const val MAX_READ = 5
    }
}
