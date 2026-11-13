package com.lumi.assistant.data

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.lumi.core.agent.AssistantSettings
import com.lumi.core.ai.AiConfig
import com.lumi.core.ai.AiPresets
import com.lumi.core.ai.ProviderType
import com.lumi.core.briefing.BriefingOptions
import com.lumi.core.memory.MemoryPolicy
import com.lumi.core.model.MemoryCategory
import com.lumi.core.notify.NotificationPrefs
import com.lumi.core.time.TimeDefaults
import com.lumi.core.tools.LearningSwitch
import com.lumi.core.tools.ToolIds
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.security.KeyStore
import java.time.LocalTime
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Stores secrets (the AI provider key, integration tokens) encrypted with an AES-GCM key that
 * lives in the Android Keystore and never leaves it. Only ciphertext is written to preferences.
 */
class SecureStore(context: Context) {
    private val prefs: SharedPreferences = context.applicationContext.getSharedPreferences("lumi_secure", Context.MODE_PRIVATE)

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    /** Returns false when the secret could not be stored; the caller must tell the user. */
    fun put(name: String, value: String): Boolean {
        if (value.isEmpty()) {
            prefs.edit().remove(name).apply()
            return true
        }
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, key())
            val data = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
            val packed = Base64.encodeToString(cipher.iv, Base64.NO_WRAP) + ":" + Base64.encodeToString(data, Base64.NO_WRAP)
            prefs.edit().putString(name, packed).apply()
            true
        } catch (e: Exception) {
            false
        }
    }

    fun get(name: String): String? {
        val packed = prefs.getString(name, null) ?: return null
        return try {
            val parts = packed.split(":")
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)))
            String(cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), Charsets.UTF_8)
        } catch (e: Exception) {
            null
        }
    }

    fun has(name: String): Boolean = prefs.contains(name)

    companion object {
        private const val ALIAS = "lumi_secret_key"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val AI_KEY = "ai_api_key"
    }
}

/** All user-adjustable behaviour. Backed by SharedPreferences; secrets go through [SecureStore]. */
class Settings(context: Context, private val secure: SecureStore) : AssistantSettings, MemoryPolicy, LearningSwitch {
    private val prefs: SharedPreferences = context.applicationContext.getSharedPreferences("lumi_settings", Context.MODE_PRIVATE)

    private val _version = MutableStateFlow(0)
    /** Bumped on every change so Compose screens recompose. */
    val version: StateFlow<Int> = _version

    private fun changed() {
        _version.value = _version.value + 1
    }

    private fun bool(key: String, default: Boolean) = prefs.getBoolean(key, default)
    private fun setBool(key: String, value: Boolean) {
        prefs.edit().putBoolean(key, value).apply()
        changed()
    }
    private fun int(key: String, default: Int) = prefs.getInt(key, default)
    private fun setInt(key: String, value: Int) {
        prefs.edit().putInt(key, value).apply()
        changed()
    }
    private fun str(key: String, default: String) = prefs.getString(key, default) ?: default
    private fun setStr(key: String, value: String) {
        prefs.edit().putString(key, value).apply()
        changed()
    }
    private fun set(key: String): Set<String> = prefs.getStringSet(key, emptySet())?.toSet() ?: emptySet()
    private fun setSet(key: String, value: Set<String>) {
        prefs.edit().putStringSet(key, HashSet(value)).apply()
        changed()
    }

    // ---- assistant behaviour
    var confirmCalls: Boolean
        get() = bool("confirm_calls", true)
        set(v) = setBool("confirm_calls", v)
    var continuousConversation: Boolean
        get() = bool("continuous", true)
        set(v) = setBool("continuous", v)
    var speakReplies: Boolean
        get() = bool("speak_replies", true)
        set(v) = setBool("speak_replies", v)
    var onboardingOffered: Boolean
        get() = bool("onboarding_offered", false)
        set(v) = setBool("onboarding_offered", v)

    override fun confirmationEnabled(toolId: String): Boolean = when (toolId) {
        ToolIds.CALL -> confirmCalls
        else -> true
    }

    // ---- time defaults
    var defaultReminderMinute: Int
        get() = int("default_time", 9 * 60)
        set(v) = setInt("default_time", v)

    override fun timeDefaults(): TimeDefaults = defaultReminderMinute.let { TimeDefaults(defaultTime = LocalTime.of(it / 60, it % 60)) }

    // ---- voice
    /** 0..4, where 2 is normal speed. */
    var speechRateStep: Int
        get() = int("speech_rate", 2)
        set(v) = setInt("speech_rate", v.coerceIn(0, 4))
    val speechRate: Float get() = floatArrayOf(0.7f, 0.85f, 1.0f, 1.2f, 1.4f)[speechRateStep.coerceIn(0, 4)]
    var preferOfflineRecognition: Boolean
        get() = bool("offline_stt", false)
        set(v) = setBool("offline_stt", v)
    var speechLanguage: String
        get() = str("speech_language", "")
        set(v) = setStr("speech_language", v)

