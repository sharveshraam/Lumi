package com.lumi.assistant.ui

import android.Manifest
import android.app.AlarmManager
import android.app.role.RoleManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings as SystemSettings
import com.lumi.assistant.Graph
import com.lumi.assistant.services.LumiAccessibilityService
import com.lumi.assistant.services.LumiNotificationListener
import com.lumi.assistant.tools.AndroidPermissions
import com.lumi.core.model.Reminder
import com.lumi.core.model.Task
import com.lumi.core.model.now
import com.lumi.core.model.toLocal
import com.lumi.core.time.Recurrence
import com.lumi.core.time.TimeFormat

/** One row of the Permissions screen. Everything here is optional; Lumi degrades gracefully. */
data class PermissionItem(
    val id: String,
    val title: String,
    /** What Lumi does with it, in plain words. */
    val why: String,
    val granted: Boolean,
    /** Runtime permissions to request; empty when [settingsIntent] is used instead. */
    val runtime: List<String> = emptyList(),
    val settingsIntent: Intent? = null,
    val core: Boolean = false,
)

object PermissionCatalog {
    fun isDefaultAssistant(context: Context): Boolean {
        if (Build.VERSION.SDK_INT >= 29) {
            try {
                val roles = context.getSystemService(RoleManager::class.java)
                if (roles != null && roles.isRoleAvailable(RoleManager.ROLE_ASSISTANT)) return roles.isRoleHeld(RoleManager.ROLE_ASSISTANT)
            } catch (e: Exception) {
                // Fall through to the settings lookup.
            }
        }
        return try {
            SystemSettings.Secure.getString(context.contentResolver, "assistant").orEmpty().startsWith(context.packageName + "/")
        } catch (e: Exception) {
            false
        }
    }

    fun assistantSettingsIntent(): Intent = Intent(SystemSettings.ACTION_VOICE_INPUT_SETTINGS)

    private fun appDetails(context: Context) = Intent(SystemSettings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", context.packageName, null))

    fun items(context: Context, graph: Graph): List<PermissionItem> {
        val p = graph.permissions
        val out = ArrayList<PermissionItem>()
        out += PermissionItem(
            "assistant", "Default digital assistant",
            "Lets you start Lumi by holding the power or home button, or with a headset button. Lumi has no wake word and never listens in the background.",
            isDefaultAssistant(context), settingsIntent = assistantSettingsIntent(), core = true,
        )
        out += PermissionItem(
            "mic", "Microphone",
            "Used only while the Lumi panel is open and shows \"Listening\".",
            p.granted(Manifest.permission.RECORD_AUDIO), runtime = listOf(Manifest.permission.RECORD_AUDIO), core = true,
        )
        if (Build.VERSION.SDK_INT >= 33) {
            out += PermissionItem(
                "notify", "Show notifications",
                "Needed to deliver your reminders and the daily briefing.",
                graph.notifier.canNotify(), runtime = listOf(Manifest.permission.POST_NOTIFICATIONS), core = true,
            )
        }
        if (Build.VERSION.SDK_INT >= 31) {
            out += PermissionItem(
                "exact", "Alarms and reminders",
                "Lets reminders fire at the exact minute. Without it they can arrive a few minutes late.",
                graph.scheduler.canScheduleExact(),
                settingsIntent = Intent(SystemSettings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM, Uri.fromParts("package", context.packageName, null)), core = true,
            )
        }
        out += PermissionItem(
            "contacts", "Contacts",
            "To find who you mean when you say \"call Dad\" or \"message Priya\". Contacts stay on the phone.",
            p.granted(Manifest.permission.READ_CONTACTS), runtime = listOf(Manifest.permission.READ_CONTACTS),
        )
        out += PermissionItem(
            "phone", "Phone calls",
            "To place calls you ask for. Without it Lumi opens the dialer and you tap call.",
            p.granted(Manifest.permission.CALL_PHONE), runtime = listOf(Manifest.permission.CALL_PHONE),
        )
        out += PermissionItem(
            "calllog", "Call log",
            "Only for \"call back the last caller\".",
            p.granted(Manifest.permission.READ_CALL_LOG), runtime = listOf(Manifest.permission.READ_CALL_LOG),
        )
        out += PermissionItem(
            "sms", "Send SMS",
            "To send a text after you approve the draft. Without it Lumi opens your messaging app with the text filled in.",
            p.granted(Manifest.permission.SEND_SMS), runtime = listOf(Manifest.permission.SEND_SMS),
        )
        out += PermissionItem(
            "calendar", "Calendar",
            "To read today's events for the briefing and \"what's on my calendar\".",
            p.granted(Manifest.permission.READ_CALENDAR), runtime = listOf(Manifest.permission.READ_CALENDAR),
        )
        out += PermissionItem(
            "location", "Location",
            "To save a place (\"remember this place as college\"). Used once, when you ask.",
            graph.geofences.hasForegroundLocation(),
            runtime = listOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION),
        )
        if (Build.VERSION.SDK_INT >= 29) {
            out += PermissionItem(
                "bglocation", "Location: allow all the time",
                "Required by Android for reminders that trigger when you arrive at or leave a saved place. Android does the detection; Lumi does not track you. Grant Location first.",
                graph.geofences.hasBackgroundLocation(),
                runtime = if (Build.VERSION.SDK_INT == 29) listOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION) else emptyList(),
                settingsIntent = if (Build.VERSION.SDK_INT == 29) null else appDetails(context),
            )
        }
        out += PermissionItem(
            "listener", "Notification access",
            "To read or summarise notifications and send quick replies when you ask. Nothing is stored or uploaded.",
            p.granted(AndroidPermissions.NOTIFICATION_ACCESS), settingsIntent = Intent(SystemSettings.ACTION_NOTIFICATION_LISTENER_SETTINGS),
        )
        out += PermissionItem(
            "accessibility", "Screen awareness (accessibility)",
            "To read, scroll, tap or type in the app on screen when you ask. Lumi never watches the screen on its own and skips password fields.",
            p.granted(AndroidPermissions.ACCESSIBILITY), settingsIntent = Intent(SystemSettings.ACTION_ACCESSIBILITY_SETTINGS),
        )
        val power = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        out += PermissionItem(
            "battery", "Unrestricted battery (optional)",
            "Only needed if your phone's battery manager delays reminders. Lumi does no background work either way; this just stops the phone from blocking its alarms.",
            power.isIgnoringBatteryOptimizations(context.packageName), settingsIntent = appDetails(context),
        )
        return out
    }
}

