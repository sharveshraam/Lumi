package com.lumi.assistant.assistant

import android.Manifest
import android.app.Activity
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.lumi.assistant.Graph
import com.lumi.assistant.LumiApp
import com.lumi.assistant.R
import com.lumi.assistant.ui.MainActivity
import com.lumi.assistant.voice.AndroidSpeechToText
import com.lumi.assistant.voice.HeadsetMicRouter
import com.lumi.assistant.voice.SpeechToText
import com.lumi.core.agent.Reply
import com.lumi.core.tools.Directive
import com.lumi.core.tools.TurnEnv
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * The compact assistant panel. It opens over the current app, listens once, answers, and
 * goes away. The microphone is open only while the panel says "Listening".
 */
class AssistActivity : Activity(), SpeechToText.Listener {
    private lateinit var graph: Graph
    private lateinit var stt: AndroidSpeechToText
    private lateinit var headset: HeadsetMicRouter
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val main = Handler(Looper.getMainLooper())

    private lateinit var status: TextView
    private lateinit var heard: TextView
    private lateinit var answer: TextView
    private lateinit var shortcut: TextView
    private lateinit var micButton: TextView
    private lateinit var input: EditText

    private var listening = false
    private var busy = false
    private var silentTurns = 0
    private var lastReply: String? = null
    private var typed = false
    private var micAsked = false
    private var visible = false
    private var pendingStart = false
    private val finishLater = Runnable { finish() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        graph = LumiApp.graph(this)
        stt = AndroidSpeechToText(this, graph.settings)
        headset = HeadsetMicRouter(this)
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        window.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        setContentView(buildUi())
        pendingStart = true
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        graph.speech.stop()
        if (visible) start(intent) else pendingStart = true
    }

    private fun start(intent: Intent?) {
        main.removeCallbacks(finishLater)
        silentTurns = 0
        if (intent?.action == ACTION_BRIEFING) {
            busy = true
            setStatus(getString(R.string.assist_thinking))
            scope.launch {
                val text = try {
                    if (deviceLocked()) getString(R.string.assist_briefing_locked) else graph.device.briefingText()
                } catch (e: Exception) {
                    getString(R.string.assist_error)
                }
                busy = false
                deliver(Reply(text))
            }
            return
        }
        if (!graph.settings.onboardingOffered && !deviceLocked()) {
            graph.settings.onboardingOffered = true
            deliver(Reply(getString(R.string.assist_welcome), expectReply = true))
            return
        }
        listen()
    }

