"""Chat building blocks: message bubbles, the pending-question card, notes."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from lumi.ui.theme import Palette, font
from lumi.ui.widgets.common import GlassCard, IconButton, MarkdownView, Pill, Row, _alpha


class ChatBubble(GlassCard):
    """One message in the transcript."""

    copy_requested = Signal(str)
    link_requested = Signal(str)

    def __init__(self, role: str, text: str = "", *, timestamp: Optional[datetime] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, strong=(role == "user"), padding=12)
        self.role = role
        self._text = text

        header = Row(spacing=8)
        self.name_label = QLabel(_display_name(role))
        self.name_label.setFont(font(10, weight=QFont.Weight.DemiBold))
        header.add(self.name_label)
        self.badge = Pill("thinking", Palette.INFO)
        self.badge.hide()
        header.add(self.badge)
        header.add_stretch()
        self.time_label = QLabel((timestamp or datetime.now()).strftime("%H:%M"))
        self.time_label.setObjectName("Faint")
        header.add(self.time_label)
        self.copy_button = IconButton("⧉", "Copy this message")
        self.copy_button.clicked.connect(lambda: self.copy_requested.emit(self._text))
        self.copy_button.hide()
        header.add(self.copy_button)
        self.body().addWidget(header)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("Faint")
        self.meta_label.hide()
        self.body().addWidget(self.meta_label)

        self.view = MarkdownView()
        self.view.link_clicked.connect(self.link_requested.emit)
        self.view.set_text(text)
        self.body().addWidget(self.view)

        self.setStyleSheet(
            f"QFrame#Glass, QFrame#GlassStrong {{ background: {_bubble_colour(role)};"
            f" border: 1px solid {Palette.BORDER}; border-radius: 14px; }}"
        )

    # ------------------------------------------------------------------ update
    def set_text(self, text: str) -> None:
        self._text = text
        self.view.set_text(text)

    def append(self, text: str) -> None:
        self._text += text
        self.view.append_text(text)

    def text(self) -> str:
        return self._text

    def set_state(self, state: str, colour: str) -> None:
        if state:
            self.badge.set_state(state, colour)
            self.badge.show()
        else:
            self.badge.hide()

    def finish(self) -> None:
        self.badge.hide()
        self.copy_button.show()

    def show_meta(self, text: str) -> None:
        """A quiet line under the message: which model, how long it took."""
        if not text:
            return
        self.meta_label.setText(text)
        self.meta_label.show()

    def set_timestamp_visible(self, visible: bool) -> None:
        self.time_label.setVisible(bool(visible))


def _display_name(role: str) -> str:
    return {"user": "You", "assistant": "LUMI", "system": "LUMI", "tool": "Tool"}.get(role, role.title())


def _bubble_colour(role: str) -> str:
    if role == "user":
        return Palette.USER_BUBBLE
    if role == "system":
        return Palette.SYSTEM_BUBBLE
    return Palette.ASSISTANT_BUBBLE


class SystemNote(QLabel):
    """A quiet one-line note in the transcript (status, errors, hints)."""

    def __init__(self, text: str, *, colour: str = Palette.TEXT_MUTED, parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setFont(font(10))
        self.setStyleSheet(
            f"QLabel {{ color: {colour}; background: {_alpha(colour, 0.10)};"
            f" border: 1px solid {_alpha(colour, 0.28)}; border-radius: 10px; padding: 8px 12px; }}"
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)


class PendingQuestion(GlassCard):
    """The yes/no card shown when LUMI wants confirmation before saving."""

    answered = Signal(bool)

    def __init__(self, question: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, strong=True, padding=12)
        label = QLabel("Needs your confirmation")
        label.setFont(font(10, weight=QFont.Weight.DemiBold))
        label.setStyleSheet(f"color: {Palette.WARNING};")
        self.body().addWidget(label)

        self.question = QLabel(question)
        self.question.setWordWrap(True)
        self.body().addWidget(self.question)

        buttons = Row(spacing=8)
        yes = IconButton("Yes, save it", "Save what LUMI described")
        yes.setText("Yes")
        yes.setObjectName("Primary")
        no = IconButton("No", "Do not save anything")
        yes.clicked.connect(lambda: self.answered.emit(True))
        no.clicked.connect(lambda: self.answered.emit(False))
        buttons.add(yes)
        buttons.add(no)
        buttons.add_stretch()
        self.body().addWidget(buttons)


class SuggestionCard(GlassCard):
    """A gentle prompt - currently used to offer saving a memory."""

    accepted = Signal(str)
    dismissed = Signal()

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, padding=12)
        label = QLabel("Save this to memory?")
        label.setFont(font(10, weight=QFont.Weight.DemiBold))
        label.setStyleSheet(f"color: {Palette.INFO};")
        self.body().addWidget(label)
        body = QLabel(text)
        body.setWordWrap(True)
        self.body().addWidget(body)

        buttons = Row(spacing=8)
        save = IconButton("Save", "")
        save.setText("Save to memory")
        save.setObjectName("Primary")
        ignore = IconButton("Not now", "")
        save.clicked.connect(lambda: self.accepted.emit(text))
        ignore.clicked.connect(self.dismissed.emit)
        buttons.add(save)
        buttons.add(ignore)
        buttons.add_stretch()
        self.body().addWidget(buttons)


class ToolCallCard(GlassCard):
    """Shows one tool call: what it was, whether it ran, and the result."""

    def __init__(self, name: str, arguments: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, padding=10)
        self.name = name
        self.arguments = dict(arguments or {})
        self.status = "running"

        header = Row(spacing=8)
        self.icon = QLabel("⚙")
        header.add(self.icon)
        self.title = QLabel(name)
        self.title.setFont(font(10, weight=QFont.Weight.DemiBold, mono=True))
        header.add(self.title)
        self.detail = QLabel(_short_args(self.arguments))
        self.detail.setObjectName("Faint")
        self.detail.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        header.add(self.detail, 1)
        self.state = Pill("running", Palette.INFO)
        header.add(self.state)
        self.body().addWidget(header)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setObjectName("Muted")
        self.result.hide()
        self.body().addWidget(self.result)

    def finish(self, status: str, summary: str = "", colour: str = "") -> None:
        from lumi.ui.theme import status_colour

        self.status = status
        colour = colour or status_colour(status)
        self.state.set_state(status, colour)
        self.icon.setText({"ok": "✓", "error": "✕", "denied": "⛔", "declined": "⛔", "invalid": "!"}.get(status, "•"))
        if summary:
            self.result.setText(summary)
            self.result.show()
            if status.startswith(("error", "invalid", "denied", "declined")):
                self.result.setStyleSheet(f"color: {colour};")


def _short_args(arguments: dict, limit: int = 90) -> str:
    if not arguments:
        return ""
    parts = []
    for key, value in arguments.items():
        text = str(value).replace("\n", " ")
        if len(text) > 40:
            text = text[:37] + "..."
        parts.append(f"{key}={text}")
    joined = ", ".join(parts)
    return joined[:limit] + ("..." if len(joined) > limit else "")
