package com.lumi.assistant.tools

import android.Manifest
import android.app.KeyguardManager
import android.app.SearchManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.media.MediaMetadata
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.net.Uri
import android.os.BatteryManager
import android.provider.AlarmClock
import android.provider.CalendarContract
import android.provider.MediaStore
import android.provider.Settings as SystemSettings
import android.view.KeyEvent
import com.lumi.assistant.data.Settings
import com.lumi.assistant.reminders.GeofenceManager
import com.lumi.assistant.services.LumiAccessibilityService
import com.lumi.assistant.services.LumiNotificationListener
import com.lumi.core.ai.AiClient
import com.lumi.core.ai.AiResult
import com.lumi.core.ai.ChatMessage
import com.lumi.core.ai.Role
import com.lumi.core.ai.ToolParam
import com.lumi.core.briefing.BriefingComposer
import com.lumi.core.briefing.BriefingData
import com.lumi.core.memory.MemoryService
import com.lumi.core.model.MemoryCategory
import com.lumi.core.model.Place
import com.lumi.core.model.TimeSource
import com.lumi.core.model.now
import com.lumi.core.model.toLocal
import com.lumi.core.model.today
import com.lumi.core.notify.NotificationIntel
import com.lumi.core.tasks.PlaceStore
import com.lumi.core.tasks.TaskService
import com.lumi.core.time.DateTimeParser
import com.lumi.core.time.TimeFormat
import com.lumi.core.tools.Args
import com.lumi.core.tools.Confirmation
import com.lumi.core.tools.Directive
import com.lumi.core.tools.Preview
import com.lumi.core.tools.Tool
import com.lumi.core.tools.ToolIds
import com.lumi.core.tools.ToolResult
import com.lumi.core.tools.ToolSpec
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId

/**
 * Apps, media, clock, navigation, screen awareness and the daily briefing. When an action is
 * only handed to another app (clock, maps, music player) the reply says so.
 */
