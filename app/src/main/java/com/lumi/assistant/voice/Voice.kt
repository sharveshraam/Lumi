package com.lumi.assistant.voice

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import com.lumi.assistant.data.Settings
import java.util.Locale

/** Speech-to-text provider. Implementations must be driven from the main thread. */
interface SpeechToText {
    interface Listener {
        fun onReady()
        fun onPartial(text: String)
        fun onFinal(text: String)
        /** [silence] is true when the user simply said nothing. */
        fun onError(message: String, silence: Boolean)
        fun onLevel(level: Float)
    }

    val available: Boolean
    fun start(listener: Listener)
    /** Stop capturing and deliver what was heard so far. */
    fun finish()
    /** Stop capturing and discard. */
    fun cancel()
    fun release()
}

/** Text-to-speech provider. */
interface SpeechSynth {
    val speaking: Boolean
    fun speak(text: String, onDone: () -> Unit = {})
    fun stop()
    fun release()
}

/** STT through the platform [SpeechRecognizer]. The microphone is only open between start() and a result. */
class AndroidSpeechToText(private val context: Context, private val settings: Settings) : SpeechToText {
    private var recognizer: SpeechRecognizer? = null
    private var listener: SpeechToText.Listener? = null
    private var active = false

    override val available: Boolean get() = isAvailable(context)

    fun hasMicPermission(): Boolean = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    override fun start(listener: SpeechToText.Listener) {
        if (!hasMicPermission()) {
            listener.onError("I need the microphone permission to listen. You can grant it in Lumi's Permissions screen, or type instead.", false)
            return
        }
        if (!available) {
            listener.onError("No speech recognition service is available on this phone. You can type instead.", false)
            return
        }
        cancel()
        this.listener = listener
        val r = try {
            create(context)
        } catch (e: Exception) {
            listener.onError("Speech recognition couldn't start (${e.message ?: e.javaClass.simpleName}).", false)
            return
        }
        recognizer = r
        r.setRecognitionListener(callbacks)
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            .putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
        if (settings.preferOfflineRecognition) intent.putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        settings.speechLanguage.takeIf { it.isNotBlank() }?.let { intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, it) }
        active = true
        try {
            r.startListening(intent)
        } catch (e: Exception) {
            active = false
            listener.onError("Speech recognition couldn't start (${e.message ?: e.javaClass.simpleName}).", false)
            destroy()
        }
    }

    override fun finish() {
        if (active) try { recognizer?.stopListening() } catch (e: Exception) { /* already stopped */ }
    }

    override fun cancel() {
        active = false
        listener = null
        try { recognizer?.cancel() } catch (e: Exception) { /* already stopped */ }
        destroy()
    }

    override fun release() = cancel()

    private fun destroy() {
        try { recognizer?.destroy() } catch (e: Exception) { /* nothing to free */ }
        recognizer = null
    }

    private fun done(block: (SpeechToText.Listener) -> Unit) {
        if (!active) return
        active = false
        val l = listener
        listener = null
        destroy()
        if (l != null) block(l)
    }

    private val callbacks = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) { if (active) listener?.onReady() }
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) { if (active) listener?.onLevel(((rmsdB + 2f) / 12f).coerceIn(0f, 1f)) }
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onEvent(eventType: Int, params: Bundle?) {}

        override fun onPartialResults(partialResults: Bundle?) {
            val text = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
            if (active && text.isNotBlank()) listener?.onPartial(text)
        }

        override fun onResults(results: Bundle?) {
            val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty().trim()
            done { if (text.isEmpty()) it.onError("I didn't catch that.", true) else it.onFinal(text) }
        }

        override fun onError(error: Int) {
            val (message, silence) = when (error) {
                SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "I didn't catch that." to true
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "I need the microphone permission to listen." to false
                SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT, SpeechRecognizer.ERROR_SERVER ->
                    "Speech recognition couldn't reach its service. If you're offline, install an offline speech language in your phone's voice input settings, or type instead." to false
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "The speech recogniser is busy. Try again in a moment." to false
                SpeechRecognizer.ERROR_AUDIO -> "I couldn't access the microphone. Another app may be using it." to false
                12, 13 -> "That speech language isn't available on this phone. Check the language in Voice settings." to false
                else -> "Speech recognition stopped unexpectedly (code $error)." to false
            }
            done { it.onError(message, silence) }
        }
    }

    companion object {
        /**
         * Recognition services other than Lumi's own placeholder. Lumi has to declare a
         * recognition service to be selectable as the assistant, and some Android versions then
         * make it the default recogniser, so the real one is picked explicitly.
         */
        private fun others(context: Context): List<android.content.ComponentName>? = try {
            context.packageManager.queryIntentServices(android.content.Intent(android.speech.RecognitionService.SERVICE_INTERFACE), 0)
                .mapNotNull { it.serviceInfo }
                .filter { it.packageName != context.packageName }
                .map { android.content.ComponentName(it.packageName, it.name) }
        } catch (e: Exception) {
            null
        }

        fun isAvailable(context: Context): Boolean = others(context)?.isNotEmpty() ?: SpeechRecognizer.isRecognitionAvailable(context)

        internal fun create(context: Context): SpeechRecognizer {
            val default = try {
                android.provider.Settings.Secure.getString(context.contentResolver, "voice_recognition_service")
            } catch (e: Exception) {
                null
            }
            val defaultIsOurs = default != null && default.startsWith(context.packageName + "/")
            if (!defaultIsOurs) return SpeechRecognizer.createSpeechRecognizer(context)
            val candidates = others(context).orEmpty()
            val preferred = listOf("com.google.android.googlequicksearchbox", "com.google.android.tts", "com.google.android.as")
            val pick = candidates.firstOrNull { it.packageName in preferred } ?: candidates.firstOrNull()
            return if (pick != null) SpeechRecognizer.createSpeechRecognizer(context, pick) else SpeechRecognizer.createSpeechRecognizer(context)
        }
    }
}

