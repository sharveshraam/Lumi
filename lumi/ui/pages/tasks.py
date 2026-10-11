"""Tasks, reminders and focus sessions in one page.

Four tabs, all of them plain lists over the repositories.  Nothing here talks to
a model: adding a task from this page is a database write.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from lumi.ui.theme import Palette
from lumi.ui.widgets.common import GlassCard, Pill, Row
from lumi.utils.timeutil import humanise_due, now as current_time

log = logging.getLogger("lumi.ui.tasks")

from lumi.tasks.models import PRIORITIES  # the repository's own list


class TasksPage(QWidget):
    """Tasks and reminders, with a focus timer."""

    changed = Signal()

    def __init__(self, services: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.services = services

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.summary = Pill("no tasks yet", Palette.TEXT_MUTED)
        header = Row(spacing=8)
        title = QLabel("Tasks & reminders")
        title.setObjectName("H2")
        header.add(title)
        header.add_stretch()
        header.add(self.summary)
        layout.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_tasks_tab(), "Tasks")
        self.tabs.addTab(self._build_reminders_tab(), "Reminders")
        self.tabs.addTab(self._build_focus_tab(), "Focus")
        layout.addWidget(self.tabs, 1)

        self.refresh()

    # ------------------------------------------------------------------- tasks
    def _build_tasks_tab(self) -> QWidget:
        card = GlassCard(padding=12)
        column = QVBoxLayout()
        column.setSpacing(8)

        add_row = Row(spacing=8)
        self.new_title = QLineEdit()
        self.new_title.setPlaceholderText("What needs doing?")
        self.new_title.returnPressed.connect(self.add_task)
        add_row.add(self.new_title, 1)

        self.new_when = QLineEdit()
        self.new_when.setPlaceholderText("when (optional, e.g. tomorrow 6pm)")
        self.new_when.setMaximumWidth(240)
        add_row.add(self.new_when)

        self.new_priority = QComboBox()
        self.new_priority.addItems(PRIORITIES)
        self.new_priority.setCurrentText("medium")
        add_row.add(self.new_priority)

        add_button = QPushButton("Add")
        add_button.setObjectName("Primary")
        add_button.clicked.connect(self.add_task)
        add_row.add(add_button)
        column.addWidget(add_row)

        self.filter_box = QComboBox()
        self.filter_box.addItem("Open tasks", "open")
        self.filter_box.addItem("Today", "today")
        self.filter_box.addItem("Overdue", "overdue")
        self.filter_box.addItem("Completed", "done")
        self.filter_box.addItem("Everything", "all")
        self.filter_box.currentIndexChanged.connect(lambda _index: self.refresh())

        self.hide_done = QCheckBox("Hide completed")
        self.hide_done.setChecked(True)
        self.hide_done.toggled.connect(lambda _checked: self.refresh())

        filters = Row(spacing=8)
        filters.add(QLabel("Show"))
        filters.add(self.filter_box)
        filters.add(self.hide_done)
        filters.add_stretch()
        column.addWidget(filters)

        self.task_list = QListWidget()
        self.task_list.itemDoubleClicked.connect(self._toggle_task)
        column.addWidget(self.task_list, 1)

        buttons = Row(spacing=8)
        complete = QPushButton("Complete")
        complete.clicked.connect(self._complete_selected)
        postpone = QPushButton("Postpone 1 day")
        postpone.clicked.connect(lambda: self._postpone(1))
        postpone_week = QPushButton("Postpone 1 week")
        postpone_week.clicked.connect(lambda: self._postpone(7))
        remove = QPushButton("Delete")
        remove.setObjectName("Danger")
        remove.clicked.connect(self.delete_selected)
        for button in (complete, postpone, postpone_week):
            buttons.add(button)
        buttons.add_stretch()
        buttons.add(remove)
        column.addWidget(buttons)

        card.body().addLayout(column)
        return card

    def add_task(self) -> None:
        """Add a task, understanding a date written either in the title or the box."""
        raw = self.new_title.text().strip()
        if not raw:
            return

        from lumi.tasks.nlp import parse_task_text

        parsed = parse_task_text(raw)
        title = (parsed.title or raw).strip()
        due = parsed.due_at
        when = self.new_when.text().strip()
        if when:
            from lumi.tools.builtin._common import parse_when

            interpreted = parse_when(when)
            if interpreted is not None:
                due = interpreted
        priority = self.new_priority.currentText()
        if parsed.priority in PRIORITIES:
            priority = parsed.priority
        try:
            self.services.tasks.create(title=title, due_at=due, priority=priority)
        except Exception as exc:  # noqa: BLE001 - show it rather than crash
            log.warning("could not add task: %s", exc)
            self.summary.set_state("could not save", Palette.DANGER)
            return
        self.new_title.clear()
        self.new_when.clear()
        self.refresh()
        self.changed.emit()

    def _selected_id(self) -> Optional[int]:
        item = self.task_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _complete_selected(self) -> None:
        task_id = self._selected_id()
        if task_id is None:
            return
        self.services.tasks.complete(task_id)
        self.refresh()
        self.changed.emit()

    def _toggle_task(self, item: QListWidgetItem) -> None:
        task_id = item.data(Qt.ItemDataRole.UserRole)
        task = self.services.tasks.get(task_id)
        if task is None:
            return
        self.services.tasks.complete(task_id, completed=task.status != "done")
        self.refresh()
        self.changed.emit()

    def _postpone(self, days: int) -> None:
        task_id = self._selected_id()
        if task_id is None:
            return
        from lumi.utils.timeutil import add_days, now

        task = self.services.tasks.get(task_id)
        base = task.due_at if task is not None and task.due_at else now()
        self.services.tasks.update(task_id, due_at=add_days(base, days))
        self.refresh()
        self.changed.emit()

    def delete_selected(self) -> None:
        task_id = self._selected_id()
        if task_id is None:
            return
        self.services.tasks.delete(task_id)
        self.refresh()
        self.changed.emit()

    # --------------------------------------------------------------- reminders
    def _build_reminders_tab(self) -> QWidget:
        card = GlassCard(padding=12)
        column = QVBoxLayout()
        column.setSpacing(8)

        add_row = Row(spacing=8)
        self.reminder_title = QLineEdit()
        self.reminder_title.setPlaceholderText("Remind me to…")
        add_row.add(self.reminder_title, 1)
        self.reminder_time = QLineEdit()
        self.reminder_time.setPlaceholderText("when (e.g. in 20 minutes, tomorrow 9am)")
        self.reminder_time.setMaximumWidth(280)
        add_row.add(self.reminder_time)
        add = QPushButton("Set reminder")
        add.setObjectName("Primary")
        add.clicked.connect(self.add_reminder)
        add_row.add(add)
        column.addWidget(add_row)

        self.reminder_hint = QLabel("Set a reminder and it will appear here, and as a Windows notification.")
        self.reminder_hint.setObjectName("Faint")
        self.reminder_hint.setWordWrap(True)
        column.addWidget(self.reminder_hint)

        self.reminder_list = QListWidget()
        column.addWidget(self.reminder_list, 1)

        buttons = Row(spacing=8)
        snooze = QPushButton("Snooze 10 minutes")
        snooze.clicked.connect(lambda: self._snooze(10))
        cancel = QPushButton("Cancel reminder")
        cancel.setObjectName("Danger")
        cancel.clicked.connect(self.cancel_reminder)
        buttons.add(snooze)
        buttons.add_stretch()
        buttons.add(cancel)
        column.addWidget(buttons)

        card.body().addLayout(column)
        return card

    def add_reminder(self) -> None:
        title = self.reminder_title.text().strip()
        when = self.reminder_time.text().strip()
        if not title or not when:
            self.reminder_hint.setText("A reminder needs something to say and a time.")
            return
        from lumi.tools.builtin._common import parse_when

        try:
            fire_at = parse_when(when)
            if fire_at is None:
                raise ValueError(f"I could not understand the time “{when}”.")
            reminder = self.services.reminders.create(title=title, fire_at=fire_at)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set reminder: %s", exc)
            self.reminder_hint.setText(f"Could not set that: {exc}")
            self.summary.set_state("could not save", Palette.DANGER)
            return
        self.reminder_title.clear()
        self.reminder_time.clear()
        self.reminder_hint.setText(f"Set for {humanise_due(reminder.fire_at)} — you will get a notification.")
        self.refresh()
        self.changed.emit()

    def _snooze(self, minutes: int) -> None:
        item = self.reminder_list.currentItem()
        if item is None:
            return
        try:
            self.services.reminders.snooze(item.data(Qt.ItemDataRole.UserRole), minutes=minutes)
        except Exception as exc:  # noqa: BLE001
            log.info("could not snooze: %s", exc)
        self.refresh()

    def cancel_reminder(self) -> None:
        item = self.reminder_list.currentItem()
        if item is None:
            return
        try:
            self.services.reminders.cancel(item.data(Qt.ItemDataRole.UserRole))
        except Exception as exc:  # noqa: BLE001
            log.info("could not cancel: %s", exc)
        self.refresh()
        self.changed.emit()

    # ------------------------------------------------------------------- focus
    def _build_focus_tab(self) -> QWidget:
        card = GlassCard(padding=12)
        column = QVBoxLayout()
        column.setSpacing(10)

        row = Row(spacing=8)
        self.focus_label = QLineEdit()
        self.focus_label.setPlaceholderText("What are you working on?")
        row.add(self.focus_label, 1)
        self.focus_minutes = QSpinBox()
        self.focus_minutes.setRange(5, 180)
        self.focus_minutes.setValue(int(self.services.settings_store.settings.notifications.focus_length_minutes))
        self.focus_minutes.setSuffix(" min")
        row.add(self.focus_minutes)
        start = QPushButton("Start focus")
        start.setObjectName("Primary")
        start.clicked.connect(lambda: self.start_focus())
        row.add(start)
        stop = QPushButton("Finish")
        stop.clicked.connect(lambda: self.end_focus())
        row.add(stop)
        column.addWidget(row)

        self.focus_status = QLabel("No focus session running.")
        self.focus_status.setObjectName("Muted")
        column.addWidget(self.focus_status)

        self.focus_stats = QLabel("")
        self.focus_stats.setObjectName("Faint")
        self.focus_stats.setWordWrap(True)
        column.addWidget(self.focus_stats)

        self.focus_list = QListWidget()
        column.addWidget(self.focus_list, 1)

        buttons = Row(spacing=8)
        completed = QPushButton("Finished it")
        completed.clicked.connect(lambda: self.end_focus(completed=True))
        stopped = QPushButton("Stopped early")
        stopped.clicked.connect(lambda: self.end_focus(completed=False))
        buttons.add(completed)
        buttons.add(stopped)
        buttons.add_stretch()
        column.addWidget(buttons)

        card.body().addLayout(column)
        return card

    def start_focus(self, minutes: Optional[int] = None) -> None:
        """Start a session.  ``minutes`` defaults to what the spin box says."""
        label = self.focus_label.text().strip() or "Focus session"
        planned = int(minutes if minutes is not None else self.focus_minutes.value())
        try:
            self.services.focus.start(label, planned_minutes=planned)
        except Exception as exc:  # noqa: BLE001 - a session already running, most likely
            log.info("could not start a focus session: %s", exc)
            self.focus_status.setText(str(exc))
            return
        self.refresh()
        self.changed.emit()

    def end_focus(self, *, completed: bool = True) -> None:
        """Finish the running session and record whether it was seen through."""
        try:
            self.services.focus.finish(completed=completed)
        except Exception as exc:  # noqa: BLE001
            log.info("could not finish the focus session: %s", exc)
        self.refresh()
        self.changed.emit()

    # ----------------------------------------------------------------- refresh
    def refresh(self) -> None:
        self._refresh_tasks()
        self._refresh_reminders()
        self._refresh_focus()
        self.refresh_summary()

    def _refresh_tasks(self) -> None:
        mode = self.filter_box.currentData()
        try:
            if mode == "today":
                tasks = self.services.tasks.due_today()
            elif mode == "overdue":
                tasks = self.services.tasks.overdue()
            elif mode == "done":
                tasks = self.services.tasks.list(status="done", limit=100)
            elif mode == "all":
                tasks = self.services.tasks.list(limit=200)
            else:
                tasks = self.services.tasks.list(status="open", limit=200)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not list tasks: %s", exc)
            return

        if self.hide_done.isChecked() and mode in ("all", "open"):
            tasks = [task for task in tasks if task.status != "done"]

        self.task_list.clear()
        for task in tasks:
            when = humanise_due(task.due_at) if task.due_at else "no date"
            mark = "✓" if task.status == "done" else "•"
            item = QListWidgetItem(f"{mark}  #{task.id}  {task.title}   —   {when}  [{task.priority}]")
            item.setData(Qt.ItemDataRole.UserRole, task.id)
            if task.is_overdue(current_time()):
                item.setForeground(Qt.GlobalColor.red)
            self.task_list.addItem(item)

    def _refresh_reminders(self) -> None:
        self.reminder_list.clear()
        try:
            upcoming = self.services.reminders.repo.upcoming(limit=40)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not list reminders: %s", exc)
            return
        for reminder in upcoming:
            repeat = f"  ({reminder.recurrence})" if getattr(reminder, "recurrence", "none") != "none" else ""
            item = QListWidgetItem(f"⏰  #{reminder.id}  {reminder.title}   —   {humanise_due(reminder.fire_at)}{repeat}")
            item.setData(Qt.ItemDataRole.UserRole, reminder.id)
            self.reminder_list.addItem(item)
        if not upcoming:
            self.reminder_list.addItem("No reminders yet.")

    def _refresh_focus(self) -> None:
        self.focus_list.clear()
        try:
            active = self.services.focus.active()
            stats = self.services.focus.week_stats(days=7)
            sessions = self.services.focus.list(limit=15)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read focus sessions: %s", exc)
            return

        if active is not None:
            self.focus_status.setText(f"Focusing on “{active.label}” — started {humanise_due(active.started_at)}")
        else:
            self.focus_status.setText("No focus session running.")

        minutes = stats.get("total_minutes", 0) or 0
        self.focus_stats.setText(
            f"This week: {stats.get('sessions', 0)} sessions · {minutes:.0f} minutes"
            f" · today {self.services.focus.today_minutes():.0f} min"
        )
        for session in sessions:
            minutes = session.actual_minutes
            state = "finished" if session.completed else "stopped"
            self.focus_list.addItem(f"{session.label} — {minutes:.0f} min ({state})")

    def refresh_summary(self) -> None:
        try:
            stats = self.services.tasks.stats()
        except Exception:  # noqa: BLE001
            return
        overdue = stats.get("overdue", 0)
        text = f"{stats.get('open', 0)} open · {overdue} overdue"
        colour = Palette.DANGER if overdue else Palette.OK
        self.summary.set_state(text, colour)

    def set_summary(self, text: str) -> None:
        self.summary.set_state(text, Palette.TEXT_MUTED)


__all__ = ["TasksPage"]
