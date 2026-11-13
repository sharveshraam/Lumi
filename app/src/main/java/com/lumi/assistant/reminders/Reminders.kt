package com.lumi.assistant.reminders

import android.Manifest
import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.drawable.Icon
import android.net.Uri
import android.os.Build
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ListenableWorker
import androidx.work.PeriodicWorkRequest
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import com.lumi.assistant.LumiApp
import com.lumi.assistant.R
import com.lumi.assistant.data.DebugLog
import com.lumi.assistant.ui.MainActivity
import com.lumi.core.model.Place
import com.lumi.core.model.Reminder
import com.lumi.core.model.toLocal
import com.lumi.core.tasks.FiredReminder
import com.lumi.core.tasks.ReminderScheduler
import com.lumi.core.tasks.ScheduleOutcome
import com.lumi.core.time.TimeFormat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import java.util.concurrent.TimeUnit

/** Posts reminder and briefing notifications. */
class Notifier(private val context: Context) {
    private val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    fun createChannels() {
        val reminders = NotificationChannel(CH_REMINDERS, context.getString(R.string.channel_reminders), NotificationManager.IMPORTANCE_HIGH).apply {
            description = context.getString(R.string.channel_reminders_desc)
            enableVibration(true)
            lockscreenVisibility = Notification.VISIBILITY_PRIVATE
        }
        val briefing = NotificationChannel(CH_BRIEFING, context.getString(R.string.channel_briefing), NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = context.getString(R.string.channel_briefing_desc)
            lockscreenVisibility = Notification.VISIBILITY_PRIVATE
        }
        manager.createNotificationChannels(listOf(reminders, briefing))
    }

