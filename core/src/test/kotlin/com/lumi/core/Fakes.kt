package com.lumi.core

import com.lumi.core.agent.ActionLog
import com.lumi.core.agent.AssistantSettings
import com.lumi.core.agent.AiTextReviser
import com.lumi.core.agent.Orchestrator
import com.lumi.core.ai.AiClient
import com.lumi.core.ai.AiResult
import com.lumi.core.ai.ChatMessage
import com.lumi.core.ai.ToolDef
import com.lumi.core.ai.ToolParam
import com.lumi.core.learning.LearningEngine
import com.lumi.core.learning.OnboardingFlow
import com.lumi.core.memory.HistoryStore
import com.lumi.core.memory.MemoryPolicy
import com.lumi.core.memory.MemoryService
import com.lumi.core.memory.MemoryStore
import com.lumi.core.memory.PatternStore
import com.lumi.core.model.Memory
import com.lumi.core.model.MemoryCategory
import com.lumi.core.model.PatternRecord
import com.lumi.core.model.Place
import com.lumi.core.model.Reminder
import com.lumi.core.model.Task
import com.lumi.core.model.TimeSource
import com.lumi.core.model.Turn
import com.lumi.core.nlu.LocalIntentParser
import com.lumi.core.tasks.PlaceStore
import com.lumi.core.tasks.ReminderScheduler
import com.lumi.core.tasks.ScheduleOutcome
import com.lumi.core.tasks.TaskRepository
import com.lumi.core.tasks.TaskService
import com.lumi.core.tools.Args
import com.lumi.core.tools.Confirmation
import com.lumi.core.tools.Draft
import com.lumi.core.tools.DraftTools
import com.lumi.core.tools.LearningSwitch
import com.lumi.core.tools.MemoryTools
import com.lumi.core.tools.MessagingBackend
import com.lumi.core.tools.PermissionGate
import com.lumi.core.tools.Recipient
import com.lumi.core.tools.ReplyTarget
import com.lumi.core.tools.SessionState
import com.lumi.core.tools.TaskTools
import com.lumi.core.tools.Tool
import com.lumi.core.tools.ToolIds
import com.lumi.core.tools.ToolRegistry
import com.lumi.core.tools.ToolResult
import com.lumi.core.tools.ToolSpec
import com.lumi.core.tools.TurnEnv
import java.time.LocalDateTime
import java.time.ZoneId

/** Friday 13 November 2026, 10:00 in Asia/Kolkata unless a test moves it. */
class FakeTime(start: LocalDateTime = LocalDateTime.of(2026, 11, 13, 10, 0)) : TimeSource {
    private val zoneId = ZoneId.of("Asia/Kolkata")
    var current: Long = start.atZone(zoneId).toInstant().toEpochMilli()
    override fun nowMillis(): Long = current
    override fun zone(): ZoneId = zoneId
    fun set(dt: LocalDateTime) { current = dt.atZone(zoneId).toInstant().toEpochMilli() }
    fun advanceMinutes(m: Long) { current += m * 60_000 }
    fun millis(dt: LocalDateTime): Long = dt.atZone(zoneId).toInstant().toEpochMilli()
}

class InMemoryTaskRepo : TaskRepository {
    val tasks = LinkedHashMap<Long, Task>()
    val reminders = LinkedHashMap<Long, Reminder>()
    private var nextTask = 1L
    private var nextReminder = 1L
    override suspend fun insertTask(task: Task): Long { val id = nextTask++; tasks[id] = task.copy(id = id); return id }
    override suspend fun updateTask(task: Task) { tasks[task.id] = task }
    override suspend fun deleteTask(id: Long) { tasks.remove(id); reminders.values.removeIf { it.taskId == id } }
    override suspend fun getTask(id: Long): Task? = tasks[id]
    override suspend fun allTasks(): List<Task> = tasks.values.toList()
    override suspend fun insertReminder(reminder: Reminder): Long { val id = nextReminder++; reminders[id] = reminder.copy(id = id); return id }
    override suspend fun updateReminder(reminder: Reminder) { reminders[reminder.id] = reminder }
    override suspend fun deleteReminder(id: Long) { reminders.remove(id) }
    override suspend fun getReminder(id: Long): Reminder? = reminders[id]
    override suspend fun remindersForTask(taskId: Long): List<Reminder> = reminders.values.filter { it.taskId == taskId }
    override suspend fun allReminders(): List<Reminder> = reminders.values.toList()
}