/**
 * TTS through the platform engine. Requests transient audio focus so music ducks while Lumi
 * speaks, and uses assistant audio attributes so output follows the active route (earbuds,
 * car, speaker).
 */
class AndroidSpeechSynth(private val context: Context, private val settings: Settings) : SpeechSynth {
    private val main = Handler(Looper.getMainLooper())
    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var tts: TextToSpeech? = null
    private var ready = false
    private var failed = false
    private var queued: Pair<String, () -> Unit>? = null
    private var onDone: (() -> Unit)? = null
    private var utterance = 0
    @Volatile private var active = false

    private val attributes: AudioAttributes = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANT)
        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
        .build()
    private val focusRequest: AudioFocusRequest = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
        .setAudioAttributes(attributes)
        .setOnAudioFocusChangeListener { change -> if (change == AudioManager.AUDIOFOCUS_LOSS) main.post { stop() } }
        .build()

    override val speaking: Boolean get() = active

    /** Null when the engine works; otherwise why speech output is unavailable. */
    var problem: String? = null
        private set

    private fun ensureEngine() {
        if (tts != null || failed) return
        tts = TextToSpeech(context.applicationContext) { status ->
            main.post {
                if (status == TextToSpeech.SUCCESS) {
                    ready = true
                    configure()
                    queued?.let { (text, done) ->
                        queued = null
                        speak(text, done)
                    }
                } else {
                    failed = true
                    problem = "No text-to-speech engine is available on this phone."
                    val done = queued?.second
                    queued = null
                    done?.invoke()
                }
            }
        }
    }

    private fun configure() {
        val engine = tts ?: return
        engine.setAudioAttributes(attributes)
        val locale = settings.speechLanguage.takeIf { it.isNotBlank() }?.let { Locale.forLanguageTag(it) } ?: Locale.getDefault()
        val result = engine.setLanguage(locale)
        if (result == TextToSpeech.LANG_MISSING_DATA || result == TextToSpeech.LANG_NOT_SUPPORTED) engine.setLanguage(Locale.US)
        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) {}
            override fun onDone(utteranceId: String?) = finished(utteranceId)
            @Deprecated("Deprecated in the platform API")
            override fun onError(utteranceId: String?) = finished(utteranceId)
            override fun onError(utteranceId: String?, errorCode: Int) = finished(utteranceId)
            override fun onStop(utteranceId: String?, interrupted: Boolean) = finished(utteranceId)
        })
    }

    private fun finished(id: String?) {
        main.post {
            if (id != utterance.toString()) return@post
            active = false
            audio.abandonAudioFocusRequest(focusRequest)
            val done = onDone
            onDone = null
            done?.invoke()
        }
    }

    override fun speak(text: String, onDone: () -> Unit) {
        val clean = text.trim()
        if (clean.isEmpty() || failed) {
            onDone()
            return
        }
        if (!ready) {
            queued = clean to onDone
            ensureEngine()
            return
        }
        val engine = tts ?: return onDone()
        // A new utterance replaces the current one; its callback is dropped, not invoked.
        this.onDone = onDone
        utterance++
        active = true
        engine.setSpeechRate(settings.speechRate)
        audio.requestAudioFocus(focusRequest)
        val chunks = split(clean)
        val id = utterance.toString()
        var result = TextToSpeech.SUCCESS
        chunks.forEachIndexed { i, chunk ->
            // Only the last chunk carries the id that completes the utterance.
            val r = engine.speak(chunk, if (i == 0) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD, null, if (i == chunks.lastIndex) id else "$id-part$i")
            if (r != TextToSpeech.SUCCESS) result = r
        }
        if (result != TextToSpeech.SUCCESS) finished(id)
    }

    /** The engine rejects very long inputs, so long replies are split at sentence boundaries. */
    private fun split(text: String): List<String> {
        val max = (TextToSpeech.getMaxSpeechInputLength() - 100).coerceIn(500, 3500)
        if (text.length <= max) return listOf(text)
        val out = ArrayList<String>()
        val current = StringBuilder()
        for (sentence in text.split(Regex("(?<=[.!?\\n])\\s+"))) {
            if (current.length + sentence.length + 1 > max && current.isNotEmpty()) {
                out += current.toString()
                current.setLength(0)
            }
            if (sentence.length > max) out += sentence.chunked(max) else current.append(sentence).append(' ')
        }
        if (current.isNotBlank()) out += current.toString().trim()
        return out
    }

    override fun stop() {
        queued = null
        onDone = null
        utterance++
        if (active) {
            active = false
            audio.abandonAudioFocusRequest(focusRequest)
        }
        try { tts?.stop() } catch (e: Exception) { /* engine already gone */ }
    }

    override fun release() {
        stop()
        try { tts?.shutdown() } catch (e: Exception) { /* engine already gone */ }
        tts = null
        ready = false
    }
}

/**
 * Optional routing of the microphone to a connected Bluetooth headset while Lumi listens.
 * Output routing needs nothing: Android already sends assistant speech to the active device.
 */
class HeadsetMicRouter(context: Context) {
    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var routed = false

    fun headsetConnected(): Boolean = audio.getDevices(AudioManager.GET_DEVICES_INPUTS).any { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || (Build.VERSION.SDK_INT >= 31 && it.type == AudioDeviceInfo.TYPE_BLE_HEADSET) }

    fun begin() {
        if (routed) return
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                val device = audio.availableCommunicationDevices.firstOrNull { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || it.type == AudioDeviceInfo.TYPE_BLE_HEADSET } ?: return
                routed = audio.setCommunicationDevice(device)
            } else if (headsetConnected()) {
                @Suppress("DEPRECATION")
                audio.startBluetoothSco()
                routed = true
            }
        } catch (e: Exception) {
            routed = false
        }
    }

    fun end() {
        if (!routed) return
        routed = false
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                audio.clearCommunicationDevice()
            } else {
                @Suppress("DEPRECATION")
                audio.stopBluetoothSco()
            }
        } catch (e: Exception) {
            // Routing is best effort.
        }
    }
}
