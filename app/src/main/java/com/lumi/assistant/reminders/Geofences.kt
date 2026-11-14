package com.lumi.assistant.reminders

import android.Manifest
import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.location.Location
import android.os.Build
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingEvent
import com.google.android.gms.location.GeofencingRequest
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.lumi.assistant.LumiApp
import com.lumi.assistant.data.DebugLog
import com.lumi.core.model.Place
import com.lumi.core.model.PlaceTransition
import com.lumi.core.model.Reminder
import com.lumi.core.tasks.ScheduleOutcome
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume

/**
 * Location reminders use Google Play services geofencing: the system batches location work for
 * all apps and wakes Lumi only on a transition, so Lumi itself never tracks location.
 */
class GeofenceManager(private val context: Context, private val log: DebugLog) {

    fun hasForegroundLocation(): Boolean =
        context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED

    fun hasBackgroundLocation(): Boolean =
        hasForegroundLocation() && (Build.VERSION.SDK_INT < 29 || context.checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION) == PackageManager.PERMISSION_GRANTED)

    private fun pendingIntent(): PendingIntent {
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
        return PendingIntent.getBroadcast(context, 7001, Intent(context, GeofenceReceiver::class.java).setAction(ACTION_GEOFENCE), flags)
    }

    @SuppressLint("MissingPermission")
    suspend fun add(reminder: Reminder, place: Place): ScheduleOutcome {
        if (!hasBackgroundLocation()) {
            return ScheduleOutcome.LocationPending(
                "Lumi needs the \"Allow all the time\" location permission to notice when you arrive at ${place.name}. Until then I can't trigger it automatically.",
            )
        }
        val transition = if (reminder.transition == PlaceTransition.EXIT) Geofence.GEOFENCE_TRANSITION_EXIT else Geofence.GEOFENCE_TRANSITION_ENTER
        val geofence = Geofence.Builder()
            .setRequestId(requestId(reminder.id))
            .setCircularRegion(place.latitude, place.longitude, place.radiusMeters.coerceAtLeast(100f))
            .setExpirationDuration(Geofence.NEVER_EXPIRE)
            .setTransitionTypes(transition)
            .setNotificationResponsiveness(RESPONSIVENESS_MS)
            .build()
        val request = GeofencingRequest.Builder()
            .setInitialTrigger(0) // only real transitions: being there already must not fire "when I reach"
            .addGeofence(geofence)
            .build()
        return try {
            val error = withTimeoutOrNull(10_000) {
                suspendCancellableCoroutine<String?> { cont ->
                    LocationServices.getGeofencingClient(context).addGeofences(request, pendingIntent())
                        .addOnSuccessListener { if (cont.isActive) cont.resume(null) }
                        .addOnFailureListener { e -> if (cont.isActive) cont.resume(e.message ?: e.javaClass.simpleName) }
                }
            } ?: "Google Play services did not respond in time."
            if (error == null) {
                ScheduleOutcome.LocationArmed
            } else {
                log.event("geofence", "Could not add geofence: $error", success = false)
                ScheduleOutcome.Failed("Google Play services refused the location trigger ($error). Check that Location is switched on.")
            }
        } catch (e: Exception) {
            log.event("geofence", "Geofencing unavailable: ${e.message}", success = false)
            ScheduleOutcome.Failed("Location triggers aren't available on this device (${e.message ?: e.javaClass.simpleName}).")
        }
    }

    suspend fun remove(reminderId: Long) {
        try {
            LocationServices.getGeofencingClient(context).removeGeofences(listOf(requestId(reminderId)))
        } catch (e: Exception) {
            log.event("geofence", "Could not remove geofence: ${e.message}", success = false)
        }
    }

    /** One on-demand location fix for "remember this place". Never used in the background. */
    @SuppressLint("MissingPermission")
    suspend fun currentLocation(): Location? {
        if (!hasForegroundLocation()) return null
        return try {
            withTimeoutOrNull(15_000) {
                suspendCancellableCoroutine<Location?> { cont ->
                    LocationServices.getFusedLocationProviderClient(context)
                        .getCurrentLocation(Priority.PRIORITY_BALANCED_POWER_ACCURACY, null)
                        .addOnSuccessListener { loc -> if (cont.isActive) cont.resume(loc) }
                        .addOnFailureListener { if (cont.isActive) cont.resume(null) }
                }
            }
        } catch (e: Exception) {
            null
        }
    }

    companion object {
        const val ACTION_GEOFENCE = "com.lumi.assistant.action.GEOFENCE"
        /** Lets the system batch location checks; a couple of minutes of latency is fine for reminders. */
        const val RESPONSIVENESS_MS = 120_000

        fun requestId(reminderId: Long) = "reminder-$reminderId"
        fun reminderId(requestId: String): Long? = requestId.removePrefix("reminder-").toLongOrNull()
    }
}

class GeofenceReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != GeofenceManager.ACTION_GEOFENCE) return
        val event = GeofencingEvent.fromIntent(intent) ?: return
        val graph = LumiApp.graph(context)
        if (event.hasError()) {
            graph.log.event("geofence", "Geofence error code ${event.errorCode}", success = false)
            return
        }
        val ids = event.triggeringGeofences.orEmpty().mapNotNull { GeofenceManager.reminderId(it.requestId) }
        if (ids.isEmpty()) return
        val result = goAsync()
        CoroutineScope(SupervisorJob() + Dispatchers.Default).launch {
            try {
                withTimeoutOrNull(8_000) {
                    for (id in ids) {
                        val fired = graph.tasks.onPlaceTriggered(id) ?: continue
                        val shown = graph.notifier.showReminder(fired)
                        graph.log.event("geofence", if (shown) "Place reminder delivered: ${fired.task.title}" else "Place reminder due but notifications are blocked", shown)
                    }
                }
            } catch (e: Exception) {
                graph.log.event("geofence", "Receiver error: ${e.message}", success = false)
            } finally {
                result.finish()
            }
        }
    }
}
