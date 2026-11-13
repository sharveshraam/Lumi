package com.lumi.assistant

import android.app.Application
import android.content.Context
import com.lumi.assistant.data.DebugLog
import com.lumi.assistant.data.LumiDb
import com.lumi.assistant.data.SecureStore
import com.lumi.assistant.data.Settings
import com.lumi.assistant.data.SqlHistoryStore
import com.lumi.assistant.data.SqlMemoryStore
import com.lumi.assistant.data.SqlPatternStore
import com.lumi.assistant.data.SqlPlaceStore
import com.lumi.assistant.data.SqlTaskRepository
import com.lumi.assistant.reminders.AlarmScheduler
import com.lumi.assistant.reminders.BriefingScheduler
import com.lumi.assistant.reminders.GeofenceManager
import com.lumi.assistant.reminders.Notifier
import com.lumi.assistant.reminders.SafetyNetWorker
import com.lumi.assistant.tools.AndroidPermissions
import com.lumi.assistant.tools.DeviceTools
import com.lumi.assistant.tools.PhoneTools
import com.lumi.assistant.tools.ToolSupport
import com.lumi.assistant.voice.AndroidSpeechSynth
import com.lumi.core.agent.AiTextReviser
import com.lumi.core.agent.Orchestrator
import com.lumi.core.ai.AiClient
import com.lumi.core.ai.AiConfig
import com.lumi.core.ai.HttpAiClient
import com.lumi.core.learning.LearningEngine
import com.lumi.core.learning.OnboardingFlow
import com.lumi.core.memory.MemoryService
import com.lumi.core.model.SystemTimeSource
import com.lumi.core.model.TimeSource
import com.lumi.core.nlu.LocalIntentParser
import com.lumi.core.tasks.TaskService
import com.lumi.core.tools.DraftTools
import com.lumi.core.tools.MemoryTools
import com.lumi.core.tools.SessionState
import com.lumi.core.tools.TaskTools
import com.lumi.core.tools.ToolRegistry

/**
 * Everything Lumi needs, built once per process. Construction is cheap: nothing here starts
 * a thread, opens the microphone or touches the network.
 */
class Graph(val context: Context) {
    val db = LumiDb(context)
    val log = DebugLog(db)
    val settings = Settings(context, SecureStore(context))
    val time: TimeSource = SystemTimeSource
    val notifier = Notifier(context)
    val geofences = GeofenceManager(context, log)
    val scheduler = AlarmScheduler(context, geofences, log)
    val places = SqlPlaceStore(db)
    val tasks = TaskService(SqlTaskRepository(db), scheduler, places, time)
    val memory = MemoryService(SqlMemoryStore(db), settings, time)
    val patterns = SqlPatternStore(db)
    val learning = LearningEngine(patterns, settings, time)
    val history = SqlHistoryStore(db) { settings.historyEnabled }
    val session = SessionState()
    val permissions = AndroidPermissions(context)
    val support = ToolSupport(context, permissions)
    val speech = AndroidSpeechSynth(context, settings)
    val briefings = BriefingScheduler(context)

    private var cachedConfig: AiConfig? = null
    private var cachedClient: AiClient? = null

    /** The configured AI client, or null when Lumi is running offline-only. */
    @Synchronized
    fun aiClient(): AiClient? {
        val config = settings.aiConfig()
        if (config == null) {
            cachedConfig = null
            cachedClient = null
            return null
        }
        if (config != cachedConfig) {
            cachedConfig = config
            cachedClient = HttpAiClient(config)
        }
        return cachedClient
    }

    val phone = PhoneTools(support, settings, memory, session)
    val device = DeviceTools(support, settings, memory, tasks, places, time, geofences) { aiClient() }

    val registry = ToolRegistry(
        TaskTools(tasks, session, time, learning) { settings.timeDefaults() }.all() +
            MemoryTools(memory, settings).all() +
            DraftTools(session, phone.messaging, AiTextReviser { aiClient() }).all() +
            phone.all() +
            device.all(),
    )

    val orchestrator = Orchestrator(
        registry = registry,
        parser = LocalIntentParser(time),
        session = session,
        memory = memory,
        history = history,
        learning = learning,
        onboarding = OnboardingFlow(memory),
        ai = { aiClient() },
        permissions = permissions,
        settings = settings,
        log = log,
        time = time,
    )

    /** Re-registers every alarm and geofence and surfaces reminders that were missed meanwhile. */
    suspend fun restoreSchedules(reason: String) {
        val report = tasks.restore()
        for (missed in report.missed) notifier.showReminder(missed)
        briefings.reschedule(settings.briefingEnabled, settings.briefingMinute)
        log.event(
            "restore",
            "After $reason: ${report.rescheduled} rescheduled, ${report.failed} failed, ${report.missed.size} missed",
            report.failed == 0,
        )
    }
}

class LumiApp : Application() {
    override fun onCreate() {
        super.onCreate()
        val graph = graph(this)
        graph.notifier.createChannels()
        try {
            SafetyNetWorker.ensureScheduled(this)
        } catch (e: Exception) {
            graph.log.event("restore", "Could not schedule the safety net: ${e.message}", success = false)
        }
    }

    companion object {
        @Volatile private var instance: Graph? = null

        fun graph(context: Context): Graph = instance ?: synchronized(this) {
            instance ?: Graph(context.applicationContext).also { instance = it }
        }
    }
}
