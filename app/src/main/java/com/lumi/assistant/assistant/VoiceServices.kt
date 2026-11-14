package com.lumi.assistant.assistant

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.service.voice.VoiceInteractionService
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService
import android.speech.RecognitionService

/**
 * Makes Lumi selectable as the phone's digital assistant. The service itself is idle: Lumi
 * has no hotword detector and never listens in the background. It only runs when the user
 * invokes the assistant gesture (power-button long press, corner swipe, headset button).
 */
class LumiVoiceInteractionService : VoiceInteractionService()

class LumiSessionService : VoiceInteractionSessionService() {
    override fun onNewSession(args: Bundle?): VoiceInteractionSession = LumiSession(this)
}

/** One assistant invocation. It shows no window of its own; the compact UI is [AssistActivity]. */
class LumiSession(context: Context) : VoiceInteractionSession(context) {
    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        val intent = Intent(context, AssistActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        try {
            startAssistantActivity(intent)
        } catch (e: Exception) {
            context.startActivity(intent)
        }
        hide()
    }
}

/**
 * Android requires an assistant to declare a recognition service. Lumi uses the phone's own
 * speech recogniser instead of providing one, so this declines every request.
 */
class LumiRecognitionService : RecognitionService() {
    override fun onStartListening(recognizerIntent: Intent?, listener: Callback?) {
        try {
            listener?.error(android.speech.SpeechRecognizer.ERROR_CLIENT)
        } catch (e: Exception) {
            // The caller went away.
        }
    }

    override fun onCancel(listener: Callback?) = Unit

    override fun onStopListening(listener: Callback?) = Unit
}
