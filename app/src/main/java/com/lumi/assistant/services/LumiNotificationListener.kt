package com.lumi.assistant.services

import android.app.Notification
import android.app.RemoteInput
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.provider.Settings as SystemSettings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import com.lumi.assistant.LumiApp
import com.lumi.core.notify.NotificationIntel
import com.lumi.core.notify.NotificationItem
import java.time.LocalTime

/**
 * Opt-in notification access. Lumi keeps no copy of notifications: it asks Android for the
 * currently showing ones only when the user asks, and all filtering happens on the device.
 * Nothing is read aloud automatically unless the user enables important announcements.
 */
class LumiNotificationListener : NotificationListenerService() {

    override fun onListenerConnected() {
        instance = this
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null || sbn.packageName == packageName) return
        val graph = LumiApp.graph(this)
        val prefs = graph.settings.notificationPrefs
        // Cheap exit: by default nothing below runs.
        if (!prefs.enabled || !prefs.announceImportant || prefs.paused) return
        val item = toItem(this, sbn) ?: return
        val now = LocalTime.now()
        if (!NotificationIntel.shouldAnnounce(item, prefs, now.hour * 60 + now.minute)) return
        if (announced.put(item.key, item.postedAt) == item.postedAt) return
        if (announced.size > 100) announced.clear()
        graph.speech.speak(NotificationIntel.speak(item, prefs))
    }

    fun items(): List<NotificationItem> = try {
        activeNotifications.orEmpty().filter { it.packageName != packageName }.mapNotNull { toItem(this, it) }
    } catch (e: Exception) {
        emptyList()
    }

    /** Sends [text] through the notification's own reply action. Returns false if it has none. */
    fun reply(key: String, text: String): Boolean {
        val sbn = try {
            activeNotifications.orEmpty().firstOrNull { it.key == key }
        } catch (e: Exception) {
            null
        } ?: return false
        val action = replyAction(sbn.notification) ?: return false
        val inputs = action.remoteInputs ?: return false
        val intent = Intent()
        val results = Bundle()
        for (input in inputs) results.putCharSequence(input.resultKey, text)
        RemoteInput.addResultsToIntent(inputs, intent, results)
        return try {
            action.actionIntent.send(this, 0, intent)
            true
        } catch (e: Exception) {
            false
        }
    }

    companion object {
        @Volatile
        var instance: LumiNotificationListener? = null
            private set

        private val announced = HashMap<String, Long>()

        fun isEnabled(context: Context): Boolean {
            val flat = SystemSettings.Secure.getString(context.contentResolver, "enabled_notification_listeners") ?: return false
            val me = ComponentName(context, LumiNotificationListener::class.java)
            return flat.split(":").any { ComponentName.unflattenFromString(it) == me }
        }

        private fun replyAction(n: Notification): Notification.Action? =
            n.actions?.firstOrNull { a -> a.remoteInputs?.any { it.allowFreeFormInput } == true }

        private val highCategories = setOf(Notification.CATEGORY_CALL, Notification.CATEGORY_ALARM, Notification.CATEGORY_REMINDER, Notification.CATEGORY_EVENT)

        fun toItem(context: Context, sbn: StatusBarNotification): NotificationItem? {
            val n = sbn.notification ?: return null
            val extras = n.extras ?: return null
            var title = (extras.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE) ?: extras.getCharSequence(Notification.EXTRA_TITLE))?.toString().orEmpty()
            var text = (extras.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: extras.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty()
            // MessagingStyle keeps the newest message last; prefer it over a collapsed summary line.
            @Suppress("DEPRECATION")
            val messages = extras.getParcelableArray(Notification.EXTRA_MESSAGES)
            val isMessaging = messages != null && messages.isNotEmpty()
            if (isMessaging) {
                val last = messages!!.last() as? Bundle
                last?.getCharSequence("text")?.toString()?.takeIf { it.isNotBlank() }?.let { text = it }
                if (title.isBlank()) last?.getCharSequence("sender")?.toString()?.let { title = it }
            }
            val reply = replyAction(n) != null
            val label = try {
                val pm = context.packageManager
                pm.getApplicationLabel(pm.getApplicationInfo(sbn.packageName, 0)).toString()
            } catch (e: Exception) {
                sbn.packageName
            }
            return NotificationItem(
                key = sbn.key,
                packageName = sbn.packageName,
                appLabel = label,
                title = title.trim(),
                text = text.trim(),
                postedAt = sbn.postTime,
                isMessage = isMessaging || n.category == Notification.CATEGORY_MESSAGE || reply,
                isOngoing = sbn.isOngoing,
                canReply = reply,
                highPriority = n.category in highCategories,
                isGroupSummary = (n.flags and Notification.FLAG_GROUP_SUMMARY) != 0,
            )
        }
    }
}
