package com.lumi.assistant.ui

import android.content.Intent
import android.net.Uri
import android.provider.Settings as SystemSettings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.lumi.assistant.Graph
import com.lumi.assistant.data.LogEntry
import com.lumi.core.ai.AiPresets
import com.lumi.core.ai.AiResult
import com.lumi.core.ai.ChatMessage
import com.lumi.core.ai.ProviderType
import com.lumi.core.ai.Role as ChatRole
import com.lumi.core.model.Memory
import com.lumi.core.model.MemoryCategory
import com.lumi.core.model.Place
import com.lumi.core.model.Reminder
import com.lumi.core.model.Task
import com.lumi.core.model.now
import com.lumi.core.model.toLocal
import com.lumi.core.time.TimeFormat
import com.lumi.core.tools.TurnEnv
import kotlinx.coroutines.launch

// ---------------------------------------------------------------------------------------- tasks

@Composable
fun TasksScreen(graph: Graph) {
    val scope = rememberCoroutineScope()
    val changes by graph.db.changes.collectAsState()
    var pending by remember { mutableStateOf(emptyList<Task>()) }
    var done by remember { mutableStateOf(emptyList<Task>()) }
    var reminders by remember { mutableStateOf(emptyMap<Long, List<Reminder>>()) }
    var command by remember { mutableStateOf("") }
    var reply by remember { mutableStateOf("") }
    var working by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf<Task?>(null) }

    LaunchedEffect(changes) {
        val open = graph.tasks.pending()
        pending = open
        done = graph.tasks.completed().take(30)
        reminders = open.associate { it.id to graph.tasks.remindersFor(it.id).filter { r -> r.status == com.lumi.core.model.ReminderStatus.SCHEDULED } }
    }

    val submit: () -> Unit = {
        val text = command.trim()
        if (text.isNotEmpty() && !working) {
            working = true
            command = ""
            scope.launch {
                reply = try {
                    graph.orchestrator.handle(text, TurnEnv(deviceLocked = false, voice = false)).text
                } catch (e: Exception) {
                    "That didn't work: ${e.message ?: "unknown error"}."
                }
                working = false
            }
        }
    }

    OutlinedTextField(
        value = command,
        onValueChange = { command = it },
        label = { Text("Tell Lumi what to add or change") },
        placeholder = { Text("Remind me to call mom tomorrow at 5") },
        singleLine = true,
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
        keyboardActions = KeyboardActions(onSend = { submit() }),
        modifier = Modifier.fillMaxWidth(),
    )
    Button(onClick = submit, enabled = !working && command.isNotBlank()) { Text(if (working) "Working" else "Send") }
    if (reply.isNotBlank()) Text(reply, style = MaterialTheme.typography.bodyMedium)

    SectionTitle("To do (${pending.size})")
    if (pending.isEmpty()) Text("No open tasks. Add one above, or just tell Lumi.", style = MaterialTheme.typography.bodyMedium)
    for (task in pending) {
        TaskRow(
            title = task.title,
            details = (listOf(Describe.due(graph, task)) + reminders[task.id].orEmpty().map { "Reminder: " + Describe.reminder(graph, it) }).filter { it.isNotBlank() },
            checked = false,
            onCheck = {
                scope.launch {
                    graph.tasks.complete(task.id)
                    graph.notifier.cancel(task.id)
                }
            },
            onDelete = { confirmDelete = task },
        )
    }

    if (done.isNotEmpty()) {
        SectionTitle("Done")
        for (task in done) {
            TaskRow(
                title = task.title,
                details = listOfNotNull(task.completedAt?.let { "Completed " + TimeFormat.dateTime(graph.time.toLocal(it), graph.time.now()) }),
                checked = true,
                onCheck = { scope.launch { graph.tasks.reopen(task.id) } },
                onDelete = { confirmDelete = task },
            )
        }
        TextButton(onClick = { scope.launch { graph.tasks.deleteCompleted() } }) { Text("Delete all completed tasks") }
    }

    val target = confirmDelete
    if (target != null) {
        AlertDialog(
            onDismissRequest = { confirmDelete = null },
            title = { Text("Delete this task?") },
            text = { Text("\"${target.title}\" and its reminders will be removed.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmDelete = null
                    scope.launch {
                        graph.tasks.delete(target.id)
                        graph.notifier.cancel(target.id)
                    }
                }) { Text("Delete") }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = null }) { Text("Keep") } },
        )
    }
}