    /** True when Android will actually show Lumi's reminder notifications. */
    fun canNotify(): Boolean {
        if (!manager.areNotificationsEnabled()) return false
        if (Build.VERSION.SDK_INT >= 33 && context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return false
        return manager.getNotificationChannel(CH_REMINDERS)?.importance != NotificationManager.IMPORTANCE_NONE
    }

    private fun action(action: String, fired: FiredReminder, code: Int): PendingIntent {
        val intent = Intent(context, ReminderReceiver::class.java)
            .setAction(action)
            .setData(Uri.parse("lumi://reminder/${fired.reminder.id}/$action"))
            .putExtra(EXTRA_REMINDER_ID, fired.reminder.id)
            .putExtra(EXTRA_TASK_ID, fired.task.id)
        return PendingIntent.getBroadcast(context, code, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    /** Shows a reminder. Returns false when notifications are blocked, so callers can log the truth. */
    fun showReminder(fired: FiredReminder): Boolean {
        val task = fired.task
        val prefix = when {
            fired.missed -> context.getString(R.string.reminder_missed)
            fired.nag -> context.getString(R.string.reminder_again)
            else -> context.getString(R.string.reminder_title)
        }
        val details = buildString {
            if (task.description.isNotBlank()) append(task.description)
            val open = task.subtasks.filter { !it.done }
            if (open.isNotEmpty()) {
                if (isNotEmpty()) append('\n')
                append(open.joinToString("\n") { "• ${it.title}" })
            }
            if (fired.reminder.placeName != null) {
                if (isNotEmpty()) append('\n')
                append(context.getString(R.string.reminder_at_place, fired.reminder.placeName))
            }
        }
        val icon = Icon.createWithResource(context, R.drawable.ic_lumi)
        val open = PendingIntent.getActivity(
            context,
            notificationId(task.id),
            Intent(context, MainActivity::class.java).putExtra(MainActivity.EXTRA_SCREEN, "tasks").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val id = notificationId(task.id)
        val builder = Notification.Builder(context, CH_REMINDERS)
            .setSmallIcon(R.drawable.ic_lumi)
            .setContentTitle(task.title)
            .setContentText(if (details.isBlank()) prefix else details.lineSequence().first())
            .setSubText(prefix)
            .setCategory(Notification.CATEGORY_REMINDER)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setAutoCancel(true)
            .setShowWhen(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(icon, context.getString(R.string.action_done), action(ACTION_COMPLETE, fired, id)).build())
            .addAction(Notification.Action.Builder(icon, context.getString(R.string.action_snooze), action(ACTION_SNOOZE, fired, id)).build())
            .addAction(Notification.Action.Builder(icon, context.getString(R.string.action_postpone), action(ACTION_POSTPONE, fired, id)).build())
        if (details.isNotBlank()) builder.setStyle(Notification.BigTextStyle().bigText(details))
        return post(id, builder.build())
    }

    fun showBriefing(text: String): Boolean {
        val open = PendingIntent.getActivity(
            context,
            BRIEFING_ID,
            Intent(context, com.lumi.assistant.assistant.AssistActivity::class.java)
                .setAction(com.lumi.assistant.assistant.AssistActivity.ACTION_BRIEFING)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val n = Notification.Builder(context, CH_BRIEFING)
            .setSmallIcon(R.drawable.ic_lumi)
            .setContentTitle(context.getString(R.string.briefing_title))
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        return post(BRIEFING_ID, n)
    }

    /** Short status message after a notification action, e.g. "Snoozed until 5:10 PM". */
    fun showUpdate(taskId: Long, title: String, text: String) {
        val n = Notification.Builder(context, CH_BRIEFING)
            .setSmallIcon(R.drawable.ic_lumi)
            .setContentTitle(title)
            .setContentText(text)
            .setAutoCancel(true)
            .setTimeoutAfter(8_000)
            .build()
        post(notificationId(taskId), n)
    }

    private fun post(id: Int, notification: Notification): Boolean {
        if (!canNotify()) return false
        return try {
            manager.notify(id, notification)
            true
        } catch (e: SecurityException) {
            false
        }
    }

    fun cancel(taskId: Long) = manager.cancel(notificationId(taskId))

    companion object {
        const val CH_REMINDERS = "reminders"
        const val CH_BRIEFING = "briefing"
        const val BRIEFING_ID = 900_001
        const val ACTION_FIRE = "com.lumi.assistant.action.REMINDER_FIRE"
        const val ACTION_COMPLETE = "com.lumi.assistant.action.REMINDER_COMPLETE"
        const val ACTION_SNOOZE = "com.lumi.assistant.action.REMINDER_SNOOZE"
        const val ACTION_POSTPONE = "com.lumi.assistant.action.REMINDER_POSTPONE"
        const val ACTION_BRIEFING = "com.lumi.assistant.action.BRIEFING"
        const val EXTRA_REMINDER_ID = "reminder_id"
        const val EXTRA_TASK_ID = "task_id"
        const val SNOOZE_MINUTES = 10L
        const val POSTPONE_MINUTES = 60L

        fun notificationId(taskId: Long): Int = (taskId % 800_000).toInt() + 1000
    }
}

/**
 * Hands reminders to AlarmManager. Uses exact, Doze-proof alarms when Android allows them and
 * falls back to an inexact while-idle alarm otherwise. The returned outcome always reflects what
 * was really registered.
 */
class AlarmScheduler(
    private val context: Context,
    private val geofences: GeofenceManager,
    private val log: DebugLog,
) : ReminderScheduler {
    private val alarms = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager

    private fun pending(reminderId: Long, flags: Int): PendingIntent? {
        val intent = Intent(context, ReminderReceiver::class.java)
            .setAction(Notifier.ACTION_FIRE)
            .setData(Uri.parse("lumi://reminder/$reminderId"))
            .putExtra(Notifier.EXTRA_REMINDER_ID, reminderId)
        return PendingIntent.getBroadcast(context, (reminderId % Int.MAX_VALUE).toInt(), intent, flags or PendingIntent.FLAG_IMMUTABLE)
    }

    fun canScheduleExact(): Boolean = Build.VERSION.SDK_INT < 31 || alarms.canScheduleExactAlarms()

    override fun scheduleTime(reminderId: Long, triggerAtMillis: Long): ScheduleOutcome {
        val pi = pending(reminderId, PendingIntent.FLAG_UPDATE_CURRENT) ?: return ScheduleOutcome.Failed("Android did not provide an alarm slot.")
        if (canScheduleExact()) {
            try {
                alarms.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pi)
                return ScheduleOutcome.Exact
            } catch (e: SecurityException) {
                log.event("alarm", "Exact alarm refused: ${e.message}", success = false)
            } catch (e: RuntimeException) {
                log.event("alarm", "Exact alarm failed: ${e.message}", success = false)
            }
        }
        return try {
            alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pi)
            ScheduleOutcome.Inexact("Exact alarms aren't allowed for Lumi, so Android may deliver this a few minutes late. You can allow them in the Permissions screen.")
        } catch (e: RuntimeException) {
            log.event("alarm", "Alarm failed: ${e.message}", success = false)
            ScheduleOutcome.Failed("Android refused to schedule the alarm (${e.message ?: e.javaClass.simpleName}).")
        }
    }

    override fun cancelTime(reminderId: Long) {
        val pi = pending(reminderId, PendingIntent.FLAG_NO_CREATE) ?: return
        alarms.cancel(pi)
        pi.cancel()
    }

    override suspend fun scheduleLocation(reminder: Reminder, place: Place): ScheduleOutcome = geofences.add(reminder, place)

    override suspend fun cancelLocation(reminderId: Long) = geofences.remove(reminderId)
}

/** Receives alarm deliveries and the notification action buttons. */
class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        val reminderId = intent.getLongExtra(Notifier.EXTRA_REMINDER_ID, -1)
        val taskId = intent.getLongExtra(Notifier.EXTRA_TASK_ID, -1)
        val graph = LumiApp.graph(context)
        val result = goAsync()
        CoroutineScope(SupervisorJob() + Dispatchers.Default).launch {
            try {
                withTimeoutOrNull(8_000) {
                    when (action) {
                        Notifier.ACTION_FIRE -> {
                            val fired = graph.tasks.onAlarm(reminderId)
                            if (fired != null) {
                                val shown = graph.notifier.showReminder(fired)
                                graph.log.event("reminder", if (shown) "Delivered: ${fired.task.title}" else "Due but notifications are blocked: ${fired.task.title}", shown)
                            }
                        }
                        Notifier.ACTION_COMPLETE -> {
                            graph.tasks.complete(taskId)
                            graph.notifier.cancel(taskId)
                            graph.log.event("reminder", "Completed from notification")
                        }
                        Notifier.ACTION_SNOOZE, Notifier.ACTION_POSTPONE -> {
                            val minutes = if (action == Notifier.ACTION_SNOOZE) Notifier.SNOOZE_MINUTES else Notifier.POSTPONE_MINUTES
                            val r = graph.tasks.postpone(taskId, byMinutes = minutes)
                            if (r == null) {
                                graph.notifier.cancel(taskId)
                            } else if (r.outcome is ScheduleOutcome.Failed) {
                                graph.notifier.showUpdate(taskId, r.task.title, context.getString(R.string.snooze_failed))
                            } else {
                                val at = r.reminder.triggerAt?.let { TimeFormat.time(graph.time.toLocal(it).toLocalTime()) }.orEmpty()
                                graph.notifier.showUpdate(taskId, r.task.title, context.getString(R.string.snoozed_until, at))
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                graph.log.event("reminder", "Receiver error: ${e.message}", success = false)
            } finally {
                result.finish()
            }
        }
    }
}

/** Re-registers alarms and geofences after anything that makes Android forget them. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        if (action !in HANDLED) return
        val graph = LumiApp.graph(context)
        val result = goAsync()
        CoroutineScope(SupervisorJob() + Dispatchers.Default).launch {
            try {
                withTimeoutOrNull(9_000) { graph.restoreSchedules(action.substringAfterLast('.')) }
            } catch (e: Exception) {
                graph.log.event("restore", "Restore failed: ${e.message}", success = false)
            } finally {
                result.finish()
            }
        }
    }

    companion object {
        val HANDLED = setOf(
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_LOCKED_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            Intent.ACTION_TIME_CHANGED,
            Intent.ACTION_TIMEZONE_CHANGED,
            "android.app.action.SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED",
            "android.intent.action.QUICKBOOT_POWERON",
        )
    }
}

/**
 * Safety net: twice a day WorkManager re-registers anything Android dropped (force-stop,
 * aggressive vendor battery managers) and surfaces reminders that were missed. This is a
 * deferrable, batched job, not polling: it does no work besides re-registering alarms.
 */
class SafetyNetWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): ListenableWorker.Result {
        val graph = LumiApp.graph(applicationContext)
        runBlocking { graph.restoreSchedules("safety-net") }
        return ListenableWorker.Result.success()
    }

    companion object {
        private const val NAME = "lumi-safety-net"

        fun ensureScheduled(context: Context) {
            val request = PeriodicWorkRequest.Builder(SafetyNetWorker::class.java, 12, TimeUnit.HOURS).build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(NAME, ExistingPeriodicWorkPolicy.KEEP, request)
        }
    }
}

/** Schedules the optional daily briefing with a single inexact alarm per day. */
class BriefingScheduler(private val context: Context) {
    private val alarms = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager

    private fun pending(flags: Int): PendingIntent? = PendingIntent.getBroadcast(
        context,
        Notifier.BRIEFING_ID,
        Intent(context, BriefingReceiver::class.java).setAction(Notifier.ACTION_BRIEFING),
        flags or PendingIntent.FLAG_IMMUTABLE,
    )

    /** Returns the next trigger time, or null when the briefing is off or could not be scheduled. */
    fun reschedule(enabled: Boolean, minuteOfDay: Int): Long? {
        pending(PendingIntent.FLAG_NO_CREATE)?.let {
            alarms.cancel(it)
            it.cancel()
        }
        if (!enabled) return null
        val now = java.time.ZonedDateTime.now()
        var next = now.toLocalDate().atTime(minuteOfDay / 60, minuteOfDay % 60).atZone(now.zone)
        if (!next.isAfter(now.plusSeconds(30))) next = next.plusDays(1)
        val at = next.toInstant().toEpochMilli()
        return try {
            alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pending(PendingIntent.FLAG_UPDATE_CURRENT) ?: return null)
            at
        } catch (e: RuntimeException) {
            null
        }
    }
}

class BriefingReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Notifier.ACTION_BRIEFING) return
        val graph = LumiApp.graph(context)
        val result = goAsync()
        CoroutineScope(SupervisorJob() + Dispatchers.Default).launch {
            try {
                withTimeoutOrNull(8_000) {
                    if (graph.settings.briefingEnabled) {
                        val text = graph.device.briefingText()
                        val shown = graph.notifier.showBriefing(text)
                        graph.log.event("briefing", if (shown) "Briefing delivered" else "Briefing ready but notifications are blocked", shown)
                    }
                    graph.briefings.reschedule(graph.settings.briefingEnabled, graph.settings.briefingMinute)
                }
            } catch (e: Exception) {
                graph.log.event("briefing", "Briefing failed: ${e.message}", success = false)
            } finally {
                result.finish()
            }
        }
    }
}
