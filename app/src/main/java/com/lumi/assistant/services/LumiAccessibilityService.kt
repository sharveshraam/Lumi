package com.lumi.assistant.services

import android.accessibilityservice.AccessibilityService
import android.content.ComponentName
import android.content.Context
import android.os.Bundle
import android.provider.Settings as SystemSettings
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Optional screen awareness. The service subscribes to no accessibility events, so it does no
 * work at all until the user explicitly asks Lumi about the screen; then it reads the current
 * window once and stops. It never observes the screen continuously.
 */
class LumiAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        instance = this
        // Receive no events: content is only fetched on demand.
        serviceInfo = serviceInfo?.apply { eventTypes = 0 }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}

    override fun onInterrupt() {}

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    /** Visible text of the foreground window, top to bottom. Password fields are skipped. */
    fun readScreen(maxChars: Int = 4000): ScreenText? {
        val root = rootInActiveWindow ?: return null
        val lines = ArrayList<String>()
        var total = 0
        walk(root) { node ->
            if (total >= maxChars) return@walk false
            if (!node.isVisibleToUser || node.isPassword) return@walk true
            val text = (node.text ?: node.contentDescription)?.toString()?.trim().orEmpty()
            if (text.isNotEmpty() && lines.lastOrNull() != text) {
                lines += text
                total += text.length + 1
            }
            true
        }
        val pkg = root.packageName?.toString().orEmpty()
        return ScreenText(pkg, lines.joinToString("\n").take(maxChars))
    }

    /** Clicks the element whose label best matches [target], or the n-th clickable element. */
    fun click(target: String?, ordinal: Int?): String? {
        val root = rootInActiveWindow ?: return null
        val clickable = ArrayList<AccessibilityNodeInfo>()
        walk(root) { node ->
            if (node.isVisibleToUser && node.isClickable && node.isEnabled) clickable += node
            clickable.size < 300
        }
        val node = if (target != null) {
            val wanted = target.trim().lowercase()
            clickable.map { it to label(it).lowercase() }
                .filter { it.second.isNotEmpty() }
                .sortedBy { (_, l) -> if (l == wanted) 0 else if (l.startsWith(wanted)) 1 else 2 }
                .firstOrNull { (_, l) -> l.contains(wanted) }?.first
        } else {
            val labelled = clickable.filter { label(it).isNotEmpty() }
            if (ordinal == -1) labelled.lastOrNull() else labelled.getOrNull((ordinal ?: 1) - 1)
        } ?: return null
        val name = label(node)
        return if (node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) name.ifEmpty { "that" } else null
    }

    fun scroll(forward: Boolean): Boolean {
        val root = rootInActiveWindow ?: return false
        var target: AccessibilityNodeInfo? = null
        walk(root) { node ->
            if (node.isVisibleToUser && node.isScrollable) {
                target = node
                false
            } else {
                true
            }
        }
        val action = if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        return target?.performAction(action) == true
    }

    /** Puts [text] into the focused editable field, or the first editable one whose hint matches [field]. */
    fun setText(text: String, field: String? = null, append: Boolean = false): Boolean {
        val root = rootInActiveWindow ?: return false
        var node: AccessibilityNodeInfo? = if (field == null) root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable } else null
        if (node == null) {
            val wanted = field?.trim()?.lowercase()
            walk(root) { n ->
                if (n.isVisibleToUser && n.isEditable && !n.isPassword) {
                    val hint = (n.hintText ?: n.contentDescription ?: n.text)?.toString()?.lowercase().orEmpty()
                    if (wanted == null || hint.contains(wanted)) {
                        node = n
                        return@walk false
                    }
                }
                true
            }
        }
        val targetNode = node ?: return false
        if (targetNode.isPassword) return false
        val existing = if (append && targetNode.text != null && !targetNode.isShowingHintText) targetNode.text.toString() else ""
        val value = if (existing.isEmpty()) text else "$existing $text"
        val args = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, value) }
        return targetNode.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    fun back(): Boolean = performGlobalAction(GLOBAL_ACTION_BACK)
    fun home(): Boolean = performGlobalAction(GLOBAL_ACTION_HOME)
    fun recents(): Boolean = performGlobalAction(GLOBAL_ACTION_RECENTS)
    fun notifications(): Boolean = performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS)

    private fun label(node: AccessibilityNodeInfo): String {
        (node.text ?: node.contentDescription)?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { return it }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            (child.text ?: child.contentDescription)?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { return it }
        }
        return ""
    }

    /** Depth-first walk; [visit] returns false to stop. Bounded so a huge tree cannot stall the turn. */
    private fun walk(root: AccessibilityNodeInfo, visit: (AccessibilityNodeInfo) -> Boolean) {
        val stack = ArrayDeque<AccessibilityNodeInfo>()
        stack.addLast(root)
        var seen = 0
        while (stack.isNotEmpty() && seen < MAX_NODES) {
            val node = stack.removeLast()
            seen++
            if (!visit(node)) return
            for (i in node.childCount - 1 downTo 0) node.getChild(i)?.let { stack.addLast(it) }
        }
    }

    data class ScreenText(val packageName: String, val text: String)

    companion object {
        private const val MAX_NODES = 2500

        @Volatile
        var instance: LumiAccessibilityService? = null
            private set

        fun isEnabled(context: Context): Boolean {
            val flat = SystemSettings.Secure.getString(context.contentResolver, SystemSettings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
            val me = ComponentName(context, LumiAccessibilityService::class.java)
            return flat.split(":").any { ComponentName.unflattenFromString(it) == me }
        }
    }
}
