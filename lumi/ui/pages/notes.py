"""Notes: quick capture on the left, the note itself on the right."""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from lumi.ui.theme import Palette
from lumi.ui.widgets.common import GlassCard, Pill, Row

log = logging.getLogger("lumi.ui.notes")


class NotesPage(QWidget):
    """Create, search, edit, pin and delete notes."""

    changed = Signal()

    def __init__(self, services: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.services = services
        self.current_id: Optional[int] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header = Row(spacing=8)
        title = QLabel("Notes")
        title.setObjectName("H2")
        header.add(title)
        header.add_stretch()
        self.count_pill = Pill("0 notes", Palette.TEXT_MUTED)
        header.add(self.count_pill)
        layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_list())
        splitter.addWidget(self._build_editor())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.refresh()

    # -------------------------------------------------------------------- list
    def _build_list(self) -> QWidget:
        card = GlassCard(padding=12)
        column = QVBoxLayout()
        column.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search notes…")
        self.search.textChanged.connect(lambda _text: self.refresh())
        column.addWidget(self.search)

        new_row = Row(spacing=8)
        self.quick_text = QLineEdit()
        self.quick_text.setPlaceholderText("Quick capture: op-amp gain = 1 + Rf/Ri")
        self.quick_text.returnPressed.connect(self.quick_capture)
        new_row.add(self.quick_text, 1)
        add = QPushButton("Save")
        add.setObjectName("Primary")
        add.clicked.connect(self.quick_capture)
        new_row.add(add)
        column.addWidget(new_row)

        only_pinned = QCheckBox("Pinned only")
        only_pinned.toggled.connect(lambda _checked: self.refresh())
        self.only_pinned = only_pinned
        column.addWidget(only_pinned)

        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self._on_selected)
        column.addWidget(self.list_widget, 1)

        card.body().addLayout(column)
        return card

    # ------------------------------------------------------------------ editor
    def _build_editor(self) -> QWidget:
        card = GlassCard(padding=12)
        column = QVBoxLayout()
        column.setSpacing(8)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Title")
        column.addWidget(self.title_edit)

        self.body_edit = QPlainTextEdit()
        self.body_edit.setPlaceholderText("Write here. Nothing is saved until you press Save.")
        column.addWidget(self.body_edit, 1)

        self.status = QLabel("")
        self.status.setObjectName("Faint")
        column.addWidget(self.status)

        buttons = Row(spacing=8)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.clicked.connect(self.save_current)
        self.pin_button = QPushButton("Pin")
        self.pin_button.clicked.connect(self.toggle_pin)
        remove = QPushButton("Delete")
        remove.setObjectName("Danger")
        remove.clicked.connect(self.delete_current)
        buttons.add(save)
        buttons.add(self.pin_button)
        buttons.add_stretch()
        buttons.add(remove)
        column.addWidget(buttons)

        card.body().addLayout(column)
        return card

    # ----------------------------------------------------------------- actions
    def quick_capture(self) -> None:
        title = self.quick_text.text().strip()
        if not title:
            return
        note = self.services.notes.create(title=title)
        self.quick_text.clear()
        self.refresh()
        self._select(note.id)
        self.changed.emit()

    def _select(self, note_id: int) -> None:
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == note_id:
                self.list_widget.setCurrentItem(item)
                return

    def _on_selected(self, item: Optional[QListWidgetItem], _previous=None) -> None:
        if item is None:
            return
        note_id = item.data(Qt.ItemDataRole.UserRole)
        note = self.services.notes.get(note_id)
        if note is None:
            return
        self.current_id = note.id
        self.title_edit.setText(note.title)
        self.body_edit.setPlainText(note.body)
        self.pin_button.setText("Unpin" if note.pinned else "Pin")
        self.status.setText(f"#{note.id} · updated {note.updated_at:%d %b %H:%M}" if note.updated_at else "")

    def save_current(self) -> None:
        title = self.title_edit.text().strip()
        if not title:
            self.status.setText("A note needs a title.")
            self.status.setStyleSheet(f"color: {Palette.WARNING};")
            return
        body = self.body_edit.toPlainText()
        try:
            if self.current_id is None:
                note = self.services.notes.create(title=title, body=body)
                self.current_id = note.id
            else:
                self.services.notes.update(self.current_id, title=title, body=body)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save the note: %s", exc)
            self.status.setText(f"Could not save: {exc}")
            self.status.setStyleSheet(f"color: {Palette.DANGER};")
            return
        self.status.setText("Saved.")
        self.status.setStyleSheet(f"color: {Palette.OK};")
        self.refresh()
        self._select(self.current_id)
        self.changed.emit()

    def toggle_pin(self) -> None:
        if self.current_id is None:
            return
        note = self.services.notes.toggle_pin(self.current_id)
        if note is not None:
            self.pin_button.setText("Unpin" if note.pinned else "Pin")
        self.refresh()
        self._select(self.current_id)

    def delete_current(self) -> None:
        if self.current_id is None:
            return
        self.services.notes.delete(self.current_id)
        self.current_id = None
        self.title_edit.clear()
        self.body_edit.clear()
        self.status.setText("Deleted.")
        self.refresh()
        self.changed.emit()

    # ---------------------------------------------------------------- refresh
    def refresh(self) -> None:
        query = self.search.text().strip()
        pinned_only = getattr(self, "only_pinned", None)
        try:
            if query:
                notes = self.services.notes.search(query, limit=100)
            else:
                notes = self.services.notes.list(limit=200)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not list notes: %s", exc)
            notes = []
        if pinned_only is not None and pinned_only.isChecked():
            notes = [note for note in notes if note.pinned]

        self.list_widget.clear()
        for note in notes:
            mark = "📌" if note.pinned else "•"
            first_line = note.excerpt(length=60)
            item = QListWidgetItem(f"{mark}  {note.title}\n     {first_line}")
            item.setData(Qt.ItemDataRole.UserRole, note.id)
            self.list_widget.addItem(item)

        try:
            total = self.services.notes.count()
        except Exception:  # noqa: BLE001
            total = len(notes)
        self.count_pill.set_state(f"{total} notes", Palette.TEXT_MUTED)


__all__ = ["NotesPage"]