    // ---- memory and learning
    override fun learningEnabled(): Boolean = bool("learning", true)
    override fun setLearningEnabled(enabled: Boolean) = setBool("learning", enabled)
    override fun categoryEnabled(category: MemoryCategory): Boolean = bool("mem_" + category.name, true)
    fun setCategoryEnabled(category: MemoryCategory, enabled: Boolean) = setBool("mem_" + category.name, enabled)
    val historyEnabled: Boolean get() = categoryEnabled(MemoryCategory.HISTORY)

    // ---- notifications
    var notificationPrefs: NotificationPrefs
        get() = NotificationPrefs(
            enabled = bool("notif_enabled", false),
            paused = bool("notif_paused", false),
            selectedApps = set("notif_selected"),
            ignoredApps = set("notif_ignored"),
            quietStart = int("notif_quiet_start", 22 * 60),
            quietEnd = int("notif_quiet_end", 7 * 60),
            announceImportant = bool("notif_announce", false),
            hideSensitive = bool("notif_hide_sensitive", true),
        )
        set(v) {
            prefs.edit()
                .putBoolean("notif_enabled", v.enabled)
                .putBoolean("notif_paused", v.paused)
                .putStringSet("notif_selected", HashSet(v.selectedApps))
                .putStringSet("notif_ignored", HashSet(v.ignoredApps))
                .putInt("notif_quiet_start", v.quietStart)
                .putInt("notif_quiet_end", v.quietEnd)
                .putBoolean("notif_announce", v.announceImportant)
                .putBoolean("notif_hide_sensitive", v.hideSensitive)
                .apply()
            changed()
        }

    // ---- screen awareness (off until the user opts in)
    var screenAwareness: Boolean
        get() = bool("screen_awareness", false)
        set(v) = setBool("screen_awareness", v)

    // ---- daily briefing
    var briefingEnabled: Boolean
        get() = bool("briefing_enabled", false)
        set(v) = setBool("briefing_enabled", v)
    var briefingMinute: Int
        get() = int("briefing_minute", 7 * 60 + 30)
        set(v) = setInt("briefing_minute", v)
    var briefingSpoken: Boolean
        get() = bool("briefing_spoken", false)
        set(v) = setBool("briefing_spoken", v)
    var briefingOptions: BriefingOptions
        get() = BriefingOptions(bool("brief_tasks", true), bool("brief_calendar", true), bool("brief_notifications", true), bool("brief_battery", false))
        set(v) {
            prefs.edit().putBoolean("brief_tasks", v.tasks).putBoolean("brief_calendar", v.calendar)
                .putBoolean("brief_notifications", v.notifications).putBoolean("brief_battery", v.battery).apply()
            changed()
        }

    // ---- AI provider
    var aiPresetId: String
        get() = str("ai_preset", "")
        set(v) = setStr("ai_preset", v)
    var aiType: ProviderType
        get() = ProviderType.values().firstOrNull { it.name == str("ai_type", "") } ?: ProviderType.OPENAI_COMPATIBLE
        set(v) = setStr("ai_type", v.name)
    var aiBaseUrl: String
        get() = str("ai_base_url", "")
        set(v) = setStr("ai_base_url", v.trim())
    var aiModel: String
        get() = str("ai_model", "")
        set(v) = setStr("ai_model", v.trim())
    var aiEnabled: Boolean
        get() = bool("ai_enabled", true)
        set(v) = setBool("ai_enabled", v)

    val hasAiKey: Boolean get() = secure.has(SecureStore.AI_KEY)

    fun setAiKey(key: String): Boolean = secure.put(SecureStore.AI_KEY, key.trim()).also { changed() }

    fun applyPreset(id: String) {
        val preset = AiPresets.byId(id) ?: return
        prefs.edit().putString("ai_preset", preset.id).putString("ai_type", preset.type.name)
            .putString("ai_base_url", preset.baseUrl).putString("ai_model", preset.model).apply()
        changed()
    }

    /** The usable AI configuration, or null when the user has not set one up (Lumi then works offline). */
    fun aiConfig(): AiConfig? {
        if (!aiEnabled) return null
        val url = aiBaseUrl
        val model = aiModel
        if (url.isBlank() || model.isBlank()) return null
        val key = secure.get(SecureStore.AI_KEY) ?: return null
        if (key.isBlank()) return null
        return AiConfig(aiType, url, model, key)
    }
}
