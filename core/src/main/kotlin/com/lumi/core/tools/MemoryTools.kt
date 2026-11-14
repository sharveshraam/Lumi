package com.lumi.core.tools

import com.lumi.core.ai.ToolParam
import com.lumi.core.memory.MemoryService
import com.lumi.core.memory.RememberResult
import com.lumi.core.model.Memory
import com.lumi.core.model.MemoryCategory

/** Switch for implicit learning, owned by app settings. */
interface LearningSwitch {
    fun setLearningEnabled(enabled: Boolean)
}

/** Voice-facing personal memory actions. */
class MemoryTools(
    private val memory: MemoryService,
    private val learningSwitch: LearningSwitch,
) {
    fun all(): List<Tool> = listOf(remember, recall, forget, preference, alias, learning)

    private fun disabled(category: MemoryCategory) =
        ToolResult.fail("Memory for \"${category.label}\" is switched off, so I didn't save that. You can turn it on in the Memory screen.", suggestScreen = "memory")

    private val remember = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.REMEMBER,
            description = "Store something the user explicitly asked to remember, or a routine/fact they stated about themselves.",
            params = listOf(
                ToolParam("content", "string", "The fact to remember, in the user's words.", required = true, prompt = "What should I remember?"),
                ToolParam("category", "string", "Kind of memory.", enumValues = listOf("explicit", "routine", "preference", "note")),
            ),
            allowedWhenLocked = true,
            verification = "Confirms the row was written to the local database.",
        )

        override suspend fun preview(args: Args, env: TurnEnv): Preview =
            if (args.string("content") == null) Preview.Ask("content", "What should I remember?") else Preview.Proceed

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val content = args.string("content") ?: return ToolResult(false, "What should I remember?", expectReply = true)
            val category = when (args.string("category")?.lowercase()) {
                "routine" -> MemoryCategory.ROUTINE
                "preference" -> MemoryCategory.PREFERENCE
                "note" -> MemoryCategory.NOTE
                else -> MemoryCategory.EXPLICIT
            }
            return when (val r = memory.remember(category, content.replaceFirstChar { it.uppercase() })) {
                is RememberResult.Saved -> ToolResult.ok(if (category == MemoryCategory.NOTE) "Noted: ${r.memory.content}" else "Okay, I'll remember that: ${r.memory.content}", verified = true)
                is RememberResult.CategoryDisabled -> disabled(r.category)
            }
        }
    }

    private val recall = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.RECALL,
            description = "Look up what Lumi remembers about the user. Use for 'what did I ask you to remember', 'what do you know about X'.",
            params = listOf(
                ToolParam("query", "string", "Topic to search for. Leave empty to list the things the user asked to remember."),
                ToolParam("category", "string", "Restrict to one kind of memory.", enumValues = listOf("explicit", "routine", "preference", "contacts", "apps", "notes", "all")),
            ),
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val query = args.string("query")
            val category = args.string("category")?.lowercase()
            val items: List<Memory> = when {
                query != null -> memory.search(query)
                category == "all" -> memory.all().filter { it.category != MemoryCategory.HISTORY }
                category == "routine" -> memory.byCategory(MemoryCategory.ROUTINE)
                category == "preference" -> memory.byCategory(MemoryCategory.PREFERENCE)
                category == "contacts" -> memory.byCategory(MemoryCategory.CONTACT_ALIAS)
                category == "apps" -> memory.byCategory(MemoryCategory.APP_PREFERENCE)
                category == "notes" -> memory.byCategory(MemoryCategory.NOTE)
                else -> memory.byCategory(MemoryCategory.EXPLICIT)
            }
            if (items.isEmpty()) {
                return ToolResult.ok(
                    if (query != null) "I don't have anything saved about \"$query\"."
                    else "You haven't asked me to remember anything yet. Just say \"remember that\" followed by the detail.",
                )
            }
            val shown = items.take(6).map { spoken(it) }
            val more = if (items.size > shown.size) " And ${items.size - shown.size} more in the Memory screen." else ""
            val intro = if (query != null) "Here's what I have" else if (category == null) "You asked me to remember" else "Here's what I know"
            return ToolResult.ok("$intro: ${shown.joinToString("; ")}.$more")
        }

        private fun spoken(m: Memory): String = when {
            m.category == MemoryCategory.CONTACT_ALIAS && m.key != null -> "${m.key.removePrefix("alias:")} means ${m.content}"
            m.key == MemoryService.KEY_NAME -> "you like to be called ${m.content}"
            m.key == MemoryService.KEY_MUSIC_APP -> "your music app is ${m.content}"
            else -> m.content.trimEnd('.')
        }
    }

    private val forget = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.FORGET,
            description = "Delete a stored memory.",
            params = listOf(
                ToolParam("query", "string", "Words identifying the memory to delete.", required = true, prompt = "What should I forget?"),
                ToolParam("memory_id", "integer", "Exact memory id, if known."),
            ),
            confirmation = Confirmation.ALWAYS,
            verification = "Confirms the row was deleted from the local database.",
        )

        private suspend fun find(args: Args): Memory? {
            args.long("memory_id")?.let { id -> memory.all().firstOrNull { it.id == id }?.let { return it } }
            val q = args.string("query") ?: return null
            return memory.search(q).firstOrNull()
        }

        override suspend fun preview(args: Args, env: TurnEnv): Preview {
            if (args.string("query") == null && args.long("memory_id") == null) return Preview.Ask("query", "What should I forget?")
            val m = find(args) ?: return Preview.Fail("I don't have anything saved about \"${args.string("query")}\".")
            return Preview.Confirm("Forget this: \"${m.content}\"?", args.with("memory_id", m.id.toString()))
        }

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val m = find(args) ?: return ToolResult.fail("I couldn't find that memory.")
            memory.forget(m.id)
            return ToolResult.ok("Forgotten.", verified = true)
        }
    }

    private val preference = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.SET_PREFERENCE,
            description = "Save a user preference such as their preferred name or default music app.",
            params = listOf(
                ToolParam("key", "string", "Preference name: preferred_name, music_app, or another short snake_case key.", required = true),
                ToolParam("value", "string", "The value.", required = true, prompt = "What should I set it to?"),
            ),
            allowedWhenLocked = true,
            verification = "Confirms the row was written to the local database.",
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val key = args.string("key")?.lowercase()?.replace(' ', '_') ?: return ToolResult.fail("I need to know which preference to set.")
            val value = args.string("value") ?: return ToolResult(false, "What should I set it to?", expectReply = true)
            val category = if (key == MemoryService.KEY_MUSIC_APP || key.endsWith("_app")) MemoryCategory.APP_PREFERENCE else MemoryCategory.PREFERENCE
            return when (val r = memory.remember(category, value, key)) {
                is RememberResult.Saved -> ToolResult.ok(
                    when (key) {
                        MemoryService.KEY_NAME -> "Got it. I'll call you $value."
                        MemoryService.KEY_MUSIC_APP -> "Okay, I'll use $value for music."
                        else -> "Saved. ${key.replace('_', ' ').replaceFirstChar { it.uppercase() }}: $value."
                    },
                    verified = true,
                )
                is RememberResult.CategoryDisabled -> disabled(r.category)
            }
        }
    }

    private val alias = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.SET_ALIAS,
            description = "Teach Lumi a nickname for a contact, e.g. alias 'dad' for the contact 'Ramesh Kumar'.",
            params = listOf(
                ToolParam("alias", "string", "The nickname the user says, e.g. 'dad'.", required = true, prompt = "What nickname should I learn?"),
                ToolParam("name", "string", "The contact's name as saved in the phone's contacts.", required = true, prompt = "Which contact is that?"),
            ),
            allowedWhenLocked = true,
            verification = "Confirms the row was written to the local database.",
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val alias = args.string("alias") ?: return ToolResult.fail("I need the nickname.")
            val name = args.string("name") ?: return ToolResult.fail("I need the contact's name.")
            return when (val r = memory.setAlias(alias, name)) {
                is RememberResult.Saved -> ToolResult.ok("Okay. When you say ${alias.lowercase().removePrefix("my ")}, I'll look for $name.", verified = true)
                is RememberResult.CategoryDisabled -> disabled(r.category)
            }
        }
    }

    private val learning = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.SET_LEARNING,
            description = "Turn Lumi's automatic learning of preferences and routines on or off.",
            params = listOf(ToolParam("enabled", "boolean", "true to enable learning, false to disable.", required = true)),
            allowedWhenLocked = false,
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val enabled = args.bool("enabled") ?: return ToolResult.fail("Should learning be on or off?")
            learningSwitch.setLearningEnabled(enabled)
            return ToolResult.ok(
                if (enabled) "Learning is on. I'll ask before turning anything I notice into a routine."
                else "Learning is off. I'll only remember things you explicitly tell me to.",
                verified = true,
            )
        }
    }
}