/** Plain-text status lines for the Diagnostics screen. */
object Diagnostics {
    data class Line(val label: String, val value: String, val ok: Boolean)

    suspend fun lines(context: Context, graph: Graph): List<Line> {
        val out = ArrayList<Line>()
        val assistant = PermissionCatalog.isDefaultAssistant(context)
        out += Line("Default assistant", if (assistant) "Lumi" else "Another app", assistant)
        val mic = graph.permissions.granted(Manifest.permission.RECORD_AUDIO)
        out += Line("Microphone", if (mic) "Allowed" else "Not allowed", mic)
        val stt = com.lumi.assistant.voice.AndroidSpeechToText.isAvailable(context)
        out += Line("Speech recognition", if (stt) "Available" else "No recognition service on this phone", stt)
        val ttsProblem = graph.speech.problem
        out += Line("Speech output", ttsProblem ?: "No problem reported", ttsProblem == null)
        val notify = graph.notifier.canNotify()
        out += Line("Reminder notifications", if (notify) "Allowed" else "Blocked: reminders cannot be shown", notify)
        val exact = graph.scheduler.canScheduleExact()
        out += Line("Exact alarms", if (exact) "Allowed" else "Not allowed: reminders may be late", exact)
        val scheduled = graph.tasks.scheduledReminders()
        val timed = scheduled.count { !it.isLocationBased }
        out += Line("Scheduled reminders", "$timed timed, ${scheduled.size - timed} at places", true)
        val inexact = scheduled.count { !it.isLocationBased && !it.exact }
        if (inexact > 0) out += Line("Inexact reminders", "$inexact may be delivered late", false)
        val alarms = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val next = alarms.nextAlarmClock
        if (next != null) out += Line("Next system alarm clock", TimeFormat.dateTime(graph.time.toLocal(next.triggerTime), graph.time.now()), true)
        val listenerOn = LumiNotificationListener.isEnabled(context)
        out += Line("Notification access", if (!listenerOn) "Off" else if (LumiNotificationListener.instance != null) "Connected" else "Granted, waiting for Android to connect", listenerOn)
        val a11y = LumiAccessibilityService.isEnabled(context)
        out += Line("Screen awareness", if (!graph.settings.screenAwareness) "Off in Lumi" else if (a11y) "On" else "On in Lumi, service not enabled", a11y || !graph.settings.screenAwareness)
        val bg = graph.geofences.hasBackgroundLocation()
        out += Line("Place reminders", if (bg) "Ready" else "Need \"allow all the time\" location", bg)
        val ai = graph.settings.aiConfig()
        out += Line("AI provider", if (ai == null) "Not configured (offline commands only)" else "${ai.model} via ${Uri.parse(ai.baseUrl).host ?: ai.baseUrl}", true)
        val power = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        out += Line("Battery optimisation", if (power.isIgnoringBatteryOptimizations(context.packageName)) "Unrestricted" else "Optimised (default)", true)
        out += Line("Phone", "${Build.MANUFACTURER} ${Build.MODEL}, Android ${Build.VERSION.RELEASE}", true)
        return out
    }
}

/** Display helpers shared by the screens. */
object Describe {
    fun due(graph: Graph, task: Task): String {
        val parts = ArrayList<String>()
        task.dueAt?.let { parts += TimeFormat.dateTime(graph.time.toLocal(it), graph.time.now()) }
        Recurrence.decode(task.recurrence)?.let { parts += it.describe() }
        if (task.priority == com.lumi.core.model.Priority.HIGH) parts += "important"
        return parts.joinToString(" · ")
    }

    fun reminder(graph: Graph, reminder: Reminder): String = when {
        reminder.isLocationBased -> (if (reminder.transition == com.lumi.core.model.PlaceTransition.EXIT) "When leaving " else "When arriving at ") + reminder.placeName
        reminder.triggerAt != null -> TimeFormat.dateTime(graph.time.toLocal(reminder.triggerAt!!), graph.time.now()) + if (reminder.exact) "" else " (may be late)"
        else -> "No time set"
    }

    fun clock(minuteOfDay: Int): String = TimeFormat.time(java.time.LocalTime.of((minuteOfDay / 60).coerceIn(0, 23), minuteOfDay % 60))
}
