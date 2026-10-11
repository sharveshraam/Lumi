"""The memory viewer and editor - the user's own data, fully editable."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lumi.memory.models import KIND_LABELS, MEMORY_KINDS
from lumi.security.redact import redact_text
from lumi.ui.theme import Palette
from lumi.ui.widgets.common import Divider, GlassCard, Row, SectionTitle

log = logging.getLogger("lumi.ui.memory")

KIND_CHOICES: tuple[tuple[str, str], ...] = (
    ("all", "Everything"),
    *[(kind, KIND_LABELS.get(kind, kind)) for kind in MEMORY_KINDS if kind != "session"],
)


class MemoryPage(QWidget):
    """Search, edit and delete what LUMI remembers, and export/import it."""

    changed = Signal()

    def __init__(self, services: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.services = services
        self.current_id: Optional[int] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        header = GlassCard(padding=12)
        head_row = Row(spacing=8)
        title = QLabel("Memory")
        title.setObjectName("Title")
        head_row.add(title)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search what LUMI remembers…")
        self.search_box.textChanged.connect(self.refresh)
        head_row.add(self.search_box, 1)

        self.kind_filter = QComboBox()
        for value, label in KIND_CHOICES:
            self.kind_filter.addItem(label, value)
        self.kind_filter.currentIndexChanged.connect(self.refresh)
        head_row.add(self.kind_filter)
        header.body().addWidget(head_row)

        self.counts_label = QLabel("")
        self.counts_label.setObjectName("Faint")
        header.body().addWidget(self.counts_label)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(10)

        left = GlassCard(padding=10)
        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self._on_selected)
        left.body().addWidget(self.list_widget, 1)
        body.addWidget(left, 3)

        right = GlassCard(padding=12)
        right.body().addWidget(SectionTitle("Edit this memory"))
        self.kind_box = QComboBox()
        for kind in MEMORY_KINDS:
            if kind != "session":
                self.kind_box.addItem(KIND_LABELS.get(kind, kind), kind)
        right.body().addWidget(self.kind_box)

        self.key_box = QLineEdit()
        self.key_box.setPlaceholderText("key (optional) - e.g. 'preferred_language'")
        right.body().addWidget(self.key_box)

        self.content_box = QPlainTextEdit()
        self.content_box.setPlaceholderText("What should LUMI remember?")
        right.body().addWidget(self.content_box, 1)

        self.status_label = QLabel("Nothing selected.")
        self.status_label.setObjectName("Faint")
        self.status_label.setWordWrap(True)
        right.body().addWidget(self.status_label)

        buttons = Row(spacing=8)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.clicked.connect(self.save_current)
        buttons.add(save)
        add = QPushButton("Add new")
        add.clicked.connect(self.add_memory)
        buttons.add(add)
        delete = QPushButton("Delete")
        delete.setObjectName("Danger")
        delete.clicked.connect(self.delete_current)
        buttons.add(delete)
        buttons.add_stretch()
        right.body().addWidget(buttons)

        right.body().addWidget(Divider())
        io_row = Row(spacing=8)
        export = QPushButton("Export…")
        export.setToolTip("Write every memory to a JSON file you can keep or move to another machine.")
        export.clicked.connect(self.export_memories)
        io_row.add(export)
        import_button = QPushButton("Import…")
        import_button.clicked.connect(self.import_memories)
        io_row.add(import_button)
        io_row.add_stretch()
        right.body().addWidget(io_row)

        note = QLabel("LUMI refuses to store passwords, tokens and card-like numbers, and it will not remember anything you did not ask it to.")
        note.setObjectName("Faint")
        note.setWordWrap(True)
        right.body().addWidget(note)
        body.addWidget(right, 4)

        root.addLayout(body, 1)
        self.refresh()

    # ------------------------------------------------------------- painting
    def refresh(self) -> None:
        query = self.search_box.text().strip()
        # "Everything" is not a real kind - it means "no filter".
        kind = self.kind_filter.currentData() or None
        if kind == "all":
            kind = None
        try:
            if query:
                memories = self.services.memory.search(query, kinds=[kind] if kind else None, limit=200)
            else:
                memories = self.services.memory.list(kind=kind, limit=200)
            memories = [memory for memory in memories if memory.kind != "session"]
            counts = self.services.memory.counts_by_kind()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load memories: %s", exc)
            return

        self.list_widget.clear()
        for memory in memories:
            label = f"[{KIND_LABELS.get(memory.kind, memory.kind)}] {redact_text(memory.content, max_length=90)}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, memory.id if memory.id is not None else -1)
            if memory.pinned:
                item.setForeground(Qt.GlobalColor.cyan)
            self.list_widget.addItem(item)

        summary = " · ".join(f"{KIND_LABELS.get(k, k)}: {v}" for k, v in sorted(counts.items()) if v)
        self.counts_label.setText(f"{len(memories)} shown · {summary or 'nothing stored yet'}")

    def _on_selected(self, current: Optional[QListWidgetItem], _previous: Any = None) -> None:
        if current is None:
            self.current_id = None
            return
        memory_id = current.data(Qt.ItemDataRole.UserRole)
        if memory_id is None or int(memory_id) < 0:
            self.current_id = None
            self.status_label.setText("This entry only exists for the current session.")
            return
        memory = self.services.memory.get(int(memory_id))
        if memory is None:
            return
        self.current_id = memory.id
        index = self.kind_box.findData(memory.kind)
        if index >= 0:
            self.kind_box.setCurrentIndex(index)
        self.key_box.setText(memory.key)
        self.content_box.setPlainText(memory.content)
        self.status_label.setText(
            f"Stored {memory.created_at:%d %b %Y %H:%M}" if memory.created_at else ""
        )

    # -------------------------------------------------------------- actions
    def save_current(self) -> None:
        if self.current_id is None:
            self.status_label.setText("Pick a memory on the left, or use 'Add new'.")
            return
        try:
            self.services.memory.update(
                int(self.current_id),
                content=self.content_box.toPlainText().strip(),
                kind=self.kind_box.currentData(),
                key=self.key_box.text().strip(),
            )
            self.status_label.setText("Saved.")
            self.changed.emit()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Not saved: {exc}")
        self.refresh()

    def add_memory(self) -> None:
        content = self.content_box.toPlainText().strip()
        if not content:
            self.status_label.setText("Type what you want remembered first.")
            return
        try:
            memory = self.services.memory.add(
                content,
                kind=self.kind_box.currentData() or "fact",
                key=self.key_box.text().strip(),
                source="ui",
            )
            self.current_id = memory.id
            self.status_label.setText("Added.")
            self.changed.emit()
        except ValueError as exc:
            # The service refuses secrets - say so plainly.
            self.status_label.setText(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Not added: {exc}")
        self.refresh()

    def delete_current(self) -> None:
        if self.current_id is None:
            self.status_label.setText("Pick a memory first.")
            return
        self.services.memory.delete(int(self.current_id))
        self.current_id = None
        self.content_box.clear()
        self.key_box.clear()
        self.status_label.setText("Deleted.")
        self.changed.emit()
        self.refresh()

    def export_memories(self) -> None:
        default = str(Path.home() / "lumi-memory-export.json")
        target, _ = QFileDialog.getSaveFileName(self, "Export memories", default, "JSON files (*.json)")
        if not target:
            return
        try:
            payload = self.services.memory.export_payload()
            Path(target).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            self.status_label.setText(f"Exported {len(payload.get('memories', []))} memories.")
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Export failed: {exc}")

    def import_memories(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "Import memories", str(Path.home()), "JSON files (*.json)")
        if not source:
            return
        try:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
            stats = self.services.memory.import_payload(payload)
            self.status_label.setText(
                f"Imported: {stats.get('added', 0)} added, {stats.get('updated', 0)} updated, {stats.get('skipped', 0)} skipped."
            )
            self.changed.emit()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Import failed: {exc}")
        self.refresh()
