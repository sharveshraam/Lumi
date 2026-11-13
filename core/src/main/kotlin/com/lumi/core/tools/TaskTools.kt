package com.lumi.core.tools

import com.lumi.core.ai.ToolParam
import com.lumi.core.learning.LearningEngine
import com.lumi.core.model.PlaceTransition
import com.lumi.core.model.Priority
import com.lumi.core.model.Subtask
import com.lumi.core.model.Task
import com.lumi.core.model.TimeSource
import com.lumi.core.model.now
import com.lumi.core.model.toLocal
import com.lumi.core.model.toMillis
import com.lumi.core.model.today
import com.lumi.core.tasks.CreateRequest
import com.lumi.core.tasks.ScheduleOutcome
import com.lumi.core.tasks.TaskMatch
import com.lumi.core.tasks.TaskService
import com.lumi.core.time.DateTimeParser
import com.lumi.core.time.Recurrence
import com.lumi.core.time.TimeDefaults
import com.lumi.core.time.TimeFormat
import java.time.LocalDate
import java.time.LocalDateTime

object ToolIds {
    const val CREATE_TASK = "create_task"
    const val LIST_TASKS = "list_tasks"
    const val COMPLETE_TASK = "complete_task"
    const val POSTPONE_TASK = "postpone_task"
    const val DELETE_TASK = "delete_task"
    const val UPDATE_TASK = "update_task"
    const val ADD_REMINDER = "add_reminder"
    const val SET_NAG = "remind_until_done"
    const val DELETE_COMPLETED = "delete_completed_tasks"
    const val LIST_LOCATION_REMINDERS = "list_location_reminders"
    const val REMEMBER = "remember"
    const val RECALL = "recall_memories"
    const val FORGET = "forget_memory"
    const val SET_PREFERENCE = "set_preference"
    const val SET_ALIAS = "set_contact_alias"
    const val SET_LEARNING = "set_learning"

    // Implemented by the Android layer.
    const val CALL = "call_contact"
    const val FIND_CONTACT = "find_contact"
    const val READ_MESSAGES = "read_messages"
    const val DRAFT_REPLY = "draft_reply"
    const val DRAFT_MESSAGE = "draft_message"
    const val DRAFT_EMAIL = "draft_email"
    const val TYPE_TEXT = "type_text"
    const val EDIT_DRAFT = "edit_draft"
    const val REVISE_DRAFT = "revise_draft"
    const val READ_DRAFT = "read_draft"
    const val SEND_DRAFT = "send_draft"
    const val DISCARD_DRAFT = "discard_draft"
    const val NOTIFICATIONS = "read_notifications"
    const val IGNORE_APP = "ignore_app_notifications"
    const val PAUSE_NOTIFICATIONS = "pause_notifications"
    const val OPEN_APP = "open_app"
    const val OPEN_SETTINGS = "open_settings"
    const val MEDIA_CONTROL = "media_control"
    const val PLAY_MUSIC = "play_music"
    const val NOW_PLAYING = "now_playing"
    const val SET_VOLUME = "set_volume"
    const val NAVIGATE = "navigate"
    const val SET_ALARM = "set_alarm"
    const val SET_TIMER = "set_timer"
    const val WEB_SEARCH = "web_search"
    const val READ_SCREEN = "read_screen"
    const val SCREEN_ACTION = "screen_action"
    const val FILL_FIELD = "fill_field"
    const val BRIEFING = "daily_briefing"
    const val BATTERY = "battery_status"
    const val CALENDAR = "calendar_events"
    const val SAVE_PLACE = "save_place"
}

