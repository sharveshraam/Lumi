"""Headless UI tests: the window, the pages and the confirmation dialog.

These run on the Qt "offscreen" platform, so they work on a build machine with
no display.  They are skipped (not failed) when PySide6 is not installed, because
the core application must be usable without it.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from lumi.tools.base import ConfirmationRequest, RiskLevel  # noqa: E402
from lumi.ui.confirm import ANSWER_NO, ANSWER_SESSION, ANSWER_YES, ConfirmationDialog  # noqa: E402
from lumi.ui.pages.chat import ChatPage  # noqa: E402
from lumi.ui.pages.memory import MemoryPage  # noqa: E402
from lumi.ui.pages.notes import NotesPage  # noqa: E402
from lumi.ui.pages.settings import SettingsPage  # noqa: E402
from lumi.ui.pages.tasks import TasksPage  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def ui_services(qt_app, settings_store, lumi_home, db, registry, executor, memory, conversations, tasks, notes, reminders, reminder_repo, focus, audit, policy, tool_context):
    """An ``AppServices``-shaped object without building the whole application."""

    class Services:
        pass

    services = Services()
    services.paths = lumi_home
    services.settings_store = settings_store
    services.database = db
    services.registry = registry
    services.executor = executor
    services.memory = memory
    services.conversations = conversations
    services.tasks = tasks
    services.notes = notes
    services.reminders = reminders
    services.reminder_repo = reminder_repo
    services.focus = focus
    services.automation = tool_context.services["automation"]
    services.files = tool_context.services["files"]
    services.browser = tool_context.services["browser"]
    services.audit = audit
    services.policy = policy
    services.confirm_provider = None
    services.engine = _FakeEngine()
    services.app = _FakeApp()
    return services


class _FakeApp:
    """The application surface the pages use (stop, contexts, notifications)."""

    def __init__(self) -> None:
        self.stopped = False
        self.listeners: list[Any] = []

    def request_stop(self) -> None:
        self.stopped = True

    def clear_stop(self) -> None:
        self.stopped = False

    def add_delivery_listener(self, callback) -> None:
        self.listeners.append(callback)

    def tool_context(self, **kwargs):
        return _StubContext(kwargs)

    def system_summary(self) -> dict[str, Any]:
        return {
            "version": "1.0.0",
            "home": "/tmp",
            "platform": "test",
            "python": "3.11",
            "hardware": {},
            "hardware_summary": "CPU: test",
            "ollama": {"running": False, "message": "not running"},
            "selected_model": "",
            "tools": {"total": 69},
            "automation": {"backend": "none", "desktop_ready": False},
            "database_bytes": 0,
        }

    def notify(self, title: str, message: str) -> None:
        return None


class _StubContext:
    def __init__(self, kwargs: dict) -> None:
        self.kwargs = kwargs


class _FakeEngine:
    """The small slice of the engine the pages touch."""

    def status(self):
        from lumi.ai.ollama import OllamaStatus

        return OllamaStatus(running=False, installed=True, message="not running", error_kind="connection")

    def list_models(self) -> list[Any]:
        return []

    def selected_model(self) -> str:
        return ""

    def describe_state(self) -> dict[str, Any]:
        return {"provider": "local", "ollama": {}, "selected_model": ""}

    def invalidate_model_cache(self) -> None:
        return None


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
def test_chat_page_builds(ui_services, qt_app) -> None:
    page = ChatPage(ui_services)
    page.refresh_models()
    assert page.status_pill.text()
    assert page.composer.isEnabled()
    assert not page.stop_button.isEnabled()


def test_chat_page_shows_a_tool_call(ui_services, qt_app) -> None:
    page = ChatPage(ui_services)
    page._on_tool_started("add_task", {"title": "Revise op-amps"})
    assert "add_task" in page._tool_cards

    class Outcome:
        tool = "add_task"
        status = "ok"

        class result:  # noqa: N801 - mirroring the real object
            summary = "Added 'Revise op-amps'."
            error = ""

    page._on_tool_finished(Outcome())
    assert page._tool_cards["add_task"].status == "ok"


def test_tasks_page_adds_and_lists(ui_services, qt_app) -> None:
    page = TasksPage(ui_services)
    page.new_title.setText("finish the lab record tomorrow 6pm")
    page.add_task()
    assert page.task_list.count() == 1
    assert "lab record" in page.task_list.item(0).text()
    assert ui_services.tasks.list(status="open")[0].due_at.hour == 18


def test_tasks_page_completes_a_task(ui_services, qt_app) -> None:
    page = TasksPage(ui_services)
    task = ui_services.tasks.create("Read chapter four")
    page.refresh()
    page.task_list.setCurrentRow(0)
    page._complete_selected()
    assert ui_services.tasks.get(int(task.id)).status == "done"


def test_tasks_page_sets_a_reminder(ui_services, qt_app) -> None:
    page = TasksPage(ui_services)
    page.reminder_title.setText("stand up and stretch")
    page.reminder_time.setText("in 30 minutes")
    page.add_reminder()
    assert ui_services.reminder_repo.count() == 1
    assert "Set for" in page.reminder_hint.text()


def test_tasks_page_focus_round_trip(ui_services, qt_app) -> None:
    page = TasksPage(ui_services)
    page.start_focus(25)
    assert ui_services.focus.active() is not None
    page.end_focus(completed=True)
    assert ui_services.focus.active() is None
    assert "This week" in page.focus_stats.text()


def test_notes_page_quick_capture(ui_services, qt_app) -> None:
    page = NotesPage(ui_services)
    page.quick_text.setText("op-amp gain = 1 + Rf/Ri")
    page.quick_capture()
    assert page.list_widget.count() == 1
    assert ui_services.notes.count() == 1


def test_memory_page_add_and_delete(ui_services, qt_app) -> None:
    page = MemoryPage(ui_services)
    page.content_box.setPlainText("I am in my third year of ECE")
    page.add_memory()
    assert page.list_widget.count() == 1
    assert ui_services.memory.total() == 1

    page.list_widget.setCurrentRow(0)
    page.delete_current()
    assert ui_services.memory.total() == 0


def test_memory_page_refuses_secrets(ui_services, qt_app) -> None:
    page = MemoryPage(ui_services)
    page.content_box.setPlainText("my password: hunter2000")
    page.add_memory()
    assert ui_services.memory.total() == 0
    assert "password" in page.status_label.text().lower() or "not" in page.status_label.text().lower()


def test_settings_page_saves_permissions(ui_services, qt_app) -> None:
    page = SettingsPage(ui_services)
    assert page.allow_screenshots.isChecked() is False
    page.allow_screenshots.setChecked(True)
    page.save()
    assert ui_services.settings_store.settings.automation.allow_screenshots is True
    page.allow_screenshots.setChecked(False)
    page.save()
    assert ui_services.settings_store.settings.automation.allow_screenshots is False


def test_settings_page_rejects_a_bad_base_url(ui_services, qt_app) -> None:
    page = SettingsPage(ui_services)
    page.base_url.setText("not a url")
    page.save()
    assert ui_services.settings_store.settings.ollama.base_url.startswith("http://")


# ---------------------------------------------------------------------------
# confirmation dialog
# ---------------------------------------------------------------------------
def test_confirmation_dialog_defaults_to_no(qt_app) -> None:
    request = ConfirmationRequest(tool="send_email", title="Send this email?", details="To: tutor@college.edu")
    dialog = ConfirmationDialog(request)
    dialog.close()
    assert dialog.answer == ANSWER_NO


def test_confirmation_dialog_approves(qt_app) -> None:
    request = ConfirmationRequest(tool="delete_files", title="Delete 3 files?", details="...", risk=RiskLevel.CONSEQUENTIAL)
    dialog = ConfirmationDialog(request)
    dialog._approve()
    assert dialog.answer == ANSWER_YES


def test_confirmation_dialog_session_grant(qt_app) -> None:
    request = ConfirmationRequest(tool="open_app", title="Open Calculator?", details="Calculator")
    dialog = ConfirmationDialog(request)
    dialog.remember.setChecked(True)
    dialog._approve()
    assert dialog.answer == ANSWER_SESSION


def test_confirmation_hides_the_session_box_when_not_allowed(qt_app) -> None:
    request = ConfirmationRequest(tool="send_email", title="Send?", details="", allow_session_grant=False)
    dialog = ConfirmationDialog(request)
    assert dialog.remember.isVisible() is False


def test_main_window_builds_and_switches_pages(ui_services, qt_app) -> None:
    from lumi.ui.main_window import MainWindow

    window = MainWindow(ui_services)
    assert window.stack.count() == 5
    for index in range(window.stack.count()):
        window.sidebar.setCurrentRow(index)
        qt_app.processEvents()
        assert window.stack.currentIndex() == index
    window._quitting = True
    window.close()


def test_main_window_stop_button_sets_the_stop_flag(ui_services, qt_app) -> None:
    from lumi.ui.main_window import MainWindow

    window = MainWindow(ui_services)
    window.emergency_stop()
    assert ui_services.app.stopped is True
    window._quitting = True
    window.close()


def test_provider_answers_no_without_a_window(ui_services, qt_app) -> None:
    """Fail closed: no UI to ask means no permission."""
    from lumi.ui.confirm import DialogConfirmationProvider

    provider = DialogConfirmationProvider(None)
    request = ConfirmationRequest(tool="send_email", title="Send?", details="")
    # With no owner the provider still must not approve anything by itself.
    decision = provider(request)
    assert decision == ANSWER_NO
    assert provider.decisions == [("send_email", ANSWER_NO)]


def test_provider_asks_through_the_window(ui_services, qt_app) -> None:
    """With a window available, the dialog is reached on the UI thread."""
    from lumi.ui.confirm import DialogConfirmationProvider

    asked: list[str] = []

    class Owner:
        def run_on_ui_thread(self, work) -> None:
            work()

    provider = DialogConfirmationProvider(Owner())
    provider.ask_on_ui_thread = lambda request: (asked.append(request.tool), ANSWER_YES)[1]  # type: ignore[method-assign]
    answer = provider(ConfirmationRequest(tool="open_app", title="Open Calculator?", details="Calculator"))
    assert answer == ANSWER_YES
    assert asked == ["open_app"]
    assert provider.decisions == [("open_app", ANSWER_YES)]


def test_provider_fails_closed_when_the_ui_raises(ui_services, qt_app) -> None:
    from lumi.ui.confirm import DialogConfirmationProvider

    class Broken:
        def run_on_ui_thread(self, work) -> None:
            raise RuntimeError("window gone")

    provider = DialogConfirmationProvider(Broken())
    assert provider(ConfirmationRequest(tool="send_email", title="Send?", details="")) == ANSWER_NO


# ---------------------------------------------------------------------------
# compact (floating) mode
# ---------------------------------------------------------------------------
def test_compact_mode_hides_the_sidebar_and_is_remembered(ui_services, qt_app) -> None:
    from lumi.ui.main_window import MainWindow

    window = MainWindow(ui_services)
    window.show()
    qt_app.processEvents()
    assert window.sidebar_card.isVisible()

    window.toggle_compact_mode()
    qt_app.processEvents()
    assert ui_services.settings_store.settings.appearance.window_mode == "compact"
    assert ui_services.settings_store.settings.appearance.compact_mode is True
    assert not window.sidebar_card.isVisible(), "the sidebar must go away in compact mode"
    assert window.width() <= 460

    # The choice survives a restart.
    saved = ui_services.settings_store.settings.appearance
    assert saved.window_mode == "compact"
    window.toggle_compact_mode()
    qt_app.processEvents()
    assert window.sidebar_card.isVisible()
    assert ui_services.settings_store.settings.appearance.compact_mode is False

    window._quitting = True
    window.close()


def test_a_saved_compact_setting_is_applied_on_start(ui_services, qt_app) -> None:
    from lumi.ui.main_window import MainWindow

    appearance = ui_services.settings_store.settings.appearance
    appearance.window_mode = "compact"
    appearance.sync_compact_flag()

    window = MainWindow(ui_services)
    window.show()
    qt_app.processEvents()
    assert not window.sidebar_card.isVisible()
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint

    window._quitting = True
    window.close()


def test_show_briefing_always_puts_something_in_the_chat(ui_services, qt_app) -> None:
    """The briefing must never crash the window - the fast path may be absent."""
    from lumi.ui.main_window import MainWindow

    window = MainWindow(ui_services)
    before = window.chat_page.transcript.count()
    window.show_briefing()
    qt_app.processEvents()
    assert window.chat_page.transcript.count() > before

    window._quitting = True
    window.close()


def test_the_compact_shortcut_is_registered(ui_services, qt_app) -> None:
    from PySide6.QtGui import QKeySequence

    from lumi.ui.main_window import MainWindow

    window = MainWindow(ui_services)
    sequences = {shortcut.key().toString() for shortcut in window.findChildren(QShortcut)}
    assert QKeySequence("Ctrl+Shift+M").toString() in sequences
    window._quitting = True
    window.close()


# ---------------------------------------------------------------------------
# settings that used to be decorative
# ---------------------------------------------------------------------------
def test_hiding_timestamps_affects_new_bubbles(ui_services, qt_app) -> None:
    ui_services.settings_store.settings.appearance.show_timestamps = False
    page = ChatPage(ui_services)
    bubble = page.add_bubble("user", "hello")
    assert bubble.time_label.isHidden()

    ui_services.settings_store.settings.appearance.show_timestamps = True
    visible = page.add_bubble("assistant", "hi")
    assert not visible.time_label.isHidden()


def test_the_startup_checkbox_writes_and_removes_a_launcher(ui_services, qt_app, tmp_path, monkeypatch) -> None:
    """Ticking "start with Windows" must actually create the launcher."""
    from lumi.config import startup

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    page = SettingsPage(ui_services)
    launcher = startup.launcher_path(platform="linux")
    assert not launcher.exists()

    page.launch_on_login.setChecked(True)
    page.save()
    assert launcher.exists(), "the checkbox must create the start-up entry"

    page.launch_on_login.setChecked(False)
    page.save()
    assert not launcher.exists(), "unticking it must remove the entry again"