class FakeScheduler : ReminderScheduler {
    /** reminderId -> trigger time of the currently registered alarm. */
    val alarms = HashMap<Long, Long>()
    val geofences = HashMap<Long, Place>()
    var timeOutcome: ScheduleOutcome = ScheduleOutcome.Exact
    var locationOutcome: ScheduleOutcome = ScheduleOutcome.LocationArmed
    override fun scheduleTime(reminderId: Long, triggerAtMillis: Long): ScheduleOutcome {
        if (timeOutcome.delivered) alarms[reminderId] = triggerAtMillis
        return timeOutcome
    }
    override fun cancelTime(reminderId: Long) { alarms.remove(reminderId) }
    override suspend fun scheduleLocation(reminder: Reminder, place: Place): ScheduleOutcome {
        if (locationOutcome.delivered) geofences[reminder.id] = place
        return locationOutcome
    }
    override suspend fun cancelLocation(reminderId: Long) { geofences.remove(reminderId) }
}

class InMemoryPlaces : PlaceStore {
    val places = mutableListOf<Place>()
    override suspend fun find(name: String): Place? = places.firstOrNull { it.name.equals(name.trim(), ignoreCase = true) }
    override suspend fun all(): List<Place> = places.toList()
    override suspend fun save(place: Place): Long {
        places.removeAll { it.name.equals(place.name, ignoreCase = true) }
        val id = (places.maxOfOrNull { it.id } ?: 0L) + 1
        places += place.copy(id = id)
        return id
    }
    override suspend fun delete(id: Long) { places.removeAll { it.id == id } }
}

class InMemoryMemoryStore : MemoryStore {
    val items = mutableListOf<Memory>()
    private var next = 1L
    override suspend fun insert(memory: Memory): Long { val id = next++; items += memory.copy(id = id); return id }
    override suspend fun update(memory: Memory) { items.replaceAll { if (it.id == memory.id) memory else it } }
    override suspend fun delete(id: Long) { items.removeAll { it.id == id } }
    override suspend fun all(): List<Memory> = items.toList()
    override suspend fun clearCategory(category: MemoryCategory) { items.removeAll { it.category == category } }
}

class FakePolicy : MemoryPolicy, LearningSwitch {
    var learning = true
    val disabled = mutableSetOf<MemoryCategory>()
    override fun learningEnabled(): Boolean = learning
    override fun categoryEnabled(category: MemoryCategory): Boolean = category !in disabled
    override fun setLearningEnabled(enabled: Boolean) { learning = enabled }
}

class InMemoryPatterns : PatternStore {
    val map = HashMap<String, PatternRecord>()
    override suspend fun get(key: String): PatternRecord? = map[key]
    override suspend fun put(record: PatternRecord) { map[record.key] = record }
    override suspend fun all(): List<PatternRecord> = map.values.toList()
    override suspend fun clear() { map.clear() }
}

class InMemoryHistory : HistoryStore {
    val turns = mutableListOf<Turn>()
    override suspend fun append(role: String, text: String) { turns += Turn(role, text, 0) }
    override suspend fun recent(limit: Int): List<Turn> = turns.takeLast(limit)
    override suspend fun clear() { turns.clear() }
}

class FakePermissions : PermissionGate {
    val denied = mutableSetOf<String>()
    override fun missing(permissions: List<String>): List<String> = permissions.filter { it in denied }
    override fun label(permission: String): String = permission.substringAfterLast('.').lowercase().replace('_', ' ')
}

/** Scripted AI: returns queued results in order and records what it was sent. */
class FakeAi : AiClient {
    val queue = ArrayDeque<AiResult>()
    val requests = mutableListOf<List<ChatMessage>>()
    var toolsSeen: List<ToolDef> = emptyList()
    override suspend fun chat(messages: List<ChatMessage>, tools: List<ToolDef>): AiResult {
        requests += messages.toList()
        toolsSeen = tools
        return queue.removeFirstOrNull() ?: AiResult.Ok("(no scripted reply)", emptyList())
    }
}

