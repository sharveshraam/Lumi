package com.lumi.assistant.data

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.lumi.core.agent.ActionLog
import com.lumi.core.memory.HistoryStore
import com.lumi.core.memory.MemoryStore
import com.lumi.core.memory.PatternStore
import com.lumi.core.model.Memory
import com.lumi.core.model.MemoryCategory
import com.lumi.core.model.PatternRecord
import com.lumi.core.model.PatternStatus
import com.lumi.core.model.Place
import com.lumi.core.model.PlaceTransition
import com.lumi.core.model.Priority
import com.lumi.core.model.Reminder
import com.lumi.core.model.ReminderStatus
import com.lumi.core.model.SubtaskCodec
import com.lumi.core.model.Task
import com.lumi.core.model.Turn
import com.lumi.core.tasks.PlaceStore
import com.lumi.core.tasks.TaskRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext

/**
 * Lumi's local database. Plain SQLite through the platform [SQLiteOpenHelper]: everything stays on
 * the device, there is no annotation processing step, and each category of data has its own table
 * so it can be inspected or cleared independently.
 */
class LumiDb(context: Context) : SQLiteOpenHelper(context.applicationContext, "lumi.db", null, VERSION) {

    /** Incremented after every write so screens can refresh without polling. */
    private val _changes = MutableStateFlow(0L)
    val changes: StateFlow<Long> = _changes

    fun notifyChanged() {
        _changes.value = _changes.value + 1
    }

    override fun onConfigure(db: SQLiteDatabase) {
        db.setForeignKeyConstraintsEnabled(true)
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', " +
                "created_at INTEGER NOT NULL, due_at INTEGER, priority TEXT NOT NULL DEFAULT 'NORMAL', completed INTEGER NOT NULL DEFAULT 0, " +
                "completed_at INTEGER, category TEXT, subtasks TEXT NOT NULL DEFAULT '', recurrence TEXT, notes TEXT NOT NULL DEFAULT '', last_completed_at INTEGER)",
        )
        db.execSQL(
            "CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, " +
                "trigger_at INTEGER, anchor_at INTEGER, recurrence TEXT, place_name TEXT, transition TEXT, status TEXT NOT NULL DEFAULT 'SCHEDULED', " +
                "nag_minutes INTEGER NOT NULL DEFAULT 0, nag_count INTEGER NOT NULL DEFAULT 0, last_fired_at INTEGER, exact INTEGER NOT NULL DEFAULT 1)",
        )
        db.execSQL("CREATE INDEX idx_reminders_task ON reminders(task_id)")
        db.execSQL("CREATE TABLE places (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, radius REAL NOT NULL)")
        db.execSQL(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, mem_key TEXT, content TEXT NOT NULL, " +
                "source TEXT NOT NULL DEFAULT 'explicit', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)",
        )
        db.execSQL("CREATE INDEX idx_memories_category ON memories(category)")
        db.execSQL("CREATE TABLE patterns (pattern_key TEXT PRIMARY KEY, label TEXT NOT NULL, count INTEGER NOT NULL, status TEXT NOT NULL, last_seen_at INTEGER NOT NULL)")
        db.execSQL("CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, text TEXT NOT NULL, at INTEGER NOT NULL)")
        db.execSQL("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, tag TEXT NOT NULL, detail TEXT NOT NULL, success INTEGER NOT NULL, message TEXT NOT NULL)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // Version 1 is the first schema. Future versions add explicit migrations here.
    }

    companion object {
        const val VERSION = 1
    }
}

private fun Cursor.longOrNull(i: Int): Long? = if (isNull(i)) null else getLong(i)
private fun Cursor.stringOrNull(i: Int): String? = if (isNull(i)) null else getString(i)

private inline fun <T> SQLiteDatabase.list(sql: String, args: Array<String>? = null, map: (Cursor) -> T): List<T> {
    val out = ArrayList<T>()
    rawQuery(sql, args).use { c -> while (c.moveToNext()) out += map(c) }
    return out
}

private inline fun <reified E : Enum<E>> enumOr(name: String?, fallback: E): E =
    enumValues<E>().firstOrNull { it.name == name } ?: fallback

// ---------------------------------------------------------------------------------------- tasks