/** Voice-facing task and reminder actions, built on [TaskService]. */
class TaskTools(
    private val tasks: TaskService,
    private val session: SessionState,
    private val time: TimeSource,
    private val learning: LearningEngine?,
    private val defaults: () -> TimeDefaults = { TimeDefaults() },
) {
    fun all(): List<Tool> = listOf(create, list, complete, postpone, delete, update, addReminder, nag, deleteCompleted, locationReminders)

    // ------------------------------------------------------------ shared helpers

    private fun parseWhen(text: String): LocalDateTime? {
        try {
            return LocalDateTime.parse(text.trim())
        } catch (e: Exception) {
            // not ISO; fall through to natural language
        }
        try {
            return LocalDate.parse(text.trim()).atTime(defaults().defaultTime)
        } catch (e: Exception) {
            // not an ISO date either
        }
        return DateTimeParser.parse(text, time.now(), defaults()).dateTime
    }

    private fun whenPhrase(millis: Long): String = TimeFormat.dateTime(time.toLocal(millis), time.now())

    private fun outcomeNote(outcome: ScheduleOutcome?): String = when (outcome) {
        is ScheduleOutcome.Inexact ->
            " Heads up: exact alarms aren't allowed for Lumi right now, so it may arrive a few minutes late. You can fix that in Permissions."
        else -> ""
    }

    private sealed class Resolved {
        data class Found(val task: Task) : Resolved()
        data class Ask(val prompt: String) : Resolved()
        data class Fail(val message: String) : Resolved()
    }

    private val ordinals = listOf("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth")

    private suspend fun resolve(args: Args, includeCompleted: Boolean = false): Resolved {
        args.long("task_id")?.let { id ->
            tasks.get(id)?.let { return Resolved.Found(it) }
        }
        val raw = args.string("query").orEmpty()
        val query = raw.lowercase().trim().trim('.', '?', '!')
        val pronoun = query.isEmpty() || Regex("^(it|this|that|this one|that one|last|the last one|my reminder|the reminder|my task|the task|that reminder|this reminder|that task|this task)$").matches(query)
        if (pronoun) {
            session.lastTaskId?.let { id -> tasks.get(id)?.let { if (includeCompleted || !it.completed) return Resolved.Found(it) } }
            tasks.lastFiredTask()?.let { return Resolved.Found(it) }
            val pending = tasks.pending()
            return when (pending.size) {
                0 -> Resolved.Fail("You don't have any open tasks.")
                1 -> Resolved.Found(pending[0])
                else -> Resolved.Ask("Which one? You have ${pending.size} open tasks, for example ${pending.take(3).joinToString(", ") { it.title }}.")
            }
        }
        val listed = session.listedTaskIds
        if (listed.isNotEmpty()) {
            val m = Regex("^(?:the |number )?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|\\d+)(?:st|nd|rd|th)?(?: one| task| reminder)?$").find(query)
            if (m != null) {
                val word = m.groupValues[1]
                val index = when {
                    word == "last" -> listed.size - 1
                    word.toIntOrNull() != null -> word.toInt() - 1
                    else -> ordinals.indexOf(word)
                }
                val id = listed.getOrNull(index)
                val task = id?.let { tasks.get(it) }
                if (task != null) return Resolved.Found(task)
            }
        }
        return when (val match = tasks.find(raw, includeCompleted)) {
            is TaskMatch.One -> Resolved.Found(match.task)
            is TaskMatch.Many -> Resolved.Ask("I found a few that match: ${match.tasks.take(4).joinToString(", ") { it.title }}. Which one?")
            TaskMatch.None -> Resolved.Fail("I couldn't find a task matching \"$raw\".")
        }
    }

    private suspend fun previewResolve(args: Args, includeCompleted: Boolean = false): Preview = when (val r = resolve(args, includeCompleted)) {
        is Resolved.Found -> Preview.Proceed
        is Resolved.Ask -> Preview.Ask("query", r.prompt)
        is Resolved.Fail -> Preview.Fail(r.message)
    }

    private val queryParam = ToolParam("query", "string", "Words identifying the task, e.g. \"assignment\". Leave empty for the task just discussed.")
    private val idParam = ToolParam("task_id", "integer", "Exact task id, if known from an earlier result.")

    private fun cleanTitle(raw: String): String {
        var t = raw.trim().trim('.', ',', '!', '?')
        t = Regex("^(?:to|about|that|for|me to)\\s+", RegexOption.IGNORE_CASE).replace(t, "")
        return t.trim().replaceFirstChar { it.uppercase() }
    }

    // ------------------------------------------------------------ create

    private val create = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.CREATE_TASK,
            description = "Create a task, optionally with a reminder. Use for 'remind me...', 'add a task...', to-dos and recurring activities. " +
                "A reminder is always attached to a task.",
            params = listOf(
                ToolParam("title", "string", "What needs to be done, without the time words.", required = true, prompt = "What should I remind you about?"),
                ToolParam("when", "string", "When it is due or should fire: ISO local date-time (2025-01-31T17:00) or natural language ('tomorrow 5pm', 'in 30 minutes')."),
                ToolParam("recurrence", "string", "Repeat rule in natural language, e.g. 'every monday', 'daily', 'every 2 weeks'."),
                ToolParam("place", "string", "Saved place name for a location reminder, e.g. 'college' or 'home'."),
                ToolParam("on_leave", "boolean", "For place reminders: true to fire when leaving instead of arriving."),
                ToolParam("priority", "string", "Importance.", enumValues = listOf("low", "normal", "high")),
                ToolParam("description", "string", "Extra details."),
                ToolParam("category", "string", "Optional category such as 'college' or 'home'."),
                ToolParam("reminder", "boolean", "Set false to store a due date without a notification. Defaults to true."),
            ),
            allowedWhenLocked = true,
            verification = "Reports the alarm or geofence registration result returned by Android; never claims a reminder was set unless it was.",
        )

        override suspend fun preview(args: Args, env: TurnEnv): Preview {
            if (args.string("title").isNullOrBlank()) return Preview.Ask("title", "What should I remind you about?")
            // The user explicitly asked to be reminded but gave no time or place: ask rather than guess.
            if (args.bool("reminder") == true && args.string("when") == null && args.string("recurrence") == null && args.string("place") == null) {
                return Preview.Ask("when", "Sure. When should I remind you?")
            }
            return Preview.Proceed
        }

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val title = cleanTitle(args.string("title") ?: return ToolResult.fail("I need to know what the task is."))
            if (title.isEmpty()) return ToolResult.fail("I need to know what the task is.")
            val now = time.now()
            val whenText = args.string("when")
            var recurrence = args.string("recurrence")?.let { Recurrence.decode(it) ?: DateTimeParser.parse(it, now, defaults()).recurrence }
            var at: LocalDateTime? = null
            if (whenText != null) {
                at = parseWhen(whenText) ?: return ToolResult.fail("I couldn't work out the time \"$whenText\". Could you say it another way?")
                if (recurrence == null) recurrence = DateTimeParser.parse(whenText, now, defaults()).recurrence
            } else if (args.string("recurrence") != null) {
                val parsed = DateTimeParser.parse(args.string("recurrence")!!, now, defaults())
                at = parsed.dateTime
                if (recurrence == null) return ToolResult.fail("I couldn't understand that repeat schedule.")
            }
            if (at != null && recurrence != null) {
                var first = recurrence.firstOnOrAfter(at)
                if (!first.isAfter(now)) first = recurrence.nextAfter(now, at)
                at = first
            }
            if (at != null && !at.isAfter(now)) {
                return ToolResult.fail("${TimeFormat.dateTime(at, now).replaceFirstChar { it.uppercase() }} has already passed. When should I remind you instead?")
            }
            val place = args.string("place")
            val wantsReminder = args.bool("reminder") ?: true
            val atMillis = at?.let { time.toMillis(it) }
            val priority = when (args.string("priority")?.lowercase()) {
                "high", "important", "urgent" -> Priority.HIGH
                "low" -> Priority.LOW
                else -> Priority.NORMAL
            }
            val result = tasks.create(
                CreateRequest(
                    title = title,
                    description = args.string("description").orEmpty(),
                    dueAt = atMillis,
                    remindAt = if (wantsReminder && place == null) atMillis else null,
                    recurrence = recurrence,
                    priority = priority,
                    category = args.string("category"),
                    placeName = place,
                    transition = if (place != null) (if (args.bool("on_leave") == true) PlaceTransition.EXIT else PlaceTransition.ENTER) else null,
                ),
            )
            session.lastTaskId = result.task.id
            val quoted = "\"$title\""
            val repeat = recurrence?.let { " ${it.describe()}" }.orEmpty()
            val outcome = result.outcome
            if (place != null) {
                val verb = if (args.bool("on_leave") == true) "leave" else "reach"
                return when (outcome) {
                    is ScheduleOutcome.LocationArmed -> ToolResult.ok("Okay. I'll remind you about $quoted when you $verb $place.", verified = true)
                    is ScheduleOutcome.LocationPending -> ToolResult(
                        success = true,
                        message = "I've saved $quoted, but it isn't armed yet: ${outcome.reason} " +
                            "It will still show in your task list.",
                        verified = false,
                        suggestScreen = "places",
                    )
                    is ScheduleOutcome.Failed -> ToolResult(false, "I saved $quoted as a task, but couldn't set the location trigger: ${outcome.reason}", suggestScreen = "permissions")
                    else -> ToolResult.ok("Saved $quoted.")
                }
            }
            if (atMillis == null) {
                return ToolResult.ok("Added $quoted to your tasks. It has no due date yet.")
            }
            val whenSpoken = TimeFormat.dateTime(at!!, now)
            if (result.reminder == null) {
                return ToolResult.ok("Added $quoted, due $whenSpoken$repeat.")
            }
            if (outcome is ScheduleOutcome.Failed) {
                return ToolResult(false, "I saved $quoted as a task due $whenSpoken, but I couldn't schedule the reminder: ${outcome.reason}", suggestScreen = "permissions")
            }
            val offer = if (recurrence == null) learning?.observeReminder(title) else null
            val message = if (recurrence != null) {
                "Got it. I'll remind you about $quoted$repeat, starting $whenSpoken."
            } else {
                "Got it. I'll remind you about $quoted $whenSpoken."
            } + outcomeNote(outcome)
            return ToolResult(true, message, verified = outcome is ScheduleOutcome.Exact, offer = offer)
        }
    }

    // ------------------------------------------------------------ list

    private val list = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.LIST_TASKS,
            description = "List the user's tasks and reminders. Use for 'what do I have today', 'show my pending tasks', 'what reminders do I have tomorrow'.",
            params = listOf(
                ToolParam("filter", "string", "Which tasks to list.", enumValues = listOf("today", "tomorrow", "week", "pending", "overdue", "completed", "upcoming", "important", "recurring", "all")),
                ToolParam("date", "string", "A specific day instead of a filter: ISO date or natural language like 'monday'."),
            ),
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val today = time.today()
            val dateText = args.string("date")
            val filter = args.string("filter")?.lowercase() ?: if (dateText != null) "date" else "today"
            val items: List<Task>
            val heading: String
            var extra = ""
            when (filter) {
                "date" -> {
                    val day = parseWhen(dateText!!)?.toLocalDate() ?: return ToolResult.fail("I couldn't work out which day you mean by \"$dateText\".")
                    items = tasks.dueOn(day)
                    heading = TimeFormat.day(day, today)
                }
                "today" -> {
                    items = tasks.dueOn(today)
                    heading = "today"
                    val overdue = tasks.overdue().filter { t -> items.none { it.id == t.id } }
                    if (overdue.isNotEmpty()) {
                        extra = " You also have ${count(overdue.size, "overdue task")}: ${overdue.take(3).joinToString(", ") { it.title }}."
                    }
                }
                "tomorrow" -> {
                    items = tasks.dueOn(today.plusDays(1))
                    heading = "tomorrow"
                }
                "week", "upcoming" -> {
                    items = tasks.upcoming(7)
                    heading = "in the next 7 days"
                }
                "overdue" -> {
                    items = tasks.overdue()
                    heading = "overdue"
                }
                "completed" -> {
                    items = tasks.completed()
                    heading = "completed"
                }
                "important" -> {
                    items = tasks.important()
                    heading = "marked important"
                }
                "recurring" -> {
                    items = tasks.recurring()
                    heading = "recurring"
                }
                else -> {
                    items = tasks.pending()
                    heading = "pending"
                }
            }
            session.listedTaskIds = items.map { it.id }
            if (items.size == 1) session.lastTaskId = items[0].id
            if (items.isEmpty()) {
                val none = when (filter) {
                    "today", "tomorrow", "date" -> "You have nothing scheduled ${heading}."
                    "week", "upcoming" -> "Nothing is due in the next 7 days."
                    "overdue" -> "Nothing is overdue. Nice."
                    "completed" -> "No completed tasks yet."
                    "important" -> "You have no open tasks marked important."
                    "recurring" -> "You have no recurring tasks."
                    else -> "You have no pending tasks."
                }
                return ToolResult.ok(none + extra)
            }
            val shown = items.take(MAX_SPOKEN)
            val descriptions = shown.map { describe(it, filter) }
            val more = if (items.size > shown.size) ", and ${items.size - shown.size} more" else ""
            val intro = when (filter) {
                "today", "tomorrow", "date" -> "You have ${count(items.size, "thing")} $heading"
                "week", "upcoming" -> "You have ${count(items.size, "task")} due $heading"
                "overdue" -> "You have ${count(items.size, "overdue task")}"
                "completed" -> "You've completed ${count(items.size, "task")}"
                "important" -> "You have ${count(items.size, "important task")}"
                "recurring" -> "You have ${count(items.size, "recurring task")}"
                else -> "You have ${count(items.size, "pending task")}"
            }
            return ToolResult.ok("$intro: ${joinSpoken(descriptions)}$more.$extra")
        }

        private fun describe(t: Task, filter: String): String {
            val due = t.dueAt ?: return t.title
            val local = time.toLocal(due)
            return when (filter) {
                "today", "tomorrow", "date" -> "${t.title} at ${TimeFormat.time(local.toLocalTime())}"
                "completed" -> t.title
                else -> "${t.title}, ${TimeFormat.dateTime(local, time.now())}"
            }
        }
    }

    private fun count(n: Int, noun: String) = if (n == 1) "1 $noun" else "$n ${noun}s"

    private fun joinSpoken(items: List<String>): String = when (items.size) {
        0 -> ""
        1 -> items[0]
        else -> items.dropLast(1).joinToString("; ") + "; and " + items.last()
    }

    // ------------------------------------------------------------ complete

    private val complete = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.COMPLETE_TASK,
            description = "Mark a task as completed.",
            params = listOf(queryParam, idParam),
            allowedWhenLocked = true,
        )

        override suspend fun preview(args: Args, env: TurnEnv) = previewResolve(args)

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val task = when (val r = resolve(args)) {
                is Resolved.Found -> r.task
                is Resolved.Ask -> return ToolResult(false, r.prompt, expectReply = true)
                is Resolved.Fail -> return ToolResult.fail(r.message)
            }
            if (task.completed) return ToolResult.ok("\"${task.title}\" was already marked as done.")
            val updated = tasks.complete(task.id) ?: return ToolResult.fail("That task no longer exists.")
            session.lastTaskId = task.id
            return if (updated.recurrence != null && updated.dueAt != null) {
                ToolResult.ok("Done. I've ticked off \"${task.title}\" for now. The next one is ${whenPhrase(updated.dueAt)}.", verified = true)
            } else {
                ToolResult.ok("Nice work. \"${task.title}\" is marked as completed.", verified = true)
            }
        }
    }

    // ------------------------------------------------------------ postpone

    private val postpone = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.POSTPONE_TASK,
            description = "Postpone, snooze or reschedule a task's reminder, by a duration or to a new time.",
            params = listOf(
                queryParam,
                idParam,
                ToolParam("minutes", "integer", "How many minutes to postpone by."),
                ToolParam("to", "string", "New time: ISO local date-time or natural language ('tomorrow 9am')."),
                ToolParam("by", "string", "Duration or time in the user's words, e.g. 'one hour' or 'until 5pm'."),
            ),
            allowedWhenLocked = true,
            verification = "Reports the alarm registration result for the new time.",
        )

        override suspend fun preview(args: Args, env: TurnEnv): Preview {
            val p = previewResolve(args)
            if (p != Preview.Proceed) return p
            if (args.long("minutes") == null && args.string("to") == null && args.string("by") == null) {
                return Preview.Ask("by", "Sure. For how long, or until when?")
            }
            return Preview.Proceed
        }

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val task = when (val r = resolve(args)) {
                is Resolved.Found -> r.task
                is Resolved.Ask -> return ToolResult(false, r.prompt, expectReply = true)
                is Resolved.Fail -> return ToolResult.fail(r.message)
            }
            var minutes = args.long("minutes")
            var to: LocalDateTime? = args.string("to")?.let { parseWhen(it) ?: return ToolResult.fail("I couldn't work out the time \"$it\".") }
            val by = args.string("by")
            if (minutes == null && to == null && by != null) {
                val parsed = DateTimeParser.parse(by, time.now(), defaults())
                val duration = if (parsed.dateTime == null) DateTimeParser.parseDuration(by) else null
                when {
                    parsed.dateTime != null -> to = parsed.dateTime
                    duration != null -> minutes = maxOf(1, duration.toMinutes())
                    else -> return ToolResult.fail("I couldn't work out how long \"$by\" is.")
                }
            }
            if (minutes == null && to == null) return ToolResult(false, "For how long should I postpone it?", expectReply = true)
            if (to != null && !to.isAfter(time.now())) return ToolResult.fail("That time has already passed.")
            if (task.completed) return ToolResult.fail("\"${task.title}\" is already completed.")
            val result = tasks.postpone(task.id, byMinutes = minutes, toMillis = to?.let { time.toMillis(it) })
                ?: return ToolResult.fail("I couldn't postpone \"${task.title}\".")
            session.lastTaskId = task.id
            val at = result.reminder.triggerAt
            if (result.outcome is ScheduleOutcome.Failed || at == null) {
                val reason = (result.outcome as? ScheduleOutcome.Failed)?.reason ?: "no time was set"
                return ToolResult(false, "I moved \"${task.title}\", but couldn't schedule the new reminder: $reason", suggestScreen = "permissions")
            }
            return ToolResult(true, "Okay. I'll remind you about \"${task.title}\" ${whenPhrase(at)}." + outcomeNote(result.outcome), verified = result.outcome is ScheduleOutcome.Exact)
        }
    }

    // ------------------------------------------------------------ delete

    private val delete = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.DELETE_TASK,
            description = "Delete a task together with its reminders.",
            params = listOf(queryParam, idParam),
            confirmation = Confirmation.ALWAYS,
        )

        override suspend fun preview(args: Args, env: TurnEnv): Preview = when (val r = resolve(args, includeCompleted = true)) {
            is Resolved.Found -> Preview.Confirm("Delete \"${r.task.title}\" and its reminders?", args.with("task_id", r.task.id.toString()))
            is Resolved.Ask -> Preview.Ask("query", r.prompt)
            is Resolved.Fail -> Preview.Fail(r.message)
        }

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val task = when (val r = resolve(args, includeCompleted = true)) {
                is Resolved.Found -> r.task
                is Resolved.Ask -> return ToolResult(false, r.prompt, expectReply = true)
                is Resolved.Fail -> return ToolResult.fail(r.message)
            }
            val ok = tasks.delete(task.id)
            if (session.lastTaskId == task.id) session.lastTaskId = null
            return if (ok) ToolResult.ok("Deleted \"${task.title}\".", verified = true) else ToolResult.fail("That task no longer exists.")
        }
    }

    private val deleteCompleted = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.DELETE_COMPLETED,
            description = "Delete every completed task.",
            confirmation = Confirmation.ALWAYS,
        )

        override suspend fun preview(args: Args, env: TurnEnv): Preview {
            val n = tasks.completed().size
            return if (n == 0) Preview.Fail("There are no completed tasks to delete.") else Preview.Confirm("Delete all ${count(n, "completed task")}?")
        }

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val n = tasks.deleteCompleted()
            return ToolResult.ok("Deleted ${count(n, "completed task")}.", verified = true)
        }
    }

    // ------------------------------------------------------------ update

    private val update = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.UPDATE_TASK,
            description = "Edit a task: rename it, change priority, due date, description, category, notes, or add/complete a subtask.",
            params = listOf(
                queryParam,
                idParam,
                ToolParam("new_title", "string", "New title."),
                ToolParam("priority", "string", "New priority.", enumValues = listOf("low", "normal", "high")),
                ToolParam("due", "string", "New due date/time: ISO or natural language."),
                ToolParam("description", "string", "New description."),
                ToolParam("category", "string", "New category."),
                ToolParam("notes", "string", "Text to append to the task's notes."),
                ToolParam("add_subtask", "string", "Title of a subtask to add."),
                ToolParam("complete_subtask", "string", "Words identifying a subtask to tick off."),
            ),
        )

        override suspend fun preview(args: Args, env: TurnEnv) = previewResolve(args)

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            var task = when (val r = resolve(args)) {
                is Resolved.Found -> r.task
                is Resolved.Ask -> return ToolResult(false, r.prompt, expectReply = true)
                is Resolved.Fail -> return ToolResult.fail(r.message)
            }
            val changes = mutableListOf<String>()
            args.string("new_title")?.let { task = task.copy(title = cleanTitle(it)); changes += "renamed it to \"${task.title}\"" }
            args.string("priority")?.let {
                val p = when (it.lowercase()) {
                    "high", "important", "urgent" -> Priority.HIGH
                    "low" -> Priority.LOW
                    else -> Priority.NORMAL
                }
                task = task.copy(priority = p)
                changes += "set its priority to ${p.name.lowercase()}"
            }
            args.string("description")?.let { task = task.copy(description = it); changes += "updated the description" }
            args.string("category")?.let { task = task.copy(category = it); changes += "filed it under $it" }
            args.string("notes")?.let { task = task.copy(notes = if (task.notes.isBlank()) it else task.notes + "\n" + it); changes += "added a note" }
            args.string("add_subtask")?.let { task = task.copy(subtasks = task.subtasks + Subtask(cleanTitle(it))); changes += "added the subtask \"${cleanTitle(it)}\"" }
            args.string("complete_subtask")?.let { q ->
                val i = task.subtasks.indexOfFirst { !it.done && TaskService.score(TaskService.tokens(q), TaskService.tokens(it.title)) >= 0.5 }
                if (i < 0) return ToolResult.fail("I couldn't find a subtask matching \"$q\" in \"${task.title}\".")
                task = task.copy(subtasks = task.subtasks.mapIndexed { n, s -> if (n == i) s.copy(done = true) else s })
                changes += "ticked off \"${task.subtasks[i].title}\""
            }
            val dueText = args.string("due")
            var note = ""
            if (dueText != null) {
                val due = parseWhen(dueText) ?: return ToolResult.fail("I couldn't work out the time \"$dueText\".")
                if (!due.isAfter(time.now())) return ToolResult.fail("That time has already passed.")
                tasks.update(task)
                val moved = tasks.postpone(task.id, toMillis = time.toMillis(due))
                if (moved == null || moved.outcome is ScheduleOutcome.Failed) {
                    return ToolResult(false, "I saved the other changes, but couldn't schedule the reminder for the new time.", suggestScreen = "permissions")
                }
                note = outcomeNote(moved.outcome)
                changes += "moved it to ${TimeFormat.dateTime(due, time.now())}"
            } else {
                if (changes.isEmpty()) return ToolResult(false, "What would you like to change about \"${task.title}\"?", expectReply = true)
                tasks.update(task)
            }
            session.lastTaskId = task.id
            return ToolResult.ok("Okay, for \"${task.title}\" I ${joinAnd(changes)}.$note", verified = true)
        }

        private fun joinAnd(items: List<String>) = if (items.size <= 1) items.joinToString() else items.dropLast(1).joinToString(", ") + " and " + items.last()
    }

    // ------------------------------------------------------------ extra reminders

    private val addReminder = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.ADD_REMINDER,
            description = "Add an additional reminder to an existing task: either a time, or a saved place (a task can have several reminders).",
            params = listOf(
                queryParam,
                idParam,
                ToolParam("when", "string", "When to remind: ISO local date-time or natural language."),
                ToolParam("place", "string", "Saved place name, to remind on arriving there instead of at a time."),
                ToolParam("on_leave", "boolean", "For place reminders: true to fire when leaving instead of arriving."),
            ),
            allowedWhenLocked = true,
            verification = "Reports the alarm registration result.",
        )

        override suspend fun preview(args: Args, env: TurnEnv): Preview {
            if (args.string("when") == null && args.string("place") == null) return Preview.Ask("when", "When should I remind you?")
            return previewResolve(args)
        }

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val task = when (val r = resolve(args)) {
                is Resolved.Found -> r.task
                is Resolved.Ask -> return ToolResult(false, r.prompt, expectReply = true)
                is Resolved.Fail -> return ToolResult.fail(r.message)
            }
            val place = args.string("place")
            if (place != null) {
                val leave = args.bool("on_leave") == true
                val (_, outcome) = tasks.addLocationReminder(task.id, place, if (leave) PlaceTransition.EXIT else PlaceTransition.ENTER)
                session.lastTaskId = task.id
                val quoted = "\"${task.title}\""
                return when (outcome) {
                    is ScheduleOutcome.LocationArmed -> ToolResult.ok("Okay. I'll remind you about $quoted when you ${if (leave) "leave" else "reach"} $place.", verified = true)
                    is ScheduleOutcome.LocationPending -> ToolResult(true, "I've noted that for $quoted, but it isn't armed yet: ${outcome.reason}", verified = false, suggestScreen = "places")
                    is ScheduleOutcome.Failed -> ToolResult(false, "I couldn't set the location trigger for $quoted: ${outcome.reason}", suggestScreen = "permissions")
                    else -> ToolResult.ok("Saved.")
                }
            }
            val whenText = args.string("when") ?: return ToolResult(false, "When should I remind you?", expectReply = true)
            val at = parseWhen(whenText) ?: return ToolResult.fail("I couldn't work out the time \"$whenText\".")
            if (!at.isAfter(time.now())) return ToolResult.fail("That time has already passed.")
            val (_, outcome) = tasks.addReminder(task.id, time.toMillis(at))
            session.lastTaskId = task.id
            if (outcome is ScheduleOutcome.Failed) return ToolResult(false, "I couldn't schedule that reminder: ${outcome.reason}", suggestScreen = "permissions")
            return ToolResult(true, "Added another reminder for \"${task.title}\" ${TimeFormat.dateTime(at, time.now())}." + outcomeNote(outcome), verified = outcome is ScheduleOutcome.Exact)
        }
    }

    private val nag = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.SET_NAG,
            description = "Keep re-reminding about a task at an interval until it is marked completed ('remind me again if I haven't completed this').",
            params = listOf(queryParam, idParam, ToolParam("minutes", "integer", "Minutes between repeat reminders. Defaults to 30.")),
            allowedWhenLocked = true,
            verification = "Reports the alarm registration result.",
        )

        override suspend fun preview(args: Args, env: TurnEnv) = previewResolve(args)

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val task = when (val r = resolve(args)) {
                is Resolved.Found -> r.task
                is Resolved.Ask -> return ToolResult(false, r.prompt, expectReply = true)
                is Resolved.Fail -> return ToolResult.fail(r.message)
            }
            if (task.completed) return ToolResult.fail("\"${task.title}\" is already completed.")
            val minutes = (args.int("minutes") ?: 30).coerceIn(5, 24 * 60)
            val result = tasks.setNag(task.id, minutes) ?: return ToolResult.fail("I couldn't set that up.")
            session.lastTaskId = task.id
            if (result.outcome is ScheduleOutcome.Failed) {
                return ToolResult(false, "I couldn't schedule the repeat reminder: ${(result.outcome as ScheduleOutcome.Failed).reason}", suggestScreen = "permissions")
            }
            val every = TimeFormat.duration(java.time.Duration.ofMinutes(minutes.toLong()))
            return ToolResult.ok(
                "Okay. Once \"${task.title}\" comes up, I'll keep reminding you every $every until you mark it done, up to ${TaskService.MAX_NAGS} times." + outcomeNote(result.outcome),
                verified = result.outcome is ScheduleOutcome.Exact,
            )
        }
    }

    private val locationReminders = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.LIST_LOCATION_REMINDERS,
            description = "List active location-based reminders (place triggers).",
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val items = tasks.locationReminders()
            if (items.isEmpty()) return ToolResult.ok("You have no location reminders.")
            session.listedTaskIds = items.map { it.second.id }
            val text = items.take(MAX_SPOKEN).map { (r, t) ->
                "${t.title} when you ${if (r.transition == PlaceTransition.EXIT) "leave" else "reach"} ${r.placeName}"
            }
            return ToolResult.ok("You have ${count(items.size, "location reminder")}: ${joinSpoken(text)}.")
        }
    }

    companion object {
        const val MAX_SPOKEN = 6
    }
}