class FakeMessaging : MessagingBackend {
    val sent = mutableListOf<Draft>()
    var contacts = mapOf("alex" to "+911234567890", "mom" to "+919876543210")
    var target: ReplyTarget? = null
    override suspend fun resolveRecipient(name: String, app: String?): Recipient {
        val number = contacts[name.lowercase()] ?: return Recipient.NotFound
        return Recipient.Found(name.replaceFirstChar { it.uppercase() }, number, app)
    }
    override suspend fun replyTarget(key: String?): ReplyTarget? = target
    override suspend fun send(draft: Draft): ToolResult { sent += draft; return ToolResult.ok("Sent.", verified = true) }
}

/** Records invocations of an Android-side tool without touching Android. */
class RecordingTool(
    id: String,
    permissions: List<String> = emptyList(),
    confirmation: Confirmation = Confirmation.NEVER,
    allowedWhenLocked: Boolean = false,
    query: Boolean = false,
    privateOutput: Boolean = false,
    params: List<ToolParam> = emptyList(),
    private val result: (Args) -> ToolResult = { ToolResult.ok("$id done.") },
) : Tool {
    val invocations = mutableListOf<Args>()
    override val spec = ToolSpec(id, "fake $id", params, permissions, confirmation, allowedWhenLocked, query = query, privateOutput = privateOutput)
    override suspend fun execute(args: Args, env: TurnEnv): ToolResult { invocations += args; return result(args) }
}

/** A fully wired assistant brain with in-memory persistence, for end-to-end conversation tests. */
class Harness {
    val time = FakeTime()
    val repo = InMemoryTaskRepo()
    val scheduler = FakeScheduler()
    val places = InMemoryPlaces()
    val tasks = TaskService(repo, scheduler, places, time)
    val policy = FakePolicy()
    val memoryStore = InMemoryMemoryStore()
    val memory = MemoryService(memoryStore, policy, time)
    val patterns = InMemoryPatterns()
    val learning = LearningEngine(patterns, policy, time)
    val history = InMemoryHistory()
    val session = SessionState()
    val permissions = FakePermissions()
    val messaging = FakeMessaging()
    var ai: FakeAi? = null
    var confirmCalls = true
    val logged = mutableListOf<String>()

    val call = RecordingTool(ToolIds.CALL, permissions = listOf("android.permission.CALL_PHONE"), confirmation = Confirmation.IF_ENABLED) { ToolResult.ok("Calling ${it.string("contact")}.") }
    val openApp = RecordingTool(ToolIds.OPEN_APP) { ToolResult.ok("Opened ${it.string("name")}.") }
    val playMusic = RecordingTool(ToolIds.PLAY_MUSIC) { ToolResult.ok("Playing ${it.string("query")}.") }
    val notifications = RecordingTool(ToolIds.NOTIFICATIONS, privateOutput = true) { ToolResult.ok("Alex says the secret code is 4321.") }
    val readScreen = RecordingTool(ToolIds.READ_SCREEN, query = true) { ToolResult.ok("Screen text: Error 404 page not found") }
    val savePlace = RecordingTool(ToolIds.SAVE_PLACE) { ToolResult.ok("Saved this spot.") }
    val media = RecordingTool(ToolIds.MEDIA_CONTROL, allowedWhenLocked = true) { ToolResult.ok("Paused.") }

    private val registry = ToolRegistry(
        TaskTools(tasks, session, time, learning).all() +
            MemoryTools(memory, policy).all() +
            DraftTools(session, messaging, AiTextReviser { ai }).all() +
            listOf(call, openApp, playMusic, notifications, readScreen, savePlace, media),
    )

    val orchestrator = Orchestrator(
        registry = registry,
        parser = LocalIntentParser(time),
        session = session,
        memory = memory,
        history = history,
        learning = learning,
        onboarding = OnboardingFlow(memory),
        ai = { ai },
        permissions = permissions,
        settings = object : AssistantSettings {
            override fun confirmationEnabled(toolId: String): Boolean = confirmCalls
        },
        log = object : ActionLog {
            override suspend fun record(tool: String, args: String, success: Boolean, message: String) { logged += "$tool:$success" }
        },
        time = time,
    )

    fun say(text: String, locked: Boolean = false) = kotlinx.coroutines.runBlocking { orchestrator.handle(text, TurnEnv(deviceLocked = locked)) }
}