@Composable
private fun TaskRow(title: String, details: List<String>, checked: Boolean, onCheck: () -> Unit, onDelete: () -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 56.dp), verticalAlignment = Alignment.CenterVertically) {
        Checkbox(checked = checked, onCheckedChange = { onCheck() })
        Column(Modifier.weight(1f).padding(horizontal = 8.dp)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            for (line in details) Text(line, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        TextButton(onClick = onDelete) { Text("Delete") }
    }
}

// ---------------------------------------------------------------------------------------- memory

@Composable
fun MemoryScreen(graph: Graph) {
    val scope = rememberCoroutineScope()
    val changes by graph.db.changes.collectAsState()
    val version by graph.settings.version.collectAsState()
    var refresh by remember { mutableIntStateOf(0) }
    var memories by remember { mutableStateOf(emptyList<Memory>()) }
    var confirmClear by remember { mutableStateOf<MemoryCategory?>(null) }
    var cleared by remember { mutableStateOf("") }

    LaunchedEffect(changes, version, refresh) { memories = graph.memory.all() }

    Text(
        "Everything Lumi remembers is stored only on this phone. Remove anything here, or say \"forget that\".",
        style = MaterialTheme.typography.bodyMedium,
    )
    SwitchRow(
        title = "Learn from how I use Lumi",
        subtitle = "When off, Lumi only saves what you explicitly ask it to remember.",
        checked = version >= 0 && graph.settings.learningEnabled(),
    ) { graph.settings.setLearningEnabled(it) }

    for (category in MemoryCategory.values()) {
        val enabled = version >= 0 && graph.settings.categoryEnabled(category)
        val items = memories.filter { it.category == category }
        HorizontalDivider()
        SwitchRow(title = category.label, subtitle = if (enabled) "${items.size} saved" else "Off: nothing is saved or used", checked = enabled) {
            graph.settings.setCategoryEnabled(category, it)
        }
        if (enabled) {
            for (memory in items) {
                Row(Modifier.fillMaxWidth().heightIn(min = 48.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(memory.content, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f).padding(end = 8.dp))
                    TextButton(onClick = {
                        scope.launch {
                            graph.memory.forget(memory.id)
                            refresh++
                        }
                    }) { Text("Forget") }
                }
            }
            if (items.isNotEmpty()) TextButton(onClick = { confirmClear = category }) { Text("Forget all in this group") }
        }
    }

    HorizontalDivider()
    SectionTitle("Other stored data")
    OutlinedButton(onClick = {
        scope.launch {
            graph.history.clear()
            cleared = "Conversation history cleared."
        }
    }) { Text("Clear conversation history") }
    OutlinedButton(onClick = {
        scope.launch {
            graph.patterns.clear()
            cleared = "Observed patterns cleared."
        }
    }) { Text("Clear observed patterns") }
    if (cleared.isNotBlank()) Text(cleared, style = MaterialTheme.typography.bodyMedium)

    val target = confirmClear
    if (target != null) {
        AlertDialog(
            onDismissRequest = { confirmClear = null },
            title = { Text("Forget this group?") },
            text = { Text("Everything under \"${target.label}\" will be deleted.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmClear = null
                    scope.launch {
                        graph.memory.clear(target)
                        refresh++
                    }
                }) { Text("Forget all") }
            },
            dismissButton = { TextButton(onClick = { confirmClear = null }) { Text("Keep") } },
        )
    }
}

// ---------------------------------------------------------------------------------------- permissions

