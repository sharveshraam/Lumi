"""The main window: sidebar, status strip, pages, tray and the Stop button."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from lumi.ui.pages.chat import ChatPage
from lumi.ui.pages.memory import MemoryPage
from lumi.ui.pages.notes import NotesPage
from lumi.ui.pages.settings import SettingsPage
from lumi.ui.pages.tasks import TasksPage
from lumi.ui.theme import Palette, make_app_icon, status_colour
from lumi.ui.widgets.common import GlassCard, Pill, Row
from lumi.version import APP_NAME, __version__

log = logging.getLogger("lumi.ui.window")

PAGES: tuple[tuple[str, str], ...] = (
    ("chat", "Chat"),
    ("tasks", "Tasks & reminders"),
    ("notes", "Notes"),
    ("memory", "Memory"),
    ("settings", "Settings"),
)


class MainWindow(QMainWindow):
    """Owns the pages, the status strip and the global emergency stop."""

    def __init__(self, services: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.services = services
        self.app = services.app
        self._quitting = False

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(make_app_icon())
        self.resize(
            int(services.settings_store.settings.appearance.window_width),
            int(services.settings_store.settings.appearance.window_height),
        )

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        layout.addWidget(self._build_top_bar())

        body = QHBoxLayout()
        body.setSpacing(10)
        self.sidebar_card = self._build_sidebar()
        body.addWidget(self.sidebar_card)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        layout.addLayout(body, 1)

        self._build_pages()
        self._build_shortcuts()
        self._build_tray()
        self._hide_instead_of_quit = bool(services.settings_store.settings.startup.minimize_to_tray)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_status)
        self.status_timer.start(5000)
        if self.services.settings_store.settings.startup.check_ollama_on_start:
            self.refresh_status()

        self.services.app.add_delivery_listener(self._on_reminder)
        if self.services.settings_store.settings.automation.resume_automation_on_launch:
            self.services.app.clear_stop()
        self.apply_appearance()

    # ------------------------------------------------------------- top bar
    def _build_top_bar(self) -> QWidget:
        card = GlassCard(padding=12, strong=True)
        row = Row(spacing=10)

        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        row.add(title)

        subtitle = QLabel("local-first assistant")
        subtitle.setObjectName("Subtitle")
        row.add(subtitle)
        row.add_stretch()

        self.model_pill = Pill("model: none", Palette.TEXT_MUTED, "Which model answers your questions")
        row.add(self.model_pill)

        self.provider_pill = Pill("Local only", Palette.LOCAL, "Your prompts stay on this computer")
        row.add(self.provider_pill)

        self.control_pill = Pill("Computer control: off", Palette.TEXT_MUTED)
        row.add(self.control_pill)

        self.compact_button = QPushButton("⇱")
        self.compact_button.setToolTip(
            "Compact mode: a small always-on-top window (Ctrl+Shift+M)"
        )
        self.compact_button.setFixedWidth(34)
        self.compact_button.clicked.connect(self.toggle_compact_mode)
        row.add(self.compact_button)

        self.stop_button = QPushButton("■  STOP")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setToolTip(
            f"Stops any running action immediately. Global hotkey: "
            f"{self.services.settings_store.settings.automation.emergency_stop_hotkey}"
        )
        self.stop_button.setMinimumWidth(110)
        self.stop_button.clicked.connect(self.emergency_stop)
        row.add(self.stop_button)

        card.body().addWidget(row)
        return card

    # ------------------------------------------------------------- sidebar
    def _build_sidebar(self) -> QWidget:
        card = GlassCard(padding=6)
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(190)
        for key, label in PAGES:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.sidebar.addItem(item)
        self.sidebar.setCurrentRow(0)
        self.sidebar.currentRowChanged.connect(self._on_page_changed)
        card.body().addWidget(self.sidebar, 1)

        version = QLabel(f"v{__version__}")
        version.setObjectName("Faint")
        card.body().addWidget(version)
        return card

    def _build_pages(self) -> None:
        from lumi.ui.confirm import DialogConfirmationProvider

        self.confirm_provider = DialogConfirmationProvider(self)
        self.services.confirm_provider = self.confirm_provider

        self.chat_page = ChatPage(self.services)
        self.chat_page.stop_requested.connect(self.emergency_stop)
        self.chat_page.memory_accepted.connect(self._save_memory)
        self.tasks_page = TasksPage(self.services)
        self.notes_page = NotesPage(self.services)
        self.memory_page = MemoryPage(self.services)
        self.settings_page = SettingsPage(self.services)
        for page in (self.chat_page, self.tasks_page, self.notes_page, self.memory_page, self.settings_page):
            self.stack.addWidget(page)

        for page in (self.tasks_page, self.memory_page, self.notes_page):
            page.changed.connect(self._on_data_changed)
        # Appearance changes (compact mode, always-on-top) apply immediately
        # instead of waiting for a restart.
        self.settings_page.changed.connect(self._on_settings_changed)

    def _on_page_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        page = self.stack.widget(index)
        if hasattr(page, "refresh"):
            page.refresh()
        if page is self.chat_page:
            self.chat_page.composer.setFocus()

    def _on_data_changed(self) -> None:
        self.refresh_status()

    def _on_settings_changed(self, section: str = "") -> None:
        if section in ("", "settings"):
            self.apply_appearance()
        self.refresh_status()

    # ------------------------------------------------------- global hotkey
    def _build_shortcuts(self) -> None:
        stop_keys = self.services.settings_store.settings.automation.emergency_stop_hotkey
        shortcut = QShortcut(QKeySequence(stop_keys), self)
        shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        shortcut.activated.connect(self.emergency_stop)

        new_chat = QShortcut(QKeySequence("Ctrl+N"), self)
        new_chat.activated.connect(self.chat_page.start_new_conversation)

        compact = QShortcut(QKeySequence("Ctrl+Shift+M"), self)
        compact.activated.connect(self.toggle_compact_mode)

        focus_keys = self.services.settings_store.settings.speech.push_to_talk_hotkey
        if focus_keys:
            talk = QShortcut(QKeySequence(focus_keys), self)
            talk.setContext(Qt.ShortcutContext.ApplicationShortcut)
            talk.activated.connect(self._push_to_talk)

    def _push_to_talk(self) -> None:
        """First press records, second press transcribes and fills the box."""
        try:
            from lumi.voice import push_to_talk

            push_to_talk(self)
        except Exception as exc:  # noqa: BLE001 - voice is optional
            log.info("push-to-talk unavailable: %s", exc)
            self._notify(
                "Voice is not set up",
                "Install the voice extras (pip install faster-whisper sounddevice numpy) "
                "and pick a speech model in Settings → Voice.",
            )

    def _on_transcribed(self, text: str) -> None:
        """Put what was heard into the message box, ready to edit or send."""
        self._on_page_changed(0)
        self.chat_page.composer.setPlainText(text)
        self.chat_page.composer.setFocus()

    # ------------------------------------------------------------- tray
    def _build_tray(self) -> None:
        self.tray: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip(f"{APP_NAME} {__version__}")
        menu = self._tray_menu()
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_clicked)
        self.tray.show()

    def _tray_menu(self):
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        show = QAction("Show LUMI", self)
        show.triggered.connect(self._restore)
        menu.addAction(show)

        stop = QAction("Stop everything now", self)
        stop.triggered.connect(self.emergency_stop)
        menu.addAction(stop)

        briefing = QAction("Today's briefing", self)
        briefing.triggered.connect(self.show_briefing)
        menu.addAction(briefing)

        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.force_quit)
        menu.addAction(quit_action)
        return menu

    def _on_tray_clicked(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._restore()

    def _restore(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------- actions
    def emergency_stop(self) -> None:
        self.services.app.request_stop()
        self.stop_button.setText("■  STOPPED")
        self.stop_button.setStyleSheet(f"color: {Palette.LOCAL};")
        if self.chat_page.is_busy():
            self.chat_page.stop()
        self._notify("Stopped", "Every running action has been halted.")
        QTimer.singleShot(2500, self._reset_stop_button)

    def _reset_stop_button(self) -> None:
        self.stop_button.setText("■  STOP")
        self.stop_button.setStyleSheet("")
        self.services.app.clear_stop()

    def _save_memory(self, text: str) -> None:
        try:
            self.services.memory.add(text, kind="fact", source="chat")
            self._notify("Saved", "I will remember that.")
            self.memory_page.refresh()
        except ValueError as exc:
            self._notify("Not saved", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save memory: %s", exc)

    def show_briefing(self) -> None:
        try:
            from lumi.tools.builtin.productivity import _briefing_text  # type: ignore[attr-defined]

            text = _briefing_text(self.services.app.tool_context())
        except Exception:  # noqa: BLE001 - fall back to something honest
            text = self._fallback_briefing()
        self.chat_page.add_bubble("assistant", text)
        self._on_page_changed(0)

    def _fallback_briefing(self) -> str:
        tasks = self.services.tasks.due_today()
        overdue = self.services.tasks.overdue()
        parts = [f"You have {len(tasks)} task(s) due today and {len(overdue)} overdue."]
        for task in tasks[:5]:
            parts.append(f"  · {task.title}")
        parts.append("Calendar: not connected, so I cannot see meetings.")
        return "\n".join(parts)

    def _on_reminder(self, delivery: Any) -> None:
        message = getattr(delivery, "message", "") or getattr(delivery, "reminder", None)
        title = "Reminder"
        body = str(message)
        self._notify(title, body)
        self.chat_page.add_note(f"⏰ {body}", Palette.INFO)

    def run_on_ui_thread(self, work) -> None:
        """Run ``work`` on the Qt thread and block until it has finished.

        Background threads (the assistant loop) use this to show dialogs: the
        dialog must live on the UI thread, and the worker must wait for the
        answer rather than guess.
        """
        from PySide6.QtCore import QThread

        import threading

        if QThread.currentThread() is self.thread():
            work()
            return
        done = threading.Event()

        def wrapper() -> None:
            try:
                work()
            finally:
                done.set()

        QTimer.singleShot(0, wrapper)
        if not done.wait(600):
            log.warning("the UI did not answer a confirmation request in time")

    def _notify(self, title: str, body: str) -> None:
        """Tray balloon first (instant), then a real Windows toast if possible."""
        settings = self.services.settings_store.settings
        if self.tray is not None and settings.notifications.tray_notifications:
            self.tray.showMessage(title, body, make_app_icon(), 6000)
        try:
            from lumi.notifications import notify as system_notify

            system_notify(title, body, sound=bool(settings.notifications.sound_enabled))
        except Exception:  # noqa: BLE001 - notification is best effort
            log.debug("native notification failed", exc_info=True)

    # -------------------------------------------------------------- status
    def refresh_status(self) -> None:
        settings = self.services.settings_store.settings
        model = self.services.engine.selected_model()
        self.model_pill.set_state(
            f"model: {model or 'none'}", Palette.TEXT_MUTED if model else Palette.WARNING,
            "Pick a model in the chat header",
        )

        provider = self.services.engine.describe_state().get("provider", "local")
        if provider == "cloud":
            self.provider_pill.set_state("Cloud", Palette.CLOUD, "A cloud provider is answering")
        else:
            self.provider_pill.set_state("Local only", Palette.LOCAL, "Prompts never leave this computer")

        automation = self.services.automation.status()
        if not settings.automation.enabled:
            self.control_pill.set_state("Computer control: off", Palette.TEXT_MUTED, "Turned off in Settings → Permissions")
        elif automation.get("active"):
            self.control_pill.set_state("Computer control: active", Palette.WARNING, "LUMI is driving an app right now")
        elif automation.get("desktop_ready"):
            self.control_pill.set_state("Computer control: ready", Palette.INFO, "Ready, and asks before acting")
        else:
            self.control_pill.set_state(
                "Computer control: unavailable", Palette.TEXT_MUTED, automation.get("reason", "")
            )

        self.tasks_page.set_summary(
            f"{len(self.services.tasks.list(status='open', limit=500))} open · "
            f"{len(self.services.tasks.overdue())} overdue"
        )

    # ------------------------------------------------------------ appearance
    def apply_appearance(self) -> None:
        """Show compact or normal mode, following the saved settings.

        Compact mode is the floating assistant: the sidebar goes away, the
        window shrinks to something you can park in a corner, and it is kept
        above other windows so it is still readable while you work.
        """
        appearance = self.services.settings_store.settings.appearance
        compact = bool(appearance.compact_mode) or appearance.window_mode == "compact"

        self.sidebar_card.setVisible(not compact)
        self.compact_button.setText("⇲" if compact else "⇱")

        if compact:
            self.setMinimumSize(320, 360)
            self.resize(420, 560)
        else:
            self.setMinimumSize(900, 600)
            self.resize(int(appearance.window_width), int(appearance.window_height))

        on_top = bool(appearance.always_on_top) or compact
        if bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) != on_top:
            was_visible = self.isVisible()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on_top)
            if was_visible:
                self.show()

    def toggle_compact_mode(self) -> None:
        """Swap between the full window and the floating assistant."""
        store = self.services.settings_store
        appearance = store.settings.appearance
        appearance.window_mode = "normal" if appearance.compact_mode else "compact"
        appearance.sync_compact_flag()
        # Remember the size to come back to when leaving compact mode.
        if not appearance.compact_mode:
            appearance.window_width = max(900, self.width())
            appearance.window_height = max(600, self.height())
        try:
            store.save()
        except Exception as exc:  # noqa: BLE001 - a read-only folder must not crash the UI
            log.warning("could not save the appearance change: %s", exc)
        self.apply_appearance()
        if hasattr(self, "settings_page") and hasattr(self.settings_page, "load_from_settings"):
            try:
                self.settings_page.load_from_settings()
            except Exception:  # noqa: BLE001
                log.debug("settings page refresh after compact toggle failed", exc_info=True)

    # ------------------------------------------------------------ lifecycle
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        settings = self.services.settings_store.settings
        if self._quitting or not (settings.startup.minimize_to_tray or settings.startup.close_to_tray):
            self.force_quit()
            return
        if self.tray is not None:
            event.ignore()
            self.hide()
            self._notify("Still running", "LUMI is in the tray. Use Quit in the tray menu to exit.")
        else:
            self.force_quit()

    def force_quit(self) -> None:
        self._quitting = True
        try:
            self.services.app.request_stop()
        except Exception:  # noqa: BLE001
            pass
        if self.tray is not None:
            self.tray.hide()
        try:
            self.services.app.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("shutdown failed")
        QApplication.quit()


def run() -> int:
    """Entry point used by ``python -m lumi`` and by the packaged executable."""
    import sys

    from lumi.app import create_app

    logging.basicConfig(level=logging.INFO)
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName(APP_NAME)
    application.setApplicationVersion(__version__)
    application.setWindowIcon(make_app_icon())
    from lumi.ui.theme import STYLESHEET

    application.setStyleSheet(STYLESHEET)

    core = create_app()
    services = core.services()
    # The pages need the owning application (tool contexts, stop, shutdown).
    services.app = core  # type: ignore[attr-defined]
    core.start_scheduler()

    startup = services.settings_store.settings.startup
    appearance = services.settings_store.settings.appearance

    # Text size is a single knob: scale the application font once, here.
    if abs(float(appearance.font_scale) - 1.0) > 0.01:
        base = application.font()
        base.setPointSizeF(max(7.0, base.pointSizeF() * float(appearance.font_scale)))
        application.setFont(base)

    window = MainWindow(services)
    if startup.start_minimised and window.tray is not None:
        # Straight to the tray: the window is one click away when wanted.
        window.hide()
    else:
        window.show()
        if startup.show_briefing_on_start:
            QTimer.singleShot(800, window.show_briefing)
    return application.exec()