class SqlTaskRepository(private val helper: LumiDb) : TaskRepository {
    private val taskCols = "id, title, description, created_at, due_at, priority, completed, completed_at, category, subtasks, recurrence, notes, last_completed_at"
    private val reminderCols = "id, task_id, trigger_at, anchor_at, recurrence, place_name, transition, status, nag_minutes, nag_count, last_fired_at, exact"

    private fun task(c: Cursor) = Task(
        id = c.getLong(0),
        title = c.getString(1),
        description = c.getString(2),
        createdAt = c.getLong(3),
        dueAt = c.longOrNull(4),
        priority = enumOr(c.getString(5), Priority.NORMAL),
        completed = c.getInt(6) != 0,
        completedAt = c.longOrNull(7),
        category = c.stringOrNull(8),
        subtasks = SubtaskCodec.decode(c.getString(9)),
        recurrence = c.stringOrNull(10),
        notes = c.getString(11),
        lastCompletedAt = c.longOrNull(12),
    )

    private fun values(t: Task) = ContentValues().apply {
        put("title", t.title)
        put("description", t.description)
        put("created_at", t.createdAt)
        put("due_at", t.dueAt)
        put("priority", t.priority.name)
        put("completed", if (t.completed) 1 else 0)
        put("completed_at", t.completedAt)
        put("category", t.category)
        put("subtasks", SubtaskCodec.encode(t.subtasks))
        put("recurrence", t.recurrence)
        put("notes", t.notes)
        put("last_completed_at", t.lastCompletedAt)
    }

    private fun reminder(c: Cursor) = Reminder(
        id = c.getLong(0),
        taskId = c.getLong(1),
        triggerAt = c.longOrNull(2),
        anchorAt = c.longOrNull(3),
        recurrence = c.stringOrNull(4),
        placeName = c.stringOrNull(5),
        transition = c.stringOrNull(6)?.let { n -> PlaceTransition.values().firstOrNull { it.name == n } },
        status = enumOr(c.getString(7), ReminderStatus.SCHEDULED),
        nagMinutes = c.getInt(8),
        nagCount = c.getInt(9),
        lastFiredAt = c.longOrNull(10),
        exact = c.getInt(11) != 0,
    )

    private fun values(r: Reminder) = ContentValues().apply {
        put("task_id", r.taskId)
        put("trigger_at", r.triggerAt)
        put("anchor_at", r.anchorAt)
        put("recurrence", r.recurrence)
        put("place_name", r.placeName)
        put("transition", r.transition?.name)
        put("status", r.status.name)
        put("nag_minutes", r.nagMinutes)
        put("nag_count", r.nagCount)
        put("last_fired_at", r.lastFiredAt)
        put("exact", if (r.exact) 1 else 0)
    }

    private suspend fun <T> io(notify: Boolean = false, block: (SQLiteDatabase) -> T): T = withContext(Dispatchers.IO) {
        val result = block(helper.writableDatabase)
        if (notify) helper.notifyChanged()
        result
    }

    override suspend fun insertTask(task: Task): Long = io(true) { it.insertOrThrow("tasks", null, values(task)) }
    override suspend fun updateTask(task: Task) { io(true) { it.update("tasks", values(task), "id = ?", arrayOf(task.id.toString())) } }
    override suspend fun deleteTask(id: Long) {
        io(true) {
            it.delete("reminders", "task_id = ?", arrayOf(id.toString()))
            it.delete("tasks", "id = ?", arrayOf(id.toString()))
        }
    }
    override suspend fun getTask(id: Long): Task? = io { it.list("SELECT $taskCols FROM tasks WHERE id = ?", arrayOf(id.toString()), ::task).firstOrNull() }
    override suspend fun allTasks(): List<Task> = io { it.list("SELECT $taskCols FROM tasks ORDER BY id", null, ::task) }

