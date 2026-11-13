package com.lumi.assistant.tools

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import com.lumi.assistant.services.LumiAccessibilityService
import com.lumi.assistant.services.LumiNotificationListener
import com.lumi.core.tools.Args
import com.lumi.core.tools.PermissionGate
import com.lumi.core.tools.Preview
import com.lumi.core.tools.Tool
import com.lumi.core.tools.ToolResult
import com.lumi.core.tools.ToolSpec
import com.lumi.core.tools.TurnEnv

/** Checks runtime permissions and the two special accesses Lumi can use. */
class AndroidPermissions(private val context: Context) : PermissionGate {
    fun granted(permission: String): Boolean = when (permission) {
        NOTIFICATION_ACCESS -> LumiNotificationListener.isEnabled(context)
        ACCESSIBILITY -> LumiAccessibilityService.isEnabled(context)
        else -> context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
    }

    override fun missing(permissions: List<String>): List<String> = permissions.filterNot { granted(it) }

    override fun label(permission: String): String = when (permission) {
        Manifest.permission.RECORD_AUDIO -> "microphone"
        Manifest.permission.READ_CONTACTS -> "contacts"
        Manifest.permission.CALL_PHONE -> "phone"
        Manifest.permission.READ_CALL_LOG -> "call log"
        Manifest.permission.READ_CALENDAR -> "calendar"
        Manifest.permission.SEND_SMS -> "SMS"
        Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION -> "location"
        "android.permission.ACCESS_BACKGROUND_LOCATION" -> "background location"
        "android.permission.POST_NOTIFICATIONS" -> "notifications"
        NOTIFICATION_ACCESS -> "notification access"
        ACCESSIBILITY -> "accessibility"
        else -> permission.substringAfterLast('.').lowercase().replace('_', ' ')
    }

    companion object {
        const val NOTIFICATION_ACCESS = "special:notification_access"
        const val ACCESSIBILITY = "special:accessibility"
    }
}

internal class SimpleTool(
    override val spec: ToolSpec,
    private val pre: (suspend (Args, TurnEnv) -> Preview)? = null,
    private val run: suspend (Args, TurnEnv) -> ToolResult,
) : Tool {
    override suspend fun preview(args: Args, env: TurnEnv): Preview = pre?.invoke(args, env) ?: Preview.Proceed
    override suspend fun execute(args: Args, env: TurnEnv): ToolResult = run(args, env)
}

data class AppHit(val packageName: String, val label: String)

/** Small helpers shared by the Android tool sets. */
class ToolSupport(val context: Context, val permissions: AndroidPermissions) {
    val pm: PackageManager get() = context.packageManager

    fun has(permission: String) = permissions.granted(permission)

    /** Starts an activity and reports whether Android accepted the request. */
    fun launch(intent: Intent): Boolean = try {
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        true
    } catch (e: Exception) {
        false
    }

    fun needs(permission: String, why: String) =
        ToolResult.fail("I need the ${permissions.label(permission)} permission $why. You can grant it in Lumi's Permissions screen.", suggestScreen = "permissions")

    fun digits(number: String): String = number.filter { it.isDigit() || it == '+' }

    fun spokenNumber(number: String): String = digits(number).toCharArray().joinToString(" ")

    /** Launchable apps, sorted by name. */
    fun installedApps(): List<AppHit> = try {
        pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0)
            .map { AppHit(it.activityInfo.packageName, it.loadLabel(pm).toString()) }
            .distinctBy { it.packageName }
            .sortedBy { it.label.lowercase() }
    } catch (e: Exception) {
        emptyList()
    }

    /** Finds an installed, launchable app by its spoken name. */
    fun findApp(spoken: String): AppHit? {
        val wanted = spoken.trim().lowercase().removeSuffix(" app").trim()
        if (wanted.isEmpty()) return null
        val apps = installedApps()
        val compact = wanted.replace(" ", "")
        return apps.firstOrNull { it.label.equals(wanted, ignoreCase = true) }
            ?: apps.firstOrNull { it.label.replace(" ", "").equals(compact, ignoreCase = true) }
            ?: apps.filter { it.label.lowercase().startsWith(wanted) }.minByOrNull { it.label.length }
            ?: apps.filter { it.label.lowercase().contains(wanted) }.minByOrNull { it.label.length }
            ?: apps.firstOrNull { it.packageName.substringAfterLast('.').equals(compact, ignoreCase = true) }
    }
}