class DeviceTools(
    private val s: ToolSupport,
    private val settings: Settings,
    private val memory: MemoryService,
    private val tasks: TaskService,
    private val places: PlaceStore,
    private val time: TimeSource,
    private val geofences: GeofenceManager,
    private val ai: () -> AiClient?,
) {
    private val context: Context get() = s.context
    private val audio: AudioManager get() = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    fun all(): List<Tool> = listOf(
        openApp, openSettings, mediaControl, playMusic, nowPlaying, setVolume, navigate, setAlarm, setTimer, webSearch,
        readScreen, screenAction, fillField, briefing, battery, calendar, savePlace,
    )

    // ------------------------------------------------------------------ apps and settings

    private val openApp: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.OPEN_APP,
            description = "Open an installed app by name.",
            params = listOf(ToolParam("name", "string", "App name as the user says it.", required = true, prompt = "Which app should I open?")),
            verification = "Reports that the launch request was accepted by Android.",
        ),
        run = { args, _ ->
            val name = args.string("name").orEmpty()
            val hit = s.findApp(name)
            val intent = hit?.let { s.pm.getLaunchIntentForPackage(it.packageName) }
            when {
                hit == null || intent == null -> ToolResult.fail("I couldn't find an app called \"$name\" on this phone.")
                s.launch(intent) -> ToolResult(true, "Opening ${hit.label}.", verified = false, directive = Directive.Dismiss)
                else -> ToolResult.fail("Android wouldn't let me open ${hit.label}.")
            }
        },
    )

    private val openSettings: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.OPEN_SETTINGS,
            description = "Open a system settings page.",
            params = listOf(ToolParam("panel", "string", "Which page: wifi, bluetooth, display, sound, battery, location, notifications, apps, date, accessibility, data, storage, security.")),
        ),
        run = { args, _ ->
            val panel = args.string("panel")?.lowercase().orEmpty()
            val action = when {
                "wi" in panel -> SystemSettings.ACTION_WIFI_SETTINGS
                "bluetooth" in panel -> SystemSettings.ACTION_BLUETOOTH_SETTINGS
                "display" in panel || "bright" in panel -> SystemSettings.ACTION_DISPLAY_SETTINGS
                "sound" in panel || "volume" in panel -> SystemSettings.ACTION_SOUND_SETTINGS
                "battery" in panel -> SystemSettings.ACTION_BATTERY_SAVER_SETTINGS
                "location" in panel || "gps" in panel -> SystemSettings.ACTION_LOCATION_SOURCE_SETTINGS
                "notification" in panel -> SystemSettings.ACTION_NOTIFICATION_LISTENER_SETTINGS
                "app" in panel -> SystemSettings.ACTION_APPLICATION_SETTINGS
                "date" in panel || "time" in panel -> SystemSettings.ACTION_DATE_SETTINGS
                "accessib" in panel -> SystemSettings.ACTION_ACCESSIBILITY_SETTINGS
                "data" in panel || "mobile" in panel || "network" in panel -> SystemSettings.ACTION_DATA_ROAMING_SETTINGS
                "hotspot" in panel || "airplane" in panel || "flight" in panel -> SystemSettings.ACTION_WIRELESS_SETTINGS
                "storage" in panel -> SystemSettings.ACTION_INTERNAL_STORAGE_SETTINGS
                "security" in panel || "lock" in panel -> SystemSettings.ACTION_SECURITY_SETTINGS
                else -> SystemSettings.ACTION_SETTINGS
            }
            if (s.launch(Intent(action)) || s.launch(Intent(SystemSettings.ACTION_SETTINGS))) {
                ToolResult(true, if (panel.isEmpty()) "Opening settings." else "Opening $panel settings.", verified = false, directive = Directive.Dismiss)
            } else {
                ToolResult.fail("I couldn't open settings on this phone.")
            }
        },
    )

    // ------------------------------------------------------------------ media

    private fun mediaKey(code: Int) {
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
    }

    private val mediaControl: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.MEDIA_CONTROL,
            description = "Control whatever is playing: play, pause, next or previous track.",
            params = listOf(ToolParam("action", "string", "Playback action.", required = true, enumValues = listOf("play", "pause", "next", "previous"))),
            allowedWhenLocked = true,
            verification = "Checks afterwards whether audio is actually playing.",
        ),
        run = { args, _ ->
            when (args.string("action")) {
                "pause", "stop" -> {
                    if (!audio.isMusicActive) {
                        ToolResult.ok("Nothing is playing right now.", verified = true)
                    } else {
                        mediaKey(KeyEvent.KEYCODE_MEDIA_PAUSE)
                        delay(700)
                        if (audio.isMusicActive) ToolResult(true, "I sent pause, but something is still playing. That app may not respond to media buttons.", verified = false)
                        else ToolResult.ok("Paused.", verified = true)
                    }
                }
                "play" -> {
                    mediaKey(KeyEvent.KEYCODE_MEDIA_PLAY)
                    delay(1200)
                    if (audio.isMusicActive) ToolResult.ok("Playing.", verified = true)
                    else ToolResult(false, "I pressed play, but nothing started. Tell me what to play, for example: play my workout playlist.", expectReply = true)
                }
                "next" -> {
                    mediaKey(KeyEvent.KEYCODE_MEDIA_NEXT)
                    ToolResult(true, "Skipping.", verified = false)
                }
                "previous" -> {
                    mediaKey(KeyEvent.KEYCODE_MEDIA_PREVIOUS)
                    ToolResult(true, "Going back.", verified = false)
                }
                else -> ToolResult.fail("I can play, pause, skip or go back.")
            }
        },
    )

    private val playMusic: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.PLAY_MUSIC,
            description = "Play a song, artist, album or playlist by name in a music app (Spotify or the user's preferred app).",
            params = listOf(
                ToolParam("query", "string", "What to play, e.g. 'workout playlist' or 'Believer'.", required = true, prompt = "What would you like to hear?"),
                ToolParam("app", "string", "Music app name. Defaults to the user's preferred music app."),
            ),
            allowedWhenLocked = true,
            verification = "Checks afterwards whether audio started; otherwise says the request was only handed to the app.",
        ),
        run = { args, _ -> play(args) },
    )

    private suspend fun play(args: Args): ToolResult {
        val raw = args.string("query").orEmpty()
        val query = raw.removePrefix("my ").trim()
        val requested = args.string("app")
        val appName = requested ?: memory.preference(MemoryService.KEY_MUSIC_APP)
        val app = appName?.let { s.findApp(it) }
        if (requested != null && app == null) return ToolResult.fail("I couldn't find $requested on this phone.")
        val isPlaylist = "playlist" in query.lowercase()
        val search = Intent(MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH)
            .putExtra(SearchManager.QUERY, query)
            .putExtra(MediaStore.EXTRA_MEDIA_FOCUS, if (isPlaylist) "vnd.android.cursor.item/playlist" else "vnd.android.cursor.item/*")
        if (isPlaylist) search.putExtra("android.intent.extra.playlist", query.replace(Regex("\\s*playlist\\s*", RegexOption.IGNORE_CASE), " ").trim())
        if (app != null) search.setPackage(app.packageName)
        var started = s.launch(search)
        if (!started && app?.packageName == SPOTIFY) {
            started = s.launch(Intent(Intent.ACTION_VIEW, Uri.parse("spotify:search:" + Uri.encode(query))).setPackage(SPOTIFY))
        }
        if (!started && app != null) started = s.launch(search.setPackage(null))
        if (!started) return ToolResult.fail("No music app on this phone accepted a play request. Install or open one, then try again.")
        val where = app?.label ?: "your music app"
        delay(2500)
        return if (audio.isMusicActive) ToolResult.ok("Playing $query on $where.", verified = true)
        else ToolResult(true, "I asked $where to play $query. If it only shows search results, tap the one you want.", verified = false)
    }

    private val nowPlaying: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.NOW_PLAYING,
            description = "Say which song or media is currently playing, read from the active media session.",
            permissions = listOf(AndroidPermissions.NOTIFICATION_ACCESS),
            allowedWhenLocked = true,
            query = true,
        ),
        run = { _, _ ->
            try {
                val manager = context.getSystemService(Context.MEDIA_SESSION_SERVICE) as MediaSessionManager
                val sessions = manager.getActiveSessions(ComponentName(context, LumiNotificationListener::class.java))
                val controller = sessions.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING } ?: sessions.firstOrNull()
                val meta = controller?.metadata
                val title = meta?.getString(MediaMetadata.METADATA_KEY_TITLE)
                if (controller == null || meta == null || title.isNullOrBlank()) {
                    ToolResult.ok("Nothing seems to be playing.")
                } else {
                    val artist = meta.getString(MediaMetadata.METADATA_KEY_ARTIST) ?: meta.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST)
                    val state = if (controller.playbackState?.state == PlaybackState.STATE_PLAYING) "Playing" else "Paused on"
                    ToolResult.ok("$state $title" + (if (artist.isNullOrBlank()) "" else " by $artist") + ".", verified = true)
                }
            } catch (e: SecurityException) {
                s.needs(AndroidPermissions.NOTIFICATION_ACCESS, "to see what's playing")
            }
        },
    )

    private val setVolume: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.SET_VOLUME,
            description = "Change the media volume: up, down, mute, unmute, or an exact percentage.",
            params = listOf(
                ToolParam("direction", "string", "Relative change.", enumValues = listOf("up", "down", "mute", "unmute")),
                ToolParam("percent", "integer", "Exact level from 0 to 100."),
            ),
            allowedWhenLocked = true,
            verification = "Reads the volume back after changing it.",
        ),
        run = { args, _ -> volume(args) },
    )

    private fun volume(args: Args): ToolResult {
        val stream = AudioManager.STREAM_MUSIC
        val max = audio.getStreamMaxVolume(stream)
        val before = audio.getStreamVolume(stream)
        val percent = args.int("percent")
        val direction = args.string("direction")
        val step = maxOf(1, max / 7)
        try {
            when {
                percent != null -> audio.setStreamVolume(stream, Math.round(max * percent.coerceIn(0, 100) / 100f), 0)
                direction == "up" -> audio.setStreamVolume(stream, (before + step).coerceAtMost(max), 0)
                direction == "down" -> audio.setStreamVolume(stream, (before - step).coerceAtLeast(0), 0)
                direction == "mute" -> audio.adjustStreamVolume(stream, AudioManager.ADJUST_MUTE, 0)
                direction == "unmute" -> audio.adjustStreamVolume(stream, AudioManager.ADJUST_UNMUTE, 0)
                else -> return ToolResult(false, "Should I turn it up or down?", expectReply = true)
            }
        } catch (e: SecurityException) {
            return ToolResult.fail("Android blocked the volume change because Do Not Disturb is on.")
        }
        val after = audio.getStreamVolume(stream)
        val level = Math.round(after * 100f / max)
        return when {
            direction == "mute" -> ToolResult.ok("Muted.", verified = audio.isStreamMute(stream))
            percent == null && after == before && direction == "up" -> ToolResult.ok("Volume is already at maximum.", verified = true)
            percent == null && after == before && direction == "down" -> ToolResult.ok("Volume is already at zero.", verified = true)
            else -> ToolResult.ok("Volume is at $level percent.", verified = true)
        }
    }

    // ------------------------------------------------------------------ utilities

    private val navigate: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.NAVIGATE,
            description = "Start turn-by-turn navigation to a destination or a saved place such as 'home'.",
            params = listOf(ToolParam("destination", "string", "Address, place name, or saved place.", required = true, prompt = "Where to?")),
            verification = "Reports that the request was handed to the maps app.",
        ),
        run = { args, _ ->
            val destination = args.string("destination").orEmpty().removePrefix("my ").trim()
            val saved = places.find(destination)
            val target = saved?.let { "${it.latitude},${it.longitude}" } ?: destination
            val nav = Intent(Intent.ACTION_VIEW, Uri.parse("google.navigation:q=" + Uri.encode(target)))
            val geo = Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=" + Uri.encode(target)))
            if (s.launch(nav) || s.launch(geo)) {
                ToolResult(true, "Starting directions to $destination.", verified = false, directive = Directive.Dismiss)
            } else {
                ToolResult.fail("I couldn't find a maps app to start navigation.")
            }
        },
    )

    private val setAlarm: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.SET_ALARM,
            description = "Set an alarm in the phone's clock app.",
            params = listOf(
                ToolParam("hour", "integer", "Hour in 24-hour time.", required = true, prompt = "What time should the alarm be?"),
                ToolParam("minute", "integer", "Minute."),
                ToolParam("label", "string", "Optional label."),
            ),
            allowedWhenLocked = true,
            verification = "The clock app does not report back, so Lumi says the request was sent.",
        ),
        pre = { args, _ ->
            if (args.int("hour") != null) {
                Preview.Proceed
            } else {
                // The answer to "what time?" arrives as spoken text in the hour slot.
                val spoken = args.string("hour").orEmpty().trim()
                val parsed = if (spoken.isEmpty()) null else DateTimeParser.parse(if (spoken.first().isDigit()) "at $spoken" else spoken, time.now(), settings.timeDefaults())
                val dt = parsed?.dateTime
                if (parsed != null && dt != null && parsed.hasTime) Preview.Confirm("", Args(args.with("hour", dt.hour.toString())).with("minute", dt.minute.toString()))
                else Preview.Ask("hour", "What time should the alarm be?")
            }
        },
        run = { args, _ ->
            val hour = args.int("hour")
            val minute = args.int("minute") ?: 0
            if (hour == null || hour !in 0..23 || minute !in 0..59) {
                ToolResult(false, "What time should the alarm be?", expectReply = true)
            } else {
                val intent = Intent(AlarmClock.ACTION_SET_ALARM)
                    .putExtra(AlarmClock.EXTRA_HOUR, hour)
                    .putExtra(AlarmClock.EXTRA_MINUTES, minute)
                    .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                args.string("label")?.let { intent.putExtra(AlarmClock.EXTRA_MESSAGE, it) }
                if (s.launch(intent)) ToolResult(true, "I've asked your clock app to set an alarm for ${TimeFormat.time(LocalTime.of(hour, minute))}.", verified = false)
                else ToolResult.fail("I couldn't find a clock app that accepts alarms. I can set a reminder instead if you like.")
            }
        },
    )

    private val setTimer: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.SET_TIMER,
            description = "Start a countdown timer in the phone's clock app.",
            params = listOf(ToolParam("seconds", "integer", "Timer length in seconds.", required = true, prompt = "How long should the timer be?")),
            allowedWhenLocked = true,
            verification = "The clock app does not report back, so Lumi says the request was sent.",
        ),
        pre = { args, _ ->
            if (args.int("seconds") != null) {
                Preview.Proceed
            } else {
                val d = args.string("seconds")?.let { DateTimeParser.parseDuration(it) }
                if (d != null) Preview.Confirm("", args.with("seconds", d.seconds.toString())) else Preview.Ask("seconds", "How long should the timer be?")
            }
        },
        run = { args, _ ->
            val seconds = args.int("seconds")
            if (seconds == null || seconds !in 1..86_400) {
                ToolResult.fail("Timers can be between one second and 24 hours.")
            } else {
                val intent = Intent(AlarmClock.ACTION_SET_TIMER).putExtra(AlarmClock.EXTRA_LENGTH, seconds).putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                if (s.launch(intent)) ToolResult(true, "I've asked your clock app to start a timer for ${TimeFormat.duration(Duration.ofSeconds(seconds.toLong()))}.", verified = false)
                else ToolResult.fail("I couldn't find a clock app that accepts timers.")
            }
        },
    )

    private val webSearch: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.WEB_SEARCH,
            description = "Open a web search for a query in the user's browser or search app.",
            params = listOf(ToolParam("query", "string", "What to search for.", required = true, prompt = "What should I search for?")),
        ),
        run = { args, _ ->
            val query = args.string("query").orEmpty()
            val search = Intent(Intent.ACTION_WEB_SEARCH).putExtra(SearchManager.QUERY, query)
            val browser = Intent(Intent.ACTION_VIEW, Uri.parse("https://www.google.com/search?q=" + Uri.encode(query)))
            if (s.launch(search) || s.launch(browser)) ToolResult(true, "Here's a search for $query.", verified = false, directive = Directive.Dismiss)
            else ToolResult.fail("I couldn't find a browser to search with.")
        },
    )

    // ------------------------------------------------------------------ screen awareness

    private class Screen(val service: LumiAccessibilityService?, val problem: ToolResult?)

    private fun screen(): Screen {
        if (!settings.screenAwareness) {
            return Screen(null, ToolResult.fail("Screen awareness is off. It's optional: you can turn it on in Lumi's Permissions screen.", suggestScreen = "permissions"))
        }
        if (!LumiAccessibilityService.isEnabled(context)) {
            return Screen(null, ToolResult.fail("Screen awareness needs Lumi's accessibility service. You can enable it from the Permissions screen.", suggestScreen = "permissions"))
        }
        val service = LumiAccessibilityService.instance
            ?: return Screen(null, ToolResult.fail("Android hasn't started Lumi's accessibility service yet. Try again in a moment."))
        return Screen(service, null)
    }

    private val readScreen: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.READ_SCREEN,
            description = "Look at the app currently on screen, once, on request: read it, summarise it, explain an error, or find a button. " +
                "With an AI provider the visible text is sent to it for summaries and explanations.",
            params = listOf(
                ToolParam("mode", "string", "What to do with the screen text.", enumValues = listOf("read", "summarize", "explain", "find")),
                ToolParam("find", "string", "For mode 'find': the button or text to look for."),
            ),
            privateOutput = true,
            verification = "Reads the live accessibility tree of the foreground window.",
        ),
        run = { args, _ -> describeScreen(args) },
    )

    private suspend fun describeScreen(args: Args): ToolResult {
        val sc = screen()
        val service = sc.service ?: return sc.problem!!
        val text = withContext(Dispatchers.Default) { service.readScreen() }?.text.orEmpty()
        if (text.isBlank()) return ToolResult.fail("I couldn't read anything on this screen. Some apps, games and videos don't expose their content.")
        val mode = args.string("mode") ?: "summarize"
        if (mode == "find") {
            val wanted = args.string("find").orEmpty()
            val line = text.lineSequence().firstOrNull { it.contains(wanted, ignoreCase = true) }
            return if (line != null) ToolResult.ok("Yes, I can see \"${line.take(80)}\" on this screen. Say \"tap $wanted\" and I'll press it.")
            else ToolResult.ok("I don't see \"$wanted\" on this screen. It may be further down; try saying scroll down.")
        }
        if (mode == "read") return ToolResult.ok(text.replace('\n', '.').take(1500))
        val client = ai()
        var answer: String? = null
        if (client != null) {
            val instruction = if (mode == "explain") {
                "The user is looking at this screen and asked what it means. Explain the main message or error in plain words and suggest the next step, in at most three short sentences for speech. No markdown."
            } else {
                "Summarise what this screen shows in at most three short sentences for speech. No markdown."
            }
            val r = client.chat(listOf(ChatMessage(Role.SYSTEM, instruction), ChatMessage(Role.USER, text.take(3500))), emptyList())
            if (r is AiResult.Ok) answer = r.text.trim().takeIf { it.isNotEmpty() }?.replace(Regex("[*_`#]+"), "")
        }
        if (answer != null) return ToolResult.ok(answer)
        val head = text.lineSequence().filter { it.length > 2 }.take(6).joinToString(". ").take(500)
        return ToolResult.ok("I can't interpret it without an AI provider, but here is what the screen says: $head.")
    }

    private val ordinals = mapOf("first" to 1, "second" to 2, "third" to 3, "fourth" to 4, "fifth" to 5, "last" to -1, "top" to 1, "bottom" to -1)

    private val screenAction: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.SCREEN_ACTION,
            description = "Act on the app currently on screen: scroll, go back, go home, or tap a button by its label or position.",
            params = listOf(
                ToolParam("action", "string", "What to do.", required = true, enumValues = listOf("scroll_down", "scroll_up", "back", "home", "click")),
                ToolParam("target", "string", "For click: the visible label of the element."),
                ToolParam("ordinal", "string", "For click without a label: first, second, third, last."),
            ),
            verification = "Reports whether Android accepted the gesture.",
        ),
        run = { args, _ -> actOnScreen(args) },
    )

    private fun actOnScreen(args: Args): ToolResult {
        val sc = screen()
        val service = sc.service ?: return sc.problem!!
        return when (val action = args.string("action")) {
            "back" -> if (service.back()) ToolResult(true, "Going back.", verified = true) else ToolResult.fail("I couldn't go back.")
            "home" -> if (service.home()) ToolResult(true, "Going home.", verified = true) else ToolResult.fail("I couldn't go to the home screen.")
            "scroll_down", "scroll_up", "scroll_left", "scroll_right" -> {
                val forward = action == "scroll_down" || action == "scroll_right"
                if (service.scroll(forward)) ToolResult(true, "Scrolled.", verified = true) else ToolResult.fail("There's nothing to scroll on this screen.")
            }
            "click" -> {
                val target = args.string("target")
                val ordinal = args.string("ordinal")?.let { ordinals[it.lowercase()] ?: it.toIntOrNull() }
                val clicked = service.click(target, if (target == null) ordinal ?: 1 else null)
                if (clicked != null) ToolResult(true, "Tapped $clicked.", verified = true)
                else ToolResult.fail(if (target != null) "I couldn't find \"$target\" to tap on this screen." else "I couldn't find that item on this screen.")
            }
            else -> ToolResult.fail("I can scroll, go back, go home, or tap something.")
        }
    }

    private val fillField: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.FILL_FIELD,
            description = "Type text into a field on the current screen (for filling forms step by step). Never used for password fields.",
            params = listOf(
                ToolParam("text", "string", "Text to enter.", required = true, prompt = "What should I type?"),
                ToolParam("field", "string", "Label or hint of the field. Defaults to the focused field."),
            ),
            confirmation = Confirmation.ALWAYS,
            verification = "Reports whether the field accepted the text.",
        ),
        pre = { args, _ -> Preview.Confirm("Type \"${args.string("text")}\"" + (args.string("field")?.let { " into $it" } ?: "") + "?") },
        run = { args, _ ->
            val sc = screen()
            val service = sc.service
            when {
                service == null -> sc.problem!!
                service.setText(args.string("text").orEmpty(), args.string("field")) -> ToolResult.ok("Done.", verified = true)
                else -> ToolResult.fail("I couldn't find that field. Tap it first, then ask again.")
            }
        },
    )

    // ------------------------------------------------------------------ briefing, battery, calendar, places

    fun batteryPercent(): Int? {
        val manager = context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        return manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY).takeIf { it in 0..100 }
    }

    private fun isCharging(): Boolean = (context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager).isCharging

    /** Calendar events for [day] as spoken phrases, or null without calendar permission. */
    private fun events(day: LocalDate): List<String>? {
        if (!s.has(Manifest.permission.READ_CALENDAR)) return null
        val zone = ZoneId.systemDefault()
        val start = day.atStartOfDay(zone).toInstant().toEpochMilli()
        val end = day.plusDays(1).atStartOfDay(zone).toInstant().toEpochMilli()
        val uri = CalendarContract.Instances.CONTENT_URI.buildUpon().appendPath(start.toString()).appendPath(end.toString()).build()
        val out = ArrayList<String>()
        return try {
            context.contentResolver.query(
                uri,
                arrayOf(CalendarContract.Instances.TITLE, CalendarContract.Instances.BEGIN, CalendarContract.Instances.ALL_DAY),
                null,
                null,
                CalendarContract.Instances.BEGIN + " ASC",
            )?.use { c ->
                while (c.moveToNext() && out.size < 12) {
                    val title = c.getString(0)?.takeIf { it.isNotBlank() } ?: "Untitled event"
                    val allDay = c.getInt(2) != 0
                    out += if (allDay) "$title, all day" else "$title at ${TimeFormat.time(Instant.ofEpochMilli(c.getLong(1)).atZone(zone).toLocalTime().withSecond(0).withNano(0))}"
                }
            }
            out
        } catch (e: Exception) {
            null
        }
    }

    /** Builds the daily briefing from local data only. */
    suspend fun briefingText(): String {
        val options = settings.briefingOptions
        val today = time.today()
        var notificationSummary: String? = null
        if (options.notifications) {
            val prefs = settings.notificationPrefs
            val listener = LumiNotificationListener.instance
            if (prefs.enabled && !prefs.paused && listener != null) notificationSummary = NotificationIntel.summary(listener.items(), prefs)
        }
        val data = BriefingData(
            name = memory.get(MemoryCategory.PREFERENCE, MemoryService.KEY_NAME),
            now = time.now(),
            dueToday = tasks.dueOn(today),
            overdue = tasks.overdue().filter { t -> t.dueAt?.let { time.toLocal(it).toLocalDate().isBefore(today) } == true },
            events = if (options.calendar) withContext(Dispatchers.IO) { events(today) } else null,
            notificationSummary = notificationSummary,
            batteryPercent = batteryPercent(),
        )
        return BriefingComposer.compose(data, options, time)
    }

    private val briefing: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.BRIEFING,
            description = "Give the daily briefing: today's tasks, overdue items, calendar events and a notification summary.",
            privateOutput = true,
        ),
        run = { _, _ -> ToolResult.ok(briefingText()) },
    )

    private val battery: Tool = SimpleTool(
        ToolSpec(ToolIds.BATTERY, "Report the phone's battery level and whether it is charging.", allowedWhenLocked = true, query = true),
        run = { _, _ ->
            val level = batteryPercent()
            if (level == null) ToolResult.fail("I couldn't read the battery level on this phone.")
            else ToolResult.ok("Battery is at $level percent" + (if (isCharging()) " and charging." else "."), verified = true)
        },
    )

    private val calendar: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.CALENDAR,
            description = "Read the user's calendar events for today or tomorrow.",
            params = listOf(ToolParam("day", "string", "Which day.", enumValues = listOf("today", "tomorrow"))),
            permissions = listOf(Manifest.permission.READ_CALENDAR),
            privateOutput = true,
        ),
        run = { args, _ ->
            val tomorrow = args.string("day") == "tomorrow"
            val day = if (tomorrow) time.today().plusDays(1) else time.today()
            val list = withContext(Dispatchers.IO) { events(day) }
            val label = if (tomorrow) "tomorrow" else "today"
            when {
                list == null -> s.needs(Manifest.permission.READ_CALENDAR, "to read your calendar")
                list.isEmpty() -> ToolResult.ok("Your calendar is clear $label.", verified = true)
                else -> ToolResult.ok("You have ${list.size} ${if (list.size == 1) "event" else "events"} $label: ${list.take(6).joinToString("; ")}.", verified = true)
            }
        },
    )

    /** Saves the current position under [name] and arms any reminders waiting for it. */
    suspend fun saveCurrentPlace(name: String): ToolResult {
        if (!geofences.hasForegroundLocation()) return s.needs(Manifest.permission.ACCESS_FINE_LOCATION, "to save where you are")
        val location = geofences.currentLocation()
            ?: return ToolResult.fail("I couldn't get your location. Check that Location is switched on, then try again.")
        val clean = name.trim().removePrefix("my ").trim()
        if (clean.isEmpty()) return ToolResult(false, "What should I call this place?", expectReply = true)
        val place = Place(name = clean, latitude = location.latitude, longitude = location.longitude)
        val saved = place.copy(id = places.save(place))
        val armed = tasks.armRemindersForPlace(saved)
        val background = geofences.hasBackgroundLocation()
        val extra = when {
            armed > 0 -> " $armed waiting ${if (armed == 1) "reminder is" else "reminders are"} now armed for it."
            !background -> " To trigger reminders there automatically, Lumi also needs the \"Allow all the time\" location permission."
            else -> ""
        }
        return ToolResult(true, "Saved this spot as $clean.$extra", verified = true, suggestScreen = if (background) null else "permissions")
    }

    private val savePlace: Tool = SimpleTool(
        ToolSpec(
            id = ToolIds.SAVE_PLACE,
            description = "Save the phone's current location under a name such as 'home' or 'college', for location reminders and navigation.",
            params = listOf(ToolParam("name", "string", "Name for this place.", required = true, prompt = "What should I call this place?")),
            verification = "Uses one on-demand location fix and confirms the place was stored.",
        ),
        run = { args, _ -> saveCurrentPlace(args.string("name").orEmpty()) },
    )

    fun deviceLocked(): Boolean = (context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager).isDeviceLocked

    companion object {
        const val SPOTIFY = "com.spotify.music"
    }
}