    override suspend fun insertReminder(reminder: Reminder): Long = io(true) { it.insertOrThrow("reminders", null, values(reminder)) }
    override suspend fun updateReminder(reminder: Reminder) { io(true) { it.update("reminders", values(reminder), "id = ?", arrayOf(reminder.id.toString())) } }
    override suspend fun deleteReminder(id: Long) { io(true) { it.delete("reminders", "id = ?", arrayOf(id.toString())) } }
    override suspend fun getReminder(id: Long): Reminder? = io { it.list("SELECT $reminderCols FROM reminders WHERE id = ?", arrayOf(id.toString()), ::reminder).firstOrNull() }
    override suspend fun remindersForTask(taskId: Long): List<Reminder> =
        io { it.list("SELECT $reminderCols FROM reminders WHERE task_id = ? ORDER BY id", arrayOf(taskId.toString()), ::reminder) }
    override suspend fun allReminders(): List<Reminder> = io { it.list("SELECT $reminderCols FROM reminders ORDER BY id", null, ::reminder) }
}

// ---------------------------------------------------------------------------------------- places

class SqlPlaceStore(private val helper: LumiDb) : PlaceStore {
    private fun place(c: Cursor) = Place(c.getLong(0), c.getString(1), c.getDouble(2), c.getDouble(3), c.getFloat(4))

    override suspend fun find(name: String): Place? = withContext(Dispatchers.IO) {
        helper.readableDatabase.list("SELECT id, name, latitude, longitude, radius FROM places WHERE name = ? COLLATE NOCASE", arrayOf(name.trim()), ::place).firstOrNull()
    }

    override suspend fun all(): List<Place> = withContext(Dispatchers.IO) {
        helper.readableDatabase.list("SELECT id, name, latitude, longitude, radius FROM places ORDER BY name COLLATE NOCASE", null, ::place)
    }

    override suspend fun save(place: Place): Long = withContext(Dispatchers.IO) {
        val db = helper.writableDatabase
        val v = ContentValues().apply {
            put("name", place.name.trim())
            put("latitude", place.latitude)
            put("longitude", place.longitude)
            put("radius", place.radiusMeters)
        }
        val existing = db.list("SELECT id FROM places WHERE name = ? COLLATE NOCASE", arrayOf(place.name.trim())) { it.getLong(0) }.firstOrNull()
        val id = if (existing != null) {
            db.update("places", v, "id = ?", arrayOf(existing.toString()))
            existing
        } else {
            db.insertOrThrow("places", null, v)
        }
        helper.notifyChanged()
        id
    }

    override suspend fun delete(id: Long) {
        withContext(Dispatchers.IO) {
            helper.writableDatabase.delete("places", "id = ?", arrayOf(id.toString()))
            helper.notifyChanged()
        }
    }
}

// ---------------------------------------------------------------------------------------- memory

class SqlMemoryStore(private val helper: LumiDb) : MemoryStore {
    private fun values(m: Memory) = ContentValues().apply {
        put("category", m.category.name)
        put("mem_key", m.key)
        put("content", m.content)
        put("source", m.source)
        put("created_at", m.createdAt)
        put("updated_at", m.updatedAt)
    }

    override suspend fun insert(memory: Memory): Long = withContext(Dispatchers.IO) {
        helper.writableDatabase.insertOrThrow("memories", null, values(memory)).also { helper.notifyChanged() }
    }

    override suspend fun update(memory: Memory) {
        withContext(Dispatchers.IO) {
            helper.writableDatabase.update("memories", values(memory), "id = ?", arrayOf(memory.id.toString()))
            helper.notifyChanged()
        }
    }

    override suspend fun delete(id: Long) {
        withContext(Dispatchers.IO) {
            helper.writableDatabase.delete("memories", "id = ?", arrayOf(id.toString()))
            helper.notifyChanged()
        }
    }

    override suspend fun all(): List<Memory> = withContext(Dispatchers.IO) {
        helper.readableDatabase.list("SELECT id, category, mem_key, content, source, created_at, updated_at FROM memories ORDER BY updated_at DESC", null) { c ->
            Memory(
                id = c.getLong(0),
                category = enumOr(c.getString(1), MemoryCategory.EXPLICIT),
                key = c.stringOrNull(2),
                content = c.getString(3),
                source = c.getString(4),
                createdAt = c.getLong(5),
                updatedAt = c.getLong(6),
            )
        }
    }

    override suspend fun clearCategory(category: MemoryCategory) {
        withContext(Dispatchers.IO) {
            helper.writableDatabase.delete("memories", "category = ?", arrayOf(category.name))
            helper.notifyChanged()
        }
    }
}