    // ------------------------------------------------------------------ UI

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density + 0.5f).toInt()

    private fun buildUi(): View {
        val night = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES
        val surface = if (night) Color.rgb(28, 27, 31) else Color.WHITE
        val onSurface = if (night) Color.rgb(230, 225, 229) else Color.rgb(28, 27, 31)
        val muted = if (night) Color.rgb(160, 156, 165) else Color.rgb(96, 93, 102)
        val accent = Color.rgb(103, 80, 164)

        val root = FrameLayout(this)
        root.setBackgroundColor(Color.argb(90, 0, 0, 0))
        root.setOnClickListener { finish() }

        val panel = LinearLayout(this)
        panel.orientation = LinearLayout.VERTICAL
        panel.setPadding(dp(20), dp(18), dp(20), dp(18))
        panel.isClickable = true
        panel.background = GradientDrawable().apply {
            setColor(surface)
            cornerRadii = floatArrayOf(dp(24).toFloat(), dp(24).toFloat(), dp(24).toFloat(), dp(24).toFloat(), 0f, 0f, 0f, 0f)
        }
        panel.fitsSystemWindows = true

        status = TextView(this).apply {
            textSize = 13f
            setTextColor(accent)
            typeface = Typeface.DEFAULT_BOLD
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        heard = TextView(this).apply {
            textSize = 15f
            setTextColor(muted)
            setPadding(0, dp(6), 0, 0)
            visibility = View.GONE
        }
        answer = TextView(this).apply {
            textSize = 19f
            setTextColor(onSurface)
            setLineSpacing(0f, 1.15f)
            setPadding(0, dp(8), 0, dp(4))
            setTextIsSelectable(true)
        }
        val scroll = ScrollView(this)
        scroll.addView(answer)

        shortcut = TextView(this).apply {
            textSize = 14f
            setTextColor(accent)
            typeface = Typeface.DEFAULT_BOLD
            setPadding(0, dp(10), 0, dp(10))
            visibility = View.GONE
            minHeight = dp(48)
            gravity = Gravity.CENTER_VERTICAL
        }

        val row = LinearLayout(this)
        row.orientation = LinearLayout.HORIZONTAL
        row.gravity = Gravity.CENTER_VERTICAL
        row.setPadding(0, dp(8), 0, 0)

        input = EditText(this).apply {
            hint = getString(R.string.assist_type_hint)
            textSize = 15f
            setTextColor(onSurface)
            setHintTextColor(muted)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
            imeOptions = EditorInfo.IME_ACTION_SEND
            maxLines = 1
            minHeight = dp(48)
            setOnEditorActionListener { _, actionId, _ ->
                if (actionId == EditorInfo.IME_ACTION_SEND || actionId == EditorInfo.IME_NULL) {
                    submitTyped()
                    true
                } else {
                    false
                }
            }
            setOnFocusChangeListener { _, focused ->
                if (focused) {
                    main.removeCallbacks(finishLater)
                    if (listening) stopListening(cancel = true)
                }
            }
        }
        micButton = TextView(this).apply {
            text = getString(R.string.assist_mic)
            contentDescription = getString(R.string.assist_mic_description)
            textSize = 14f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            minWidth = dp(72)
            minHeight = dp(48)
            setPadding(dp(16), 0, dp(16), 0)
            background = GradientDrawable().apply {
                setColor(accent)
                cornerRadius = dp(24).toFloat()
            }
            isClickable = true
            isFocusable = true
            setOnClickListener { onMicPressed() }
        }
        row.addView(input, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        row.addView(micButton, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { leftMargin = dp(12) })

        panel.addView(status)
        panel.addView(heard)
        panel.addView(scroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        panel.addView(shortcut)
        panel.addView(row)

        // Keep long answers from pushing the controls off screen.
        scroll.viewTreeObserver.addOnGlobalLayoutListener {
            val limit = resources.displayMetrics.heightPixels / 2
            val lp = scroll.layoutParams
            if (answer.height > limit && lp.height != limit) {
                lp.height = limit
                scroll.layoutParams = lp
            } else if (answer.height <= limit && lp.height != ViewGroup.LayoutParams.WRAP_CONTENT) {
                lp.height = ViewGroup.LayoutParams.WRAP_CONTENT
                scroll.layoutParams = lp
            }
        }

        root.addView(panel, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))
        return root
    }

    private fun setStatus(text: String) {
        status.text = text
    }

    private fun onMicPressed() {
        main.removeCallbacks(finishLater)
        when {
            listening -> stopListening(cancel = false)
            graph.speech.speaking -> {
                graph.speech.stop()
                listen()
            }
            !busy -> listen()
        }
    }

    private fun submitTyped() {
        val text = input.text.toString().trim()
        if (text.isEmpty() || busy) return
        input.setText("")
        (getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager).hideSoftInputFromWindow(input.windowToken, 0)
        input.clearFocus()
        typed = true
        graph.speech.stop()
        handle(text)
    }

    // ------------------------------------------------------------------ listening

    private fun listen() {
        if (isFinishing || busy || listening || !visible) return
        main.removeCallbacks(finishLater)
        if (!stt.hasMicPermission()) {
            if (!micAsked && !deviceLocked()) {
                micAsked = true
                requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_MIC)
            } else {
                setStatus(getString(R.string.assist_no_mic))
                showShortcut("permissions")
            }
            return
        }
        if (!stt.available) {
            setStatus(getString(R.string.assist_no_recognizer))
            return
        }
        listening = true
        typed = false
        headset.begin()
        setStatus(getString(R.string.assist_starting))
        micButton.text = getString(R.string.assist_stop)
        stt.start(this)
    }

    private fun stopListening(cancel: Boolean) {
        if (!listening) return
        if (cancel) {
            listening = false
            stt.cancel()
            headset.end()
            micButton.text = getString(R.string.assist_mic)
            setStatus(getString(R.string.assist_idle))
        } else {
            stt.finish()
        }
    }

    private fun listeningEnded() {
        listening = false
        headset.end()
        micButton.text = getString(R.string.assist_mic)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_MIC) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                listen()
            } else {
                setStatus(getString(R.string.assist_no_mic))
                showShortcut("permissions")
            }
        }
    }

    override fun onReady() {
        setStatus(getString(R.string.assist_listening))
    }

    override fun onPartial(text: String) {
        heard.visibility = View.VISIBLE
        heard.text = text
    }

    override fun onLevel(level: Float) {
        micButton.alpha = 0.7f + 0.3f * level.coerceIn(0f, 1f)
    }

    override fun onFinal(text: String) {
        listeningEnded()
        micButton.alpha = 1f
        silentTurns = 0
        handle(text)
    }

    override fun onError(message: String, silence: Boolean) {
        listeningEnded()
        micButton.alpha = 1f
        if (silence) {
            silentTurns++
            setStatus(getString(R.string.assist_idle))
            // Nobody is talking: close quickly instead of keeping the panel (and the screen) on.
            main.postDelayed(finishLater, if (graph.orchestrator.awaitingAnswer && silentTurns < 2) 20_000 else 6_000)
        } else {
            setStatus(message)
        }
    }

    // ------------------------------------------------------------------ turns

    private fun deviceLocked(): Boolean = (getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager).isDeviceLocked

    private fun handle(text: String) {
        if (busy) return
        busy = true
        main.removeCallbacks(finishLater)
        heard.visibility = View.VISIBLE
        heard.text = text
        shortcut.visibility = View.GONE
        setStatus(getString(R.string.assist_thinking))
        scope.launch {
            val reply = try {
                graph.orchestrator.handle(text, TurnEnv(deviceLocked = deviceLocked(), voice = !typed))
            } catch (e: Exception) {
                Reply(getString(R.string.assist_error), success = false)
            }
            busy = false
            deliver(reply)
        }
    }

    private fun deliver(reply: Reply) {
        if (isFinishing) return
        val directive = reply.directive
        when (directive) {
            Directive.StopSpeaking -> {
                graph.speech.stop()
                setStatus(getString(R.string.assist_idle))
                main.postDelayed(finishLater, 4_000)
                return
            }
            is Directive.SpeechRate -> graph.settings.speechRateStep = if (directive.change == 0) 2 else graph.settings.speechRateStep + directive.change
            else -> Unit
        }
        if (reply.text.isNotBlank()) {
            lastReply = reply.text
            answer.text = reply.text
        }
        setStatus(getString(R.string.app_name))
        reply.suggestScreen?.let { showShortcut(it) }

        val after: () -> Unit = {
            when {
                isFinishing || isDestroyed -> Unit
                // Another app is in front now: never reopen the microphone from behind it.
                !visible && directive != Directive.RequestUnlock && directive !is Directive.OpenScreen -> finish()
                directive is Directive.OpenScreen -> {
                    openScreen(directive.screen)
                }
                directive == Directive.RequestUnlock -> requestUnlock()
                directive == Directive.Dismiss -> main.postDelayed(finishLater, 400)
                reply.expectReply || (graph.settings.continuousConversation && reply.success && !typed) -> listen()
                else -> main.postDelayed(finishLater, 8_000)
            }
        }
        val speakable = reply.text.isNotBlank() && graph.settings.speakReplies
        if (speakable) {
            graph.speech.speak(reply.text) { main.post(after) }
            val problem = graph.speech.problem
            if (problem != null) setStatus(problem)
        } else {
            // Leave silent replies on screen long enough to read before moving on.
            main.postDelayed(after, if (reply.text.isBlank()) 0L else (1_200L + reply.text.length * 45L).coerceAtMost(9_000L))
        }
    }

    private fun showShortcut(screen: String) {
        shortcut.visibility = View.VISIBLE
        shortcut.text = getString(R.string.assist_open_screen, screen)
        shortcut.setOnClickListener { openScreen(screen) }
    }

    private fun openScreen(screen: String) {
        val keyguard = getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        val open = {
            startActivity(Intent(this, MainActivity::class.java).putExtra(MainActivity.EXTRA_SCREEN, screen).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP))
            finish()
        }
        if (keyguard.isKeyguardLocked) {
            keyguard.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() = open()
            })
        } else {
            open()
        }
    }

    private fun requestUnlock() {
        val keyguard = getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        setStatus(getString(R.string.assist_unlock))
        keyguard.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
            override fun onDismissSucceeded() {
                busy = true
                scope.launch {
                    val reply = try {
                        graph.orchestrator.resumeAfterUnlock(TurnEnv(deviceLocked = false, voice = !typed))
                    } catch (e: Exception) {
                        Reply(getString(R.string.assist_error), success = false)
                    }
                    busy = false
                    if (reply != null) deliver(reply) else main.postDelayed(finishLater, 1_000)
                }
            }

            override fun onDismissCancelled() {
                setStatus(getString(R.string.assist_unlock_cancelled))
                main.postDelayed(finishLater, 5_000)
            }

            override fun onDismissError() {
                setStatus(getString(R.string.assist_unlock_cancelled))
                main.postDelayed(finishLater, 5_000)
            }
        })
    }

    // ------------------------------------------------------------------ lifecycle

    override fun onStart() {
        super.onStart()
        visible = true
        if (pendingStart) {
            pendingStart = false
            start(intent)
        }
    }

    override fun onStop() {
        super.onStop()
        visible = false
        // Never keep the microphone open once the panel is not visible.
        if (listening) {
            listening = false
            stt.cancel()
            headset.end()
            micButton.text = getString(R.string.assist_mic)
        }
        if (!isChangingConfigurations && !busy && !graph.speech.speaking) finish()
    }

    override fun onDestroy() {
        main.removeCallbacksAndMessages(null)
        scope.cancel()
        stt.release()
        headset.end()
        if (isFinishing) {
            graph.speech.stop()
            graph.orchestrator.resetConversation()
        }
        super.onDestroy()
    }

    companion object {
        const val ACTION_BRIEFING = "com.lumi.assistant.action.BRIEFING"
        private const val REQUEST_MIC = 41
    }
}
