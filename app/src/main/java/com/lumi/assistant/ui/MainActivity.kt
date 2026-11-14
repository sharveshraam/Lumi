package com.lumi.assistant.ui

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.lumi.assistant.Graph
import com.lumi.assistant.LumiApp
import com.lumi.assistant.assistant.AssistActivity
import com.lumi.core.model.Task
import com.lumi.core.model.today

/** The full-screen app: management screens for everything the voice assistant does. */
class MainActivity : ComponentActivity() {
    private val screen = mutableStateOf("home")
    private val resumes = mutableIntStateOf(0)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val graph = LumiApp.graph(this)
        screen.value = Screens.valid(savedInstanceState?.getString(EXTRA_SCREEN) ?: intent?.getStringExtra(EXTRA_SCREEN))
        setContent {
            LumiTheme {
                LumiScaffold(graph, screen.value, resumes.intValue) { screen.value = it }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        intent.getStringExtra(EXTRA_SCREEN)?.let { screen.value = Screens.valid(it) }
    }

    override fun onResume() {
        super.onResume()
        // Permissions and system settings may have changed while we were away.
        resumes.intValue = resumes.intValue + 1
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(EXTRA_SCREEN, screen.value)
    }

    companion object {
        const val EXTRA_SCREEN = "screen"
    }
}

object Screens {
    val titles = linkedMapOf(
        "home" to "Lumi",
        "tasks" to "Tasks and reminders",
        "memory" to "Memory",
        "permissions" to "Permissions",
        "places" to "Places",
        "settings" to "Settings",
        "diagnostics" to "Diagnostics",
    )

    fun valid(name: String?): String = if (name != null && titles.containsKey(name)) name else "home"
}

@Composable
fun LumiTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val colors = when {
        Build.VERSION.SDK_INT >= 31 -> if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        dark -> darkColorScheme()
        else -> lightColorScheme()
    }
    MaterialTheme(colorScheme = colors, content = content)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LumiScaffold(graph: Graph, screen: String, resumes: Int, navigate: (String) -> Unit) {
    BackHandler(enabled = screen != "home") { navigate("home") }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(Screens.titles[screen] ?: "Lumi", modifier = Modifier.semantics { heading() }) },
                navigationIcon = {
                    if (screen != "home") {
                        IconButton(onClick = { navigate("home") }) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back to home")
                        }
                    }
                },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            when (screen) {
                "tasks" -> TasksScreen(graph)
                "memory" -> MemoryScreen(graph)
                "permissions" -> PermissionsScreen(graph, resumes)
                "places" -> PlacesScreen(graph, resumes)
                "settings" -> SettingsScreen(graph)
                "diagnostics" -> DiagnosticsScreen(graph, resumes)
                else -> HomeScreen(graph, resumes, navigate)
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

fun Context.launchAssistant() {
    startActivity(Intent(this, AssistActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
}

fun Context.safeStart(intent: Intent): Boolean = try {
    startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    true
} catch (e: Exception) {
    false
}

@Composable
fun HomeScreen(graph: Graph, resumes: Int, navigate: (String) -> Unit) {
    val context = LocalContext.current
    val changes by graph.db.changes.collectAsState()
    var today by remember { mutableStateOf(emptyList<Task>()) }
    var overdue by remember { mutableStateOf(0) }
    var isAssistant by remember { mutableStateOf(true) }
    LaunchedEffect(changes, resumes) {
        today = graph.tasks.dueOn(graph.time.today())
        overdue = graph.tasks.overdue().size
        isAssistant = PermissionCatalog.isDefaultAssistant(context)
    }

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Talk to Lumi", style = MaterialTheme.typography.titleMedium)
            Text(
                if (isAssistant) "Hold the power or home button, or use your headset's assistant button. You can also start here."
                else "Lumi is not your default assistant yet, so the power-button shortcut opens another app. You can still start it here.",
                style = MaterialTheme.typography.bodyMedium,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { context.launchAssistant() }) { Text("Start Lumi") }
                if (!isAssistant) {
                    OutlinedButton(onClick = { if (!context.safeStart(PermissionCatalog.assistantSettingsIntent())) navigate("permissions") }) { Text("Set as assistant") }
                }
            }
        }
    }

    Card(onClick = { navigate("tasks") }, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Today", style = MaterialTheme.typography.titleMedium)
            if (today.isEmpty() && overdue == 0) {
                Text("Nothing due today.", style = MaterialTheme.typography.bodyMedium)
            } else {
                for (task in today.take(5)) Text("• ${task.title}  ${Describe.due(graph, task)}", style = MaterialTheme.typography.bodyMedium)
                if (today.size > 5) Text("and ${today.size - 5} more", style = MaterialTheme.typography.bodySmall)
                if (overdue > 0) Text("$overdue overdue", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.error)
            }
        }
    }

    val descriptions = mapOf(
        "tasks" to "See, add, complete and delete tasks and reminders.",
        "memory" to "See and delete what Lumi remembers. Turn learning on or off.",
        "permissions" to "Choose what Lumi may use. Everything is optional.",
        "places" to "Saved places for reminders like \"when I reach college\".",
        "settings" to "Voice, notifications, daily briefing and AI provider.",
        "diagnostics" to "Check why something isn't working.",
    )
    for ((id, text) in descriptions) {
        Card(onClick = { navigate(id) }, modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp)) {
                Text(Screens.titles[id].orEmpty(), style = MaterialTheme.typography.titleMedium)
                Text(text, style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}

// ---------------------------------------------------------------------------------------- shared pieces

@Composable
fun SectionTitle(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.titleMedium,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(top = 8.dp).semantics { heading() },
    )
}

@Composable
fun SwitchRow(title: String, subtitle: String? = null, checked: Boolean, enabled: Boolean = true, onChange: (Boolean) -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(min = 56.dp)
            .toggleable(value = checked, enabled = enabled, role = Role.Switch, onValueChange = onChange),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f).padding(end = 12.dp)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            if (subtitle != null) Text(subtitle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Switch(checked = checked, onCheckedChange = null, enabled = enabled)
    }
}

/** A value with minus and plus buttons; used for times so no keyboard is needed. */
@Composable
fun StepperRow(title: String, value: String, onMinus: () -> Unit, onPlus: () -> Unit, enabled: Boolean = true, minusLabel: String = "Earlier", plusLabel: String = "Later") {
    Row(Modifier.fillMaxWidth().heightIn(min = 56.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(value, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        OutlinedButton(onClick = onMinus, enabled = enabled) { Text(minusLabel) }
        Spacer(Modifier.padding(4.dp))
        OutlinedButton(onClick = onPlus, enabled = enabled) { Text(plusLabel) }
    }
}