class SqlPatternStore(private val helper: LumiDb) : PatternStore {
    private fun record(c: Cursor) = PatternRecord(c.getString(0), c.getString(1), c.getInt(2), enumOr(c.getString(3), PatternStatus.OBSERVING), c.getLong(4))

    override suspend fun get(key: String): PatternRecord? = withContext(Dispatchers.IO) {
        helper.readableDatabase.list("SELECT pattern_key, label, count, status, last_seen_at FROM patterns WHERE pattern_key = ?", arrayOf(key), ::record).firstOrNull()
    }

    override suspend fun put(record: PatternRecord) {
        withContext(Dispatchers.IO) {
            val v = ContentValues().apply {
                put("pattern_key", record.key)
                put("label", record.label)
                put("count", record.count)
                put("status", record.status.name)
                put("last_seen_at", record.lastSeenAt)
            }
            helper.writableDatabase.insertWithOnConflict("patterns", null, v, SQLiteDatabase.CONFLICT_REPLACE)
            helper.notifyChanged()
        }
    }

    override suspend fun all(): List<PatternRecord> = withContext(Dispatchers.IO) {
        helper.readableDatabase.list("SELECT pattern_key, label, count, status, last_seen_at FROM patterns ORDER BY last_seen_at DESC", null, ::record)
    }

    override suspend fun clear() {
        withContext(Dispatchers.IO) {
            helper.writableDatabase.delete("patterns", null, null)
            helper.notifyChanged()
        }
    }
}

class SqlHistoryStore(private val helper: LumiDb, private val enabled: () -> Boolean) : HistoryStore {
    override suspend fun append(role: String, text: String) {
        if (!enabled()) return
        withContext(Dispatchers.IO) {
            val db = helper.writableDatabase
            db.insert("history", null, ContentValues().apply {
                put("role", role)
                put("text", text.take(2000))
                put("at", System.currentTimeMillis())
            })
            db.execSQL("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT $KEEP)")
        }
    }

    override suspend fun recent(limit: Int): List<Turn> = withContext(Dispatchers.IO) {
        if (!enabled()) return@withContext emptyList<Turn>()
        helper.readableDatabase.list("SELECT role, text, at FROM history ORDER BY id DESC LIMIT ?", arrayOf(limit.toString())) { Turn(it.getString(0), it.getString(1), it.getLong(2)) }.reversed()
    }

    override suspend fun clear() {
        withContext(Dispatchers.IO) {
            helper.writableDatabase.delete("history", null, null)
            helper.notifyChanged()
        }
    }

    companion object {
        const val KEEP = 200
    }
}

// ---------------------------------------------------------------------------------------- debug log

data class LogEntry(val at: Long, val tag: String, val detail: String, val success: Boolean, val message: String)

/** Small rolling action log shown in the Diagnostics screen. Private tool output is never written here. */
class DebugLog(private val helper: LumiDb) : ActionLog {
    override suspend fun record(tool: String, args: String, success: Boolean, message: String) {
        withContext(Dispatchers.IO) { write(tool, args, success, message) }
    }

    /** Synchronous variant for receivers and services. */
    fun event(tag: String, message: String, success: Boolean = true) {
        try {
            write(tag, "", success, message)
        } catch (e: Exception) {
            // Logging must never break the caller.
        }
    }

    private fun write(tag: String, detail: String, success: Boolean, message: String) {
        val db = helper.writableDatabase
        db.insert("logs", null, ContentValues().apply {
            put("at", System.currentTimeMillis())
            put("tag", tag)
            put("detail", detail.take(400))
            put("success", if (success) 1 else 0)
            put("message", message.take(400))
        })
        db.execSQL("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT $KEEP)")
    }

    suspend fun recent(limit: Int = 200): List<LogEntry> = withContext(Dispatchers.IO) {
        helper.readableDatabase.list("SELECT at, tag, detail, success, message FROM logs ORDER BY id DESC LIMIT ?", arrayOf(limit.toString())) {
            LogEntry(it.getLong(0), it.getString(1), it.getString(2), it.getInt(3) != 0, it.getString(4))
        }
    }

    suspend fun clear() {
        withContext(Dispatchers.IO) { helper.writableDatabase.delete("logs", null, null) }
    }

    companion object {
        const val KEEP = 300
    }
}