@Composable
fun PermissionsScreen(graph: Graph, resumes: Int) {
    val context = LocalContext.current
    val version by graph.settings.version.collectAsState()
    var refresh by remember { mutableIntStateOf(0) }
    var discloseAccessibility by remember { mutableStateOf(false) }
    var note by remember { mutableStateOf("") }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        refresh++
        note = if (result.values.any { !it }) "Android didn't grant that. If no dialog appeared, allow it from Lumi's app settings below." else ""
    }
    val items = remember(resumes, refresh, version) { PermissionCatalog.items(context, graph) }

    Text(
        "Lumi asks only for what a feature needs, and works without any of the optional ones. Nothing here makes Lumi listen or run in the background.",
        style = MaterialTheme.typography.bodyMedium,
    )
    if (note.isNotBlank()) Text(note, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.error)

    for (item in items) {
        Card(modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(item.title, style = MaterialTheme.typography.titleMedium)
                Text(
                    if (item.granted) "Allowed" else if (item.core) "Not allowed (recommended)" else "Not allowed (optional)",
                    style = MaterialTheme.typography.labelLarge,
                    color = if (item.granted) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Text(item.why, style = MaterialTheme.typography.bodyMedium)
                val open: () -> Unit = {
                    if (item.runtime.isNotEmpty()) {
                        launcher.launch(item.runtime.toTypedArray())
                    } else {
                        val intent = item.settingsIntent
                        if (intent == null || !context.safeStart(Intent(intent))) {
                            context.safeStart(Intent(SystemSettings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", context.packageName, null)))
                        }
                    }
                }
                if (item.id == "accessibility") {
                    SwitchRow(
                        title = "Use screen awareness",
                        subtitle = "Lumi's own switch. Off means Lumi never looks at the screen, even if the service is enabled.",
                        checked = version >= 0 && graph.settings.screenAwareness,
                    ) { on -> if (on) discloseAccessibility = true else graph.settings.screenAwareness = false }
                    if (!item.granted && graph.settings.screenAwareness) OutlinedButton(onClick = open) { Text("Open accessibility settings") }
                } else if (item.id == "listener") {
                    SwitchRow(
                        title = "Let Lumi read notifications when I ask",
                        checked = version >= 0 && graph.settings.notificationPrefs.enabled,
                    ) { on -> graph.settings.notificationPrefs = graph.settings.notificationPrefs.copy(enabled = on) }
                    OutlinedButton(onClick = open) { Text(if (item.granted) "Manage notification access" else "Grant notification access") }
                } else if (!item.granted) {
                    Button(onClick = open) { Text(if (item.runtime.isNotEmpty()) "Allow" else "Open settings") }
                } else if (item.settingsIntent != null) {
                    TextButton(onClick = open) { Text("Change in settings") }
                }
            }
        }
    }
    OutlinedButton(onClick = {
        context.safeStart(Intent(SystemSettings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", context.packageName, null)))
    }) { Text("Open Lumi's app settings") }

    if (discloseAccessibility) {
        AlertDialog(
            onDismissRequest = { discloseAccessibility = false },
            title = { Text("Screen awareness") },
            text = {
                Text(
                    "This uses Android's Accessibility service. When you ask (for example \"what does this error mean\" or \"scroll down\"), " +
                        "Lumi reads the text of the app on screen and can scroll, tap or type for you.\n\n" +
                        "Lumi does not monitor your screen in the background, never reads password fields, and stores none of it. " +
                        "If you use an AI provider, the visible text is sent to it only for the summary or explanation you asked for.",
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    discloseAccessibility = false
                    graph.settings.screenAwareness = true
                    context.safeStart(Intent(SystemSettings.ACTION_ACCESSIBILITY_SETTINGS))
                }) { Text("I agree, continue") }
            },
            dismissButton = { TextButton(onClick = { discloseAccessibility = false }) { Text("Not now") } },
        )
    }
}

// ---------------------------------------------------------------------------------------- places

@Composable
fun PlacesScreen(graph: Graph, resumes: Int) {
    val scope = rememberCoroutineScope()
    val changes by graph.db.changes.collectAsState()
    var refresh by remember { mutableIntStateOf(0) }
    var places by remember { mutableStateOf(emptyList<Place>()) }
    var waiting by remember { mutableStateOf(emptyList<String>()) }
    var name by remember { mutableStateOf("") }
    var message by remember { mutableStateOf("") }
    var working by remember { mutableStateOf(false) }

    LaunchedEffect(changes, refresh, resumes) {
        places = graph.places.all()
        waiting = graph.tasks.locationReminders().map { (reminder, task) -> "${task.title}: ${Describe.reminder(graph, reminder)}" }
    }

    Text(
        "Save where you are now under a name, then say things like \"remind me to submit the form when I reach college\". " +
            "Android watches for arrival; Lumi doesn't track your location.",
        style = MaterialTheme.typography.bodyMedium,
    )
    OutlinedTextField(
        value = name,
        onValueChange = { name = it },
        label = { Text("Name for where you are now") },
        placeholder = { Text("College") },
        singleLine = true,
        modifier = Modifier.fillMaxWidth(),
    )
    Button(
        enabled = name.isNotBlank() && !working,
        onClick = {
            working = true
            val wanted = name
            scope.launch {
                val result = try {
                    graph.device.saveCurrentPlace(wanted)
                } catch (e: Exception) {
                    null
                }
                message = result?.message ?: "Couldn't save this place."
                if (result?.success == true) name = ""
                working = false
                refresh++
            }
        },
    ) { Text(if (working) "Finding your location" else "Save current location") }
    if (message.isNotBlank()) Text(message, style = MaterialTheme.typography.bodyMedium)

    SectionTitle("Saved places")
    if (places.isEmpty()) Text("No places saved yet.", style = MaterialTheme.typography.bodyMedium)
    for (place in places) {
        Row(Modifier.fillMaxWidth().heightIn(min = 56.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(place.name, style = MaterialTheme.typography.bodyLarge)
                Text("Within about ${place.radiusMeters.toInt()} metres", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            TextButton(onClick = {
                scope.launch {
                    graph.places.delete(place.id)
                    refresh++
                }
            }) { Text("Delete") }
        }
    }

    if (waiting.isNotEmpty()) {
        SectionTitle("Reminders tied to places")
        for (line in waiting) Text(line, style = MaterialTheme.typography.bodyMedium)
    }
}

// ---------------------------------------------------------------------------------------- settings

@Composable
fun SettingsScreen(graph: Graph) {
    val scope = rememberCoroutineScope()
    val version by graph.settings.version.collectAsState()
    val s = graph.settings
    // Reading `version` makes every row below refresh after a change.
    val live = version >= 0

    SectionTitle("Conversation")
    SwitchRow("Speak replies", "Off shows answers on screen only.", live && s.speakReplies) { s.speakReplies = it }
    SwitchRow("Keep listening after an answer", "Lumi listens once more for a follow-up, then closes if you say nothing.", live && s.continuousConversation) { s.continuousConversation = it }
    SwitchRow("Confirm before calling", "Lumi asks \"Call Dad?\" and waits for yes.", live && s.confirmCalls) { s.confirmCalls = it }
    SwitchRow("Prefer on-device speech recognition", "Works offline if your phone has the language pack; may be less accurate.", live && s.preferOfflineRecognition) { s.preferOfflineRecognition = it }
    StepperRow(
        title = "Speaking speed",
        value = listOf("Slowest", "Slow", "Normal", "Fast", "Fastest")[s.speechRateStep.coerceIn(0, 4)],
        onMinus = { s.speechRateStep = s.speechRateStep - 1 },
        onPlus = { s.speechRateStep = s.speechRateStep + 1 },
        minusLabel = "Slower",
        plusLabel = "Faster",
    )
    StepperRow(
        title = "Default reminder time",
        value = "Used for \"remind me tomorrow\": " + Describe.clock(s.defaultReminderMinute),
        onMinus = { s.defaultReminderMinute = (s.defaultReminderMinute + 1410) % 1440 },
        onPlus = { s.defaultReminderMinute = (s.defaultReminderMinute + 30) % 1440 },
    )

    HorizontalDivider()
    SectionTitle("Notifications")
    val prefs = s.notificationPrefs
    SwitchRow("Read notifications when I ask", "Needs notification access (see Permissions).", live && prefs.enabled) { s.notificationPrefs = prefs.copy(enabled = it) }
    SwitchRow("Skip codes and banking details", "One-time passwords and similar are never read aloud.", prefs.hideSensitive, enabled = prefs.enabled) { s.notificationPrefs = prefs.copy(hideSensitive = it) }
    SwitchRow("Announce important notifications", "Off by default. Speaks calls-to-action and messages as they arrive.", prefs.announceImportant, enabled = prefs.enabled) { s.notificationPrefs = prefs.copy(announceImportant = it) }
    StepperRow(
        title = "Quiet hours start",
        value = if (prefs.quietStart == prefs.quietEnd) "No quiet hours" else Describe.clock(prefs.quietStart),
        onMinus = { s.notificationPrefs = prefs.copy(quietStart = (prefs.quietStart + 1410) % 1440) },
        onPlus = { s.notificationPrefs = prefs.copy(quietStart = (prefs.quietStart + 30) % 1440) },
        enabled = prefs.enabled && prefs.announceImportant,
    )
    StepperRow(
        title = "Quiet hours end",
        value = if (prefs.quietStart == prefs.quietEnd) "No quiet hours" else Describe.clock(prefs.quietEnd),
        onMinus = { s.notificationPrefs = prefs.copy(quietEnd = (prefs.quietEnd + 1410) % 1440) },
        onPlus = { s.notificationPrefs = prefs.copy(quietEnd = (prefs.quietEnd + 30) % 1440) },
        enabled = prefs.enabled && prefs.announceImportant,
    )
    if (prefs.ignoredApps.isNotEmpty()) {
        Text("Ignored apps: ${prefs.ignoredApps.size}", style = MaterialTheme.typography.bodyMedium)
        TextButton(onClick = { s.notificationPrefs = prefs.copy(ignoredApps = emptySet()) }) { Text("Stop ignoring all apps") }
    }

    HorizontalDivider()
    SectionTitle("Daily briefing")
    val reschedule = { graph.briefings.reschedule(s.briefingEnabled, s.briefingMinute) }
    SwitchRow("Daily briefing", "One notification a day with your tasks and events. Tap it to hear it.", live && s.briefingEnabled) {
        s.briefingEnabled = it
        reschedule()
    }
    StepperRow(
        title = "Briefing time",
        value = Describe.clock(s.briefingMinute),
        onMinus = {
            s.briefingMinute = (s.briefingMinute + 1410) % 1440
            reschedule()
        },
        onPlus = {
            s.briefingMinute = (s.briefingMinute + 30) % 1440
            reschedule()
        },
        enabled = s.briefingEnabled,
    )
    val options = s.briefingOptions
    SwitchRow("Include tasks", checked = options.tasks, enabled = s.briefingEnabled) { s.briefingOptions = options.copy(tasks = it) }
    SwitchRow("Include calendar", checked = options.calendar, enabled = s.briefingEnabled) { s.briefingOptions = options.copy(calendar = it) }
    SwitchRow("Include notification summary", checked = options.notifications, enabled = s.briefingEnabled) { s.briefingOptions = options.copy(notifications = it) }
    SwitchRow("Include battery level", checked = options.battery, enabled = s.briefingEnabled) { s.briefingOptions = options.copy(battery = it) }

    HorizontalDivider()
    SectionTitle("AI provider (optional)")
    Text(
        "Reminders, tasks, calls, messages, music and apps work without any AI. Add a provider for open questions, summaries and rewriting. " +
            "Your key is encrypted with the phone's keystore and sent only to the address below.",
        style = MaterialTheme.typography.bodyMedium,
    )
    SwitchRow("Use AI provider", checked = live && s.aiEnabled) { s.aiEnabled = it }

    var baseUrl by remember { mutableStateOf(s.aiBaseUrl) }
    var model by remember { mutableStateOf(s.aiModel) }
    var key by remember { mutableStateOf("") }
    var status by remember { mutableStateOf("") }
    var testing by remember { mutableStateOf(false) }

    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        for (preset in AiPresets.all) {
            FilterChip(
                selected = live && s.aiPresetId == preset.id,
                onClick = {
                    s.applyPreset(preset.id)
                    baseUrl = s.aiBaseUrl
                    model = s.aiModel
                    status = ""
                },
                label = { Text(preset.label) },
            )
        }
    }
    if (s.aiPresetId == "custom") {
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            for (type in ProviderType.values()) {
                FilterChip(selected = live && s.aiType == type, onClick = { s.aiType = type }, label = { Text(type.name.lowercase().replace('_', ' ')) })
            }
        }
    }
    OutlinedTextField(value = baseUrl, onValueChange = { baseUrl = it }, label = { Text("Base URL (https)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
    OutlinedTextField(value = model, onValueChange = { model = it }, label = { Text("Model") }, singleLine = true, modifier = Modifier.fillMaxWidth())
    OutlinedTextField(
        value = key,
        onValueChange = { key = it },
        label = { Text(if (live && s.hasAiKey) "API key (saved; type to replace)" else "API key") },
        singleLine = true,
        visualTransformation = PasswordVisualTransformation(),
        modifier = Modifier.fillMaxWidth(),
    )
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = {
            s.aiBaseUrl = baseUrl
            s.aiModel = model
            status = when {
                baseUrl.isNotBlank() && !baseUrl.trim().startsWith("https://") -> "Saved, but Lumi only connects to https addresses."
                key.isNotBlank() && !s.setAiKey(key) -> "The phone's keystore refused to store the key."
                else -> "Saved."
            }
            key = ""
        }) { Text("Save") }
        OutlinedButton(
            enabled = !testing,
            onClick = {
                val client = graph.aiClient()
                if (client == null) {
                    status = "Not configured yet: save a base URL, model and key, and keep the switch on."
                } else {
                    testing = true
                    status = "Testing"
                    scope.launch {
                        val result = client.chat(listOf(ChatMessage(ChatRole.USER, "Reply with the single word: ready")), emptyList())
                        status = when (result) {
                            is AiResult.Ok -> "Connected. The model answered: ${result.text.take(60)}"
                            is AiResult.Error -> "Failed: ${result.message}"
                        }
                        testing = false
                    }
                }
            },
        ) { Text("Test connection") }
    }
    if (status.isNotBlank()) Text(status, style = MaterialTheme.typography.bodyMedium)
}

// ---------------------------------------------------------------------------------------- diagnostics

@Composable
fun DiagnosticsScreen(graph: Graph, resumes: Int) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var refresh by remember { mutableIntStateOf(0) }
    var lines by remember { mutableStateOf(emptyList<Diagnostics.Line>()) }
    var log by remember { mutableStateOf(emptyList<LogEntry>()) }
    var message by remember { mutableStateOf("") }

    LaunchedEffect(resumes, refresh) {
        lines = Diagnostics.lines(context, graph)
        log = graph.log.recent(60)
    }

    for (line in lines) {
        Row(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
            Text(line.label, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
            Text(
                (if (line.ok) "" else "Check: ") + line.value,
                style = MaterialTheme.typography.bodyMedium,
                color = if (line.ok) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.error,
                modifier = Modifier.weight(1.4f),
            )
        }
    }

    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = {
            scope.launch {
                message = try {
                    graph.restoreSchedules("manual check")
                    "Reminders re-registered with Android."
                } catch (e: Exception) {
                    "Could not re-register: ${e.message}"
                }
                refresh++
            }
        }) { Text("Re-arm reminders") }
        OutlinedButton(onClick = {
            graph.speech.speak("This is Lumi. Speech output is working.")
            refresh++
        }) { Text("Test voice") }
    }
    if (message.isNotBlank()) Text(message, style = MaterialTheme.typography.bodyMedium)

    HorizontalDivider()
    SectionTitle("Recent activity")
    Text("Private content such as messages and screen text is never written here.", style = MaterialTheme.typography.bodySmall)
    if (log.isEmpty()) Text("Nothing logged yet.", style = MaterialTheme.typography.bodyMedium)
    for (entry in log) {
        Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
            Text(
                TimeFormat.dateTime(graph.time.toLocal(entry.at), graph.time.now()) + " · " + entry.tag + (if (entry.success) "" else " · failed"),
                style = MaterialTheme.typography.labelMedium,
                color = if (entry.success) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.error,
            )
            if (entry.message.isNotBlank()) Text(entry.message, style = MaterialTheme.typography.bodySmall)
        }
    }
    if (log.isNotEmpty()) {
        TextButton(onClick = {
            scope.launch {
                graph.log.clear()
                refresh++
            }
        }) { Text("Clear activity log") }
    }
}
