"""Settings: every switch that changes what LUMI is allowed to do."""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from lumi.config.settings import QUALITY_PROFILES, SettingsStore
from lumi.ui.theme import Palette
from lumi.ui.widgets.common import GlassCard, Row

log = logging.getLogger("lumi.ui.settings")


class SettingsPage(QWidget):
    """A plain editor over :class:`lumi.config.settings.Settings`.

    The UI never writes files directly: it changes the settings object and asks
    the store to save, so validation happens in exactly one place.
    """

    changed = Signal(str)

    def __init__(self, services: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.services = services
        self.store: SettingsStore = services.settings_store

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        header = GlassCard(padding=12)
        row = Row(spacing=8)
        title = QLabel("Settings")
        title.setObjectName("Title")
        row.add(title)
        self.paths_label = QLabel(f"Data folder: {self.store.paths.home}")
        self.paths_label.setObjectName("Faint")
        row.add(self.paths_label)
        row.add_stretch()
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        row.add(save)
        reset = QPushButton("Reload from disk")
        reset.clicked.connect(self.reload)
        row.add(reset)
        header.body().addWidget(row)
        self.status_label = QLabel("Changes are applied when you press Save.")
        self.status_label.setObjectName("Faint")
        header.body().addWidget(self.status_label)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.pages = QTabWidget()
        self.pages.addTab(self._build_model_tab(), "Model")
        self.pages.addTab(self._build_permissions_tab(), "Permissions")
        self.pages.addTab(self._build_speech_tab(), "Voice")
        self.pages.addTab(self._build_privacy_tab(), "Privacy & data")
        self.pages.addTab(self._build_appearance_tab(), "Appearance")
        self.pages.addTab(self._build_about_tab(), "About")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.pages)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        self.load_from_settings()
        self.store.add_listener(lambda _settings, _section: self.load_from_settings())

    # ------------------------------------------------------------------ model
    def _build_model_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        ollama = QGroupBox("Ollama (local engine)")
        form = QFormLayout(ollama)
        self.base_url = QLineEdit()
        form.addRow("Server address", self.base_url)
        self.connect_timeout = QDoubleSpinBox()
        self.connect_timeout.setRange(0.5, 30.0)
        self.connect_timeout.setSingleStep(0.5)
        self.connect_timeout.setSuffix(" s")
        form.addRow("Connect timeout", self.connect_timeout)
        self.request_timeout = QDoubleSpinBox()
        self.request_timeout.setRange(10.0, 3600.0)
        self.request_timeout.setSingleStep(10.0)
        self.request_timeout.setSuffix(" s")
        form.addRow("Answer timeout", self.request_timeout)
        self.keep_alive = QLineEdit()
        self.keep_alive.setPlaceholderText("10m")
        form.addRow("Keep model loaded for", self.keep_alive)
        self.allow_loopback = QCheckBox("Allow a server that is not on this computer")
        self.allow_loopback.setToolTip("Only turn this on if you deliberately run Ollama on another machine.")
        form.addRow(self.allow_loopback)
        self.unload_idle = QCheckBox("Unload the model after it has been idle")
        form.addRow(self.unload_idle)
        layout.addWidget(ollama)

        generation = QGroupBox("Answer style")
        gen_form = QFormLayout(generation)
        self.quality_profile = QComboBox()
        self.quality_profile.addItems(list(QUALITY_PROFILES.keys()))
        gen_form.addRow("Quality profile", self.quality_profile)
        self.use_profile_limits = QCheckBox("Apply the profile's context limits")
        gen_form.addRow(self.use_profile_limits)
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.05)
        gen_form.addRow("Temperature", self.temperature)
        self.num_ctx = QSpinBox()
        self.num_ctx.setRange(512, 131072)
        self.num_ctx.setSingleStep(512)
        gen_form.addRow("Context window", self.num_ctx)
        self.num_predict = QSpinBox()
        self.num_predict.setRange(32, 16384)
        self.num_predict.setSingleStep(64)
        gen_form.addRow("Maximum answer length", self.num_predict)
        self.max_tool_iterations = QSpinBox()
        self.max_tool_iterations.setRange(1, 10)
        gen_form.addRow("Maximum tool steps per answer", self.max_tool_iterations)
        layout.addWidget(generation)

        system = QGroupBox("Extra instructions")
        system_layout = QVBoxLayout(system)
        self.system_prompt_extra = QLineEdit()
        self.system_prompt_extra.setPlaceholderText("e.g. 'Answer in short bullet points'")
        system_layout.addWidget(self.system_prompt_extra)
        hint = QLabel("Added to the system prompt. LUMI's safety rules cannot be overridden here.")
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        system_layout.addWidget(hint)
        layout.addWidget(system)

        layout.addStretch(1)
        return page

    # ------------------------------------------------------------ permissions
    def _build_permissions_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        master = QGroupBox("Computer control")
        master_layout = QVBoxLayout(master)
        self.automation_enabled = QCheckBox("Let LUMI control apps and the desktop at all")
        master_layout.addWidget(self.automation_enabled)
        note = QLabel(
            "Everything below is off by default. LUMI asks for confirmation before anything consequential, "
            "and the Stop button halts a running action immediately."
        )
        note.setObjectName("Faint")
        note.setWordWrap(True)
        master_layout.addWidget(note)
        layout.addWidget(master)

        toggles = QGroupBox("What LUMI may do")
        toggles_layout = QVBoxLayout(toggles)
        self.allow_app_launch = QCheckBox("Open and switch applications")
        self.allow_keyboard_mouse = QCheckBox("Move the mouse and type (drives apps for you)")
        self.allow_screenshots = QCheckBox("Take screenshots")
        self.allow_file_operations = QCheckBox("Read and search files in the folders below")
        self.allow_file_writes = QCheckBox("Create and change files")
        self.allow_browser_automation = QCheckBox("Work with your browser (search, read pages, Gmail drafts)")
        self.allow_bulk_file_operations = QCheckBox("Bulk file operations (many files at once)")
        for widget in (
            self.allow_app_launch,
            self.allow_keyboard_mouse,
            self.allow_screenshots,
            self.allow_file_operations,
            self.allow_file_writes,
            self.allow_browser_automation,
            self.allow_bulk_file_operations,
        ):
            toggles_layout.addWidget(widget)
        layout.addWidget(toggles)

        confirmations = QGroupBox("Always ask first")
        conf_layout = QVBoxLayout(confirmations)
        self.confirm_list = QLabel("")
        self.confirm_list.setWordWrap(True)
        conf_layout.addWidget(self.confirm_list)
        conf_note = QLabel("This list is fixed: sending mail, submitting forms, deleting files, installing software and changing security settings always ask.")
        conf_note.setObjectName("Faint")
        conf_note.setWordWrap(True)
        conf_layout.addWidget(conf_note)
        layout.addWidget(confirmations)

        folders = QGroupBox("Folders LUMI works in")
        folders_layout = QVBoxLayout(folders)
        self.workspace_folders = QLineEdit()
        self.workspace_folders.setPlaceholderText("Fully allowed: C:\\Users\\you\\LUMI-Workspace;D:\\College")
        folders_layout.addWidget(QLabel("Full access (read and write):"))
        folders_layout.addWidget(self.workspace_folders)
        self.search_folders = QLineEdit()
        self.search_folders.setPlaceholderText("Read-only: C:\\Users\\you\\Documents")
        folders_layout.addWidget(QLabel("Read-only search: "))
        folders_layout.addWidget(self.search_folders)
        self.blocklist_folders = QLineEdit()
        self.blocklist_folders.setPlaceholderText("Never opened")
        folders_layout.addWidget(QLabel("Blocked:"))
        folders_layout.addWidget(self.blocklist_folders)
        layout.addWidget(folders)

        hotkey = QGroupBox("Emergency stop")
        hotkey_layout = QFormLayout(hotkey)
        self.stop_hotkey = QLineEdit()
        hotkey_layout.addRow("Global hotkey", self.stop_hotkey)
        stop_note = QLabel("Pressing it stops any running automation, wherever it is. It cannot be disabled.")
        stop_note.setObjectName("Faint")
        stop_note.setWordWrap(True)
        hotkey_layout.addRow(stop_note)
        layout.addWidget(hotkey)

        layout.addStretch(1)
        return page

    # ----------------------------------------------------------------- voice
    def _build_speech_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        speech = QGroupBox("Speech")
        form = QFormLayout(speech)
        self.speech_enabled = QCheckBox("Enable voice input and output")
        form.addRow(self.speech_enabled)
        self.tts_enabled = QCheckBox("Speak answers aloud")
        form.addRow(self.tts_enabled)
        self.tts_engine = QComboBox()
        self.tts_engine.addItems(["auto", "piper", "sapi", "pyttsx3", "espeak", "none"])
        form.addRow("Voice engine", self.tts_engine)
        self.piper_exe = QLineEdit()
        self.piper_exe.setPlaceholderText("Path to piper.exe (if you use Piper)")
        form.addRow("Piper executable", self.piper_exe)
        self.piper_voice = QLineEdit()
        self.piper_voice.setPlaceholderText("Path to a .onnx voice model")
        form.addRow("Piper voice", self.piper_voice)
        self.speak_replies = QCheckBox("Read every reply out loud automatically")
        form.addRow(self.speak_replies)
        layout.addWidget(speech)

        listening = QGroupBox("Listening")
        listen_form = QFormLayout(listening)
        self.push_to_talk = QCheckBox("Push-to-talk (hold a key to speak) - recommended")
        listen_form.addRow(self.push_to_talk)
        self.ptt_hotkey = QLineEdit()
        listen_form.addRow("Push-to-talk key", self.ptt_hotkey)
        self.wake_word_enabled = QCheckBox("Always listening for a wake word")
        listen_form.addRow(self.wake_word_enabled)
        self.wake_word = QLineEdit()
        listen_form.addRow("Wake word", self.wake_word)
        self.stt_model = QComboBox()
        self.stt_model.addItems(["tiny", "base", "small", "medium"])
        listen_form.addRow("Speech model size", self.stt_model)
        self.retain_audio = QCheckBox("Keep recordings on disk (off = they are discarded after transcription)")
        listen_form.addRow(self.retain_audio)
        layout.addWidget(listening)

        note = QLabel(
            "Voice runs locally through faster-whisper. Audio is only kept if you explicitly tick the box above; "
            "otherwise it is deleted the moment the transcription finishes."
        )
        note.setObjectName("Faint")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # --------------------------------------------------------------- privacy
    def _build_privacy_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        privacy = QGroupBox("History and privacy")
        form = QFormLayout(privacy)
        self.save_conversations = QCheckBox("Save conversations to this computer")
        form.addRow(self.save_conversations)
        self.retention_days = QSpinBox()
        self.retention_days.setRange(0, 3650)
        self.retention_days.setSuffix(" days (0 = keep forever)")
        form.addRow("Delete history after", self.retention_days)
        self.remember_from_chat = QCheckBox("Offer to remember things you tell it")
        form.addRow(self.remember_from_chat)
        self.ask_before_remembering = QCheckBox("Always ask before saving a memory")
        form.addRow(self.ask_before_remembering)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        form.addRow("Log detail", self.log_level)
        self.log_prompts = QCheckBox("Write prompts to the log (off by default)")
        form.addRow(self.log_prompts)
        layout.addWidget(privacy)

        cloud = QGroupBox("Cloud providers")
        cloud_layout = QVBoxLayout(cloud)
        self.cloud_provider = QComboBox()
        self.cloud_provider.addItems(["local", "cloud", "ask"])
        cloud_layout.addWidget(QLabel("Answers come from:"))
        cloud_layout.addWidget(self.cloud_provider)
        cloud_note = QLabel(
            "LUMI is local-first: 'local' keeps every prompt on this machine. Choosing 'cloud' requires your own "
            "API key, stored in the Windows credential manager - never in the settings file or the log. "
            "No cloud provider is enabled in this build."
        )
        cloud_note.setObjectName("Faint")
        cloud_note.setWordWrap(True)
        cloud_layout.addWidget(cloud_note)
        self.cloud_status = QLabel("")
        self.cloud_status.setObjectName("Faint")
        cloud_layout.addWidget(self.cloud_status)
        layout.addWidget(cloud)

        data = QGroupBox("Data")
        data_layout = QVBoxLayout(data)
        open_folder = QPushButton("Open the LUMI data folder")
        open_folder.setToolTip(str(self.store.paths.home))
        open_folder.clicked.connect(self.open_data_folder)
        data_layout.addWidget(open_folder)
        self.data_summary = QLabel("")
        self.data_summary.setObjectName("Faint")
        self.data_summary.setWordWrap(True)
        data_layout.addWidget(self.data_summary)
        layout.addWidget(data)

        layout.addStretch(1)
        return page

    # ------------------------------------------------------------ appearance
    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        window = QGroupBox("Window")
        form = QFormLayout(window)
        self.theme = QComboBox()
        self.theme.addItems(["dark"])
        form.addRow("Theme", self.theme)
        self.font_scale = QDoubleSpinBox()
        self.font_scale.setRange(0.8, 1.6)
        self.font_scale.setSingleStep(0.05)
        form.addRow("Text size", self.font_scale)
        self.compact_mode = QCheckBox("Compact mode (small floating window)")
        form.addRow(self.compact_mode)
        self.always_on_top = QCheckBox("Keep the window above other windows")
        form.addRow(self.always_on_top)
        self.show_timestamps = QCheckBox("Show timestamps in the chat")
        form.addRow(self.show_timestamps)
        layout.addWidget(window)

        startup = QGroupBox("Startup")
        startup_layout = QVBoxLayout(startup)
        self.launch_on_login = QCheckBox("Start LUMI when I sign in to Windows")
        self.start_minimised = QCheckBox("Start minimised")
        self.minimize_to_tray = QCheckBox("Keep running in the tray when closed")
        self.check_ollama_on_start = QCheckBox("Check Ollama when LUMI starts")
        self.briefing_on_start = QCheckBox("Show the daily briefing when LUMI starts")
        for widget in (
            self.launch_on_login,
            self.start_minimised,
            self.minimize_to_tray,
            self.check_ollama_on_start,
            self.briefing_on_start,
        ):
            startup_layout.addWidget(widget)
        layout.addWidget(startup)

        notifications = QGroupBox("Notifications")
        notify_layout = QVBoxLayout(notifications)
        self.reminders_enabled = QCheckBox("Show reminder notifications")
        self.sound_enabled = QCheckBox("Play a sound with reminders")
        self.break_notifications = QCheckBox("Nudge me to take breaks during focus sessions")
        for widget in (self.reminders_enabled, self.sound_enabled, self.break_notifications):
            notify_layout.addWidget(widget)
        layout.addWidget(notifications)

        layout.addStretch(1)
        return page

    # ---------------------------------------------------------------- about
    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.about_label = QLabel("")
        self.about_label.setWordWrap(True)
        self.about_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.about_label)
        refresh = QPushButton("Refresh system report")
        refresh.clicked.connect(self.refresh_about)
        layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.refresh_about()
        return page

    # ------------------------------------------------------------- load/save
    def load_from_settings(self) -> None:
        s = self.store.settings
        # model
        self.base_url.setText(s.ollama.base_url)
        self.connect_timeout.setValue(s.ollama.connect_timeout_s)
        self.request_timeout.setValue(s.ollama.request_timeout_s)
        self.keep_alive.setText(s.ollama.keep_alive)
        self.allow_loopback.setChecked(s.ollama.allow_non_loopback_host)
        self.unload_idle.setChecked(s.ollama.unload_on_idle)
        self.quality_profile.setCurrentText(s.model.quality_profile)
        self.use_profile_limits.setChecked(s.model.use_profile_limits)
        self.temperature.setValue(s.generation.temperature)
        self.num_ctx.setValue(s.generation.num_ctx)
        self.num_predict.setValue(s.generation.num_predict)
        self.max_tool_iterations.setValue(s.model.max_tool_iterations)
        self.system_prompt_extra.setText(s.model.system_prompt_extra)
        # permissions
        self.automation_enabled.setChecked(s.automation.enabled)
        self.allow_app_launch.setChecked(s.automation.allow_app_launch)
        self.allow_keyboard_mouse.setChecked(s.automation.allow_keyboard_mouse)
        self.allow_screenshots.setChecked(s.automation.allow_screenshots)
        self.allow_file_operations.setChecked(s.automation.allow_file_operations)
        self.allow_file_writes.setChecked(s.automation.allow_file_writes)
        self.allow_browser_automation.setChecked(s.automation.allow_browser_automation)
        self.allow_bulk_file_operations.setChecked(s.automation.allow_bulk_file_operations)
        self.confirm_list.setText(", ".join(s.automation.require_confirmation_for).replace("_", " "))
        self.workspace_folders.setText(";".join(s.automation.workspace_folders))
        self.search_folders.setText(";".join(s.automation.search_folders))
        self.blocklist_folders.setText(";".join(s.automation.blocklist_folders))
        self.stop_hotkey.setText(s.automation.emergency_stop_hotkey)
        # speech
        self.speech_enabled.setChecked(s.speech.enabled)
        self.tts_enabled.setChecked(s.speech.tts_enabled)
        self.tts_engine.setCurrentText(s.speech.tts_engine)
        self.piper_exe.setText(s.speech.piper_exe)
        self.piper_voice.setText(s.speech.piper_voice)
        self.speak_replies.setChecked(s.speech.speak_replies)
        self.push_to_talk.setChecked(s.speech.push_to_talk_enabled)
        self.ptt_hotkey.setText(s.speech.push_to_talk_hotkey)
        self.wake_word_enabled.setChecked(s.speech.wake_word_enabled)
        self.wake_word.setText(s.speech.wake_word)
        self.stt_model.setCurrentText(s.speech.stt_model)
        self.retain_audio.setChecked(s.speech.retain_audio)
        # privacy
        self.save_conversations.setChecked(s.privacy.save_conversations)
        self.retention_days.setValue(s.privacy.conversation_retention_days)
        self.remember_from_chat.setChecked(s.privacy.remember_from_chat)
        self.ask_before_remembering.setChecked(s.privacy.ask_before_remembering)
        self.log_level.setCurrentText(s.privacy.log_level)
        self.log_prompts.setChecked(s.privacy.log_prompts)
        self.cloud_provider.setCurrentText(s.cloud.preferred_provider)
        enabled = [p.name for p in s.cloud.providers.values() if p.enabled]
        self.cloud_status.setText(
            f"Enabled cloud providers: {', '.join(enabled)}" if enabled else "No cloud provider is enabled."
        )
        # appearance
        self.theme.setCurrentText(s.appearance.theme)
        self.font_scale.setValue(s.appearance.font_scale)
        self.compact_mode.setChecked(s.appearance.compact_mode)
        self.always_on_top.setChecked(s.appearance.always_on_top)
        self.show_timestamps.setChecked(s.appearance.show_timestamps)
        self.launch_on_login.setChecked(s.startup.launch_on_login)
        self.start_minimised.setChecked(s.startup.start_minimised)
        self.minimize_to_tray.setChecked(s.startup.minimize_to_tray)
        self.check_ollama_on_start.setChecked(s.startup.check_ollama_on_start)
        self.briefing_on_start.setChecked(s.startup.show_briefing_on_start)
        self.reminders_enabled.setChecked(s.notifications.reminders_enabled)
        self.sound_enabled.setChecked(s.notifications.sound_enabled)
        self.break_notifications.setChecked(s.notifications.break_notifications)

        self.paths_label.setText(f"Data folder: {self.store.paths.home}")

    def save(self) -> None:
        try:
            self.store.update(
                ollama={
                    "base_url": self.base_url.text().strip(),
                    "connect_timeout_s": self.connect_timeout.value(),
                    "request_timeout_s": self.request_timeout.value(),
                    "keep_alive": self.keep_alive.text().strip() or "10m",
                    "allow_non_loopback_host": self.allow_loopback.isChecked(),
                    "unload_on_idle": self.unload_idle.isChecked(),
                },
                model={
                    "quality_profile": self.quality_profile.currentText(),
                    "use_profile_limits": self.use_profile_limits.isChecked(),
                    "max_tool_iterations": self.max_tool_iterations.value(),
                    "system_prompt_extra": self.system_prompt_extra.text().strip(),
                },
                generation={
                    "temperature": self.temperature.value(),
                    "num_ctx": self.num_ctx.value(),
                    "num_predict": self.num_predict.value(),
                },
                automation={
                    "enabled": self.automation_enabled.isChecked(),
                    "allow_app_launch": self.allow_app_launch.isChecked(),
                    "allow_keyboard_mouse": self.allow_keyboard_mouse.isChecked(),
                    "allow_screenshots": self.allow_screenshots.isChecked(),
                    "allow_file_operations": self.allow_file_operations.isChecked(),
                    "allow_file_writes": self.allow_file_writes.isChecked(),
                    "allow_browser_automation": self.allow_browser_automation.isChecked(),
                    "allow_bulk_file_operations": self.allow_bulk_file_operations.isChecked(),
                    "workspace_folders": _split_paths(self.workspace_folders.text()),
                    "search_folders": _split_paths(self.search_folders.text()),
                    "blocklist_folders": _split_paths(self.blocklist_folders.text()),
                    "emergency_stop_hotkey": self.stop_hotkey.text().strip() or "Ctrl+Alt+Shift+S",
                },
                speech={
                    "enabled": self.speech_enabled.isChecked(),
                    "tts_enabled": self.tts_enabled.isChecked(),
                    "tts_engine": self.tts_engine.currentText(),
                    "piper_exe": self.piper_exe.text().strip(),
                    "piper_voice": self.piper_voice.text().strip(),
                    "speak_replies": self.speak_replies.isChecked(),
                    "push_to_talk_enabled": self.push_to_talk.isChecked(),
                    "push_to_talk_hotkey": self.ptt_hotkey.text().strip() or "Ctrl+Alt+Space",
                    "wake_word_enabled": self.wake_word_enabled.isChecked(),
                    "wake_word": self.wake_word.text().strip() or "lumi",
                    "stt_model": self.stt_model.currentText(),
                    "retain_audio": self.retain_audio.isChecked(),
                },
                privacy={
                    "save_conversations": self.save_conversations.isChecked(),
                    "conversation_retention_days": self.retention_days.value(),
                    "remember_from_chat": self.remember_from_chat.isChecked(),
                    "ask_before_remembering": self.ask_before_remembering.isChecked(),
                    "log_level": self.log_level.currentText(),
                    "log_prompts": self.log_prompts.isChecked(),
                },
                cloud={"preferred_provider": self.cloud_provider.currentText()},
                appearance={
                    "theme": self.theme.currentText(),
                    "font_scale": self.font_scale.value(),
                    "compact_mode": self.compact_mode.isChecked(),
                    "always_on_top": self.always_on_top.isChecked(),
                    "show_timestamps": self.show_timestamps.isChecked(),
                },
                startup={
                    "launch_on_login": self.launch_on_login.isChecked(),
                    "start_minimised": self.start_minimised.isChecked(),
                    "minimize_to_tray": self.minimize_to_tray.isChecked(),
                    "check_ollama_on_start": self.check_ollama_on_start.isChecked(),
                    "show_briefing_on_start": self.briefing_on_start.isChecked(),
                },
                notifications={
                    "reminders_enabled": self.reminders_enabled.isChecked(),
                    "sound_enabled": self.sound_enabled.isChecked(),
                    "break_notifications": self.break_notifications.isChecked(),
                },
            )
            self.store.save()
            note = self._sync_start_with_system()
            self.status_label.setText(
                "Saved. The changes apply immediately." + (f" {note}" if note else "")
            )
            self.changed.emit("settings")
        except Exception as exc:  # noqa: BLE001
            log.exception("could not save settings")
            self.status_label.setText(f"Could not save: {exc}")

    def _sync_start_with_system(self) -> str:
        """Create or remove the Start-up launcher so the checkbox is real.

        Returns a short note for the status line, or "" when nothing changed.
        """
        from lumi.config import startup

        wanted = bool(self.store.settings.startup.launch_on_login)
        try:
            if wanted and not startup.is_enabled():
                path = startup.enable()
                return f"LUMI will start when you sign in ({path})."
            if not wanted and startup.is_enabled():
                startup.disable()
                return "LUMI will no longer start when you sign in."
        except Exception as exc:  # noqa: BLE001 - a locked profile folder must not break saving
            log.warning("could not change the start-up entry: %s", exc)
            return f"(Could not change the start-up entry: {exc})"
        return ""

    def reload(self) -> None:
        self.store.load()
        self.load_from_settings()
        self.status_label.setText("Reloaded from disk.")

    # ----------------------------------------------------------------- misc
    def open_data_folder(self) -> None:
        import subprocess
        import sys

        path = str(self.store.paths.home)
        try:
            if sys.platform.startswith("win"):
                import os

                os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 - user's own folder
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])  # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", path])  # noqa: S603,S607
        except Exception as exc:  # noqa: BLE001
            self.status_line_error(f"Could not open the folder: {exc}")

    def status_line_error(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {Palette.DANGER};")

    def refresh_about(self) -> None:
        try:
            report = self.services.app.system_summary()
        except Exception as exc:  # noqa: BLE001
            self.about_label.setText(f"Could not build the system report: {exc}")
            return
        hardware = report.get("hardware", {})
        ollama = report.get("ollama", {})
        tools = report.get("tools", {})
        automation = report.get("automation", {})
        lines = [
            f"LUMI {report.get('version')}",
            f"Python {report.get('python')} on {report.get('platform')}",
            f"Data folder: {report.get('home')}",
            "",
            f"Hardware: {report.get('hardware_summary')}",
            f"CPU: {hardware.get('cpu', 'unknown')}",
            f"RAM: {hardware.get('ram_gb', 0):.1f} GB (budget {hardware.get('ram_budget_gb', 0):.1f} GB)",
            "",
            f"Ollama: {'running' if ollama.get('running') else 'not running'} "
            f"({ollama.get('message', '')})",
            f"Selected model: {report.get('selected_model') or 'none'}",
            "",
            f"Tools registered: {tools.get('total', 0)}",
            f"Automation backend: {automation.get('backend')} "
            f"({'ready' if automation.get('desktop_ready') else 'unavailable'})",
            f"Permissions: keyboard/mouse {'on' if automation.get('keyboard_mouse_allowed') else 'off'}, "
            f"screenshots {'on' if automation.get('screenshots_allowed') else 'off'}",
            f"Database: {report.get('database_bytes', 0) / 1024:.0f} KB",
        ]
        if report.get("previous_run_unclean"):
            lines.append("")
            lines.append("Note: LUMI did not shut down cleanly last time. Settings and data were checked on start.")
        self.about_label.setText("\n".join(lines))


def _split_paths(text: str) -> list[str]:
    parts = [part.strip() for part in (text or "").replace(",", ";").split(";")]
    return [part for part in parts if part]
