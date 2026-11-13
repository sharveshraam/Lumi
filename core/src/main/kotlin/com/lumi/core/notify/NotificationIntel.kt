package com.lumi.core.notify

/** A notification as seen by the listener, reduced to what Lumi needs. Never persisted. */
data class NotificationItem(
    val key: String,
    val packageName: String,
    val appLabel: String,
    val title: String,
    val text: String,
    val postedAt: Long,
    val isMessage: Boolean = false,
    val isOngoing: Boolean = false,
    val canReply: Boolean = false,
    val highPriority: Boolean = false,
    val isGroupSummary: Boolean = false,
)

/** The user's notification choices. Everything is off until the user opts in. */
data class NotificationPrefs(
    val enabled: Boolean = false,
    val paused: Boolean = false,
    /** Packages Lumi may read. Empty means every app that is not ignored. */
    val selectedApps: Set<String> = emptySet(),
    val ignoredApps: Set<String> = emptySet(),
    /** Minutes after midnight; equal values mean no quiet hours. */
    val quietStart: Int = 0,
    val quietEnd: Int = 0,
    /** Speak important notifications as they arrive. Off by default. */
    val announceImportant: Boolean = false,
    /** Never speak one-time codes, passwords or banking details. */
    val hideSensitive: Boolean = true,
)

/** Local, deterministic notification filtering. Nothing here calls a network service. */
object NotificationIntel {
    private val sensitive = Regex(
        "\\b(otp|one[- ]time (?:pass(?:word|code)|code)|verification code|security code|auth(?:entication)? code|passcode|password|cvv|pin\\b|" +
            "debited|credited|a/c|acct|account balance|available balance|card ending|upi|transaction of|login code|2fa)\\b|\\b\\d{4,8} is your\\b|\\bcode(?: is)?:? ?\\d{4,8}\\b",
        RegexOption.IGNORE_CASE,
    )
    private val urgent = Regex("\\b(urgent|asap|emergency|important|immediately|right now|call me)\\b", RegexOption.IGNORE_CASE)

    fun isSensitive(item: NotificationItem): Boolean = sensitive.containsMatchIn(item.title + " " + item.text)

    /** Whether Lumi may use this notification at all. */
    fun allowed(item: NotificationItem, prefs: NotificationPrefs): Boolean {
        if (!prefs.enabled) return false
        if (item.isOngoing || item.isGroupSummary) return false
        if (item.title.isBlank() && item.text.isBlank()) return false
        if (item.packageName in prefs.ignoredApps) return false
        return prefs.selectedApps.isEmpty() || item.packageName in prefs.selectedApps
    }

    fun inQuietHours(prefs: NotificationPrefs, minuteOfDay: Int): Boolean {
        val s = prefs.quietStart
        val e = prefs.quietEnd
        if (s == e) return false
        return if (s < e) minuteOfDay in s until e else minuteOfDay >= s || minuteOfDay < e
    }

    fun isImportant(item: NotificationItem): Boolean =
        item.highPriority || urgent.containsMatchIn(item.title + " " + item.text)

    /** Whether to speak this notification unprompted. Requires several explicit opt-ins. */
    fun shouldAnnounce(item: NotificationItem, prefs: NotificationPrefs, minuteOfDay: Int): Boolean =
        prefs.announceImportant && !prefs.paused && allowed(item, prefs) && !inQuietHours(prefs, minuteOfDay) &&
            isImportant(item) && !(prefs.hideSensitive && isSensitive(item))

    fun usable(items: List<NotificationItem>, prefs: NotificationPrefs): List<NotificationItem> =
        items.filter { allowed(it, prefs) }.sortedByDescending { it.postedAt }

    /** One notification as a spoken sentence, with sensitive content withheld. */
    fun speak(item: NotificationItem, prefs: NotificationPrefs): String {
        if (prefs.hideSensitive && isSensitive(item)) {
            return "${item.appLabel}: a notification with sensitive details, which I won't read aloud."
        }
        val body = item.text.replace(Regex("\\s+"), " ").trim().take(300)
        val who = item.title.trim()
        return when {
            item.isMessage && who.isNotEmpty() -> "$who on ${item.appLabel}: $body"
            who.isNotEmpty() && body.isNotEmpty() -> "${item.appLabel}, $who: $body"
            else -> "${item.appLabel}: ${who.ifEmpty { body }}"
        }.trim().let { if (it.last() in ".!?") it else "$it." }
    }

    /** "5 notifications: 3 from WhatsApp, 2 from Gmail." */
    fun summary(items: List<NotificationItem>, prefs: NotificationPrefs): String {
        val list = usable(items, prefs)
        if (list.isEmpty()) return "You have no notifications."
        val byApp = list.groupBy { it.appLabel }.entries.sortedByDescending { it.value.size }
        val parts = byApp.take(5).map { (app, n) ->
            val senders = n.filter { it.isMessage }.map { it.title.trim() }.filter { it.isNotEmpty() }.distinct()
            if (senders.isNotEmpty() && senders.size <= 3) "${n.size} from $app (${senders.joinToString(", ")})" else "${n.size} from $app"
        }
        val more = if (byApp.size > 5) ", and others" else ""
        val important = list.count { isImportant(it) }
        val tail = if (important > 0) " $important ${if (important == 1) "looks" else "look"} important." else ""
        return "You have ${list.size} ${if (list.size == 1) "notification" else "notifications"}: ${parts.joinToString(", ")}$more.$tail"
    }

    /** The newest message notification, optionally narrowed by sender name or app label. */
    fun latestMessage(items: List<NotificationItem>, prefs: NotificationPrefs, from: String? = null, app: String? = null): NotificationItem? =
        usable(items, prefs).filter { it.isMessage }
            .filter { from == null || it.title.contains(from.trim(), ignoreCase = true) }
            .firstOrNull { app == null || it.appLabel.contains(app.trim(), ignoreCase = true) || it.packageName.contains(app.trim().replace(" ", ""), ignoreCase = true) }
}
