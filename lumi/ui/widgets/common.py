"""Small shared widgets: glass cards, pills, section headers, markdown text."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from lumi.ui.theme import Palette, font


class GlassCard(QFrame):
    """A rounded, translucent panel - the base container for the whole UI."""

    def __init__(self, parent: Optional[QWidget] = None, *, strong: bool = False, padding: int = 14) -> None:
        super().__init__(parent)
        self.setObjectName("GlassStrong" if strong else "Glass")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(10)

    def body(self) -> QVBoxLayout:
        return self._layout


class Divider(QFrame):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)


class SectionTitle(QLabel):
    def __init__(self, text: str, subtitle: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("SectionTitle")
        if subtitle:
            self.setToolTip(subtitle)


class Pill(QLabel):
    """A small rounded status chip, e.g. "Local model" or "Computer control off"."""

    def __init__(
        self,
        text: str = "",
        colour: str = Palette.TEXT_MUTED,
        tooltip: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(text, parent)
        self.setObjectName("Pill")
        self._colour = colour
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFont(font(9, weight=QFont.Weight.DemiBold))
        self.setContentsMargins(0, 0, 0, 0)
        if tooltip:
            self.setToolTip(tooltip)
        self._apply()

    def set_state(self, text: str, colour: str, tooltip: str = "") -> None:
        self.setText(text)
        self._colour = colour
        if tooltip:
            self.setToolTip(tooltip)
        self._apply()

    def set_colour(self, colour: str) -> None:
        """Recolour the chip without touching its text."""
        self._colour = colour
        self._apply()

    def _apply(self) -> None:
        self.setStyleSheet(
            f"QLabel#Pill {{ color: {self._colour};"
            f" background: {_alpha(self._colour, 0.16)};"
            f" border: 1px solid {_alpha(self._colour, 0.35)};"
            " border-radius: 9px; padding: 3px 10px; }"
        )
        self.adjustSize()


def _alpha(colour: str, alpha: float) -> str:
    """Turn '#rrggbb' into an rgba() string, passing through rgba() unchanged."""
    if colour.startswith("rgba") or colour.startswith("rgb"):
        return colour
    value = colour.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return colour
    return f"rgba({red}, {green}, {blue}, {alpha})"


class IconButton(QPushButton):
    def __init__(
        self,
        text: str,
        tooltip: str = "",
        parent: Optional[QWidget] = None,
        *,
        small: bool = False,
    ) -> None:
        super().__init__(text, parent)
        self.setObjectName("IconButton")
        if tooltip:
            self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        if small:
            self.setFixedSize(26, 26)


class LinkButton(QPushButton):
    def __init__(self, text: str, click: Callable[[], None], tooltip: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Ghost")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
        self.clicked.connect(click)


class MarkdownView(QTextBrowser):
    """Read-only text that understands the small markdown subset models emit.

    Links are shown but never opened automatically: clicking one asks the user
    first (handled by the chat page), because a local model can hallucinate URLs.
    """

    link_clicked = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.document().setDocumentMargin(0)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.anchorClicked.connect(lambda url: self.link_clicked.emit(url.toString()))
        self._raw = ""

    def set_text(self, text: str) -> None:
        self._raw = text or ""
        self.setMarkdown(_to_markdown(self._raw))
        self._fit_height()

    def append_text(self, text: str) -> None:
        self._raw += text or ""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text or "")
        self._fit_height()

    def raw_text(self) -> str:
        return self._raw

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        width = max(120, self.viewport().width())
        self.document().setTextWidth(width)
        height = int(self.document().size().height()) + 8
        self.setFixedHeight(max(24, height))


def _to_markdown(text: str) -> str:
    """Escape the little HTML that a model might emit, keep markdown code fences."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped


def copy_to_clipboard(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


class Row(QWidget):
    """A horizontal row of widgets with consistent spacing."""

    def __init__(self, *widgets: QWidget, spacing: int = 8, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(spacing)
        for widget in widgets:
            layout.addWidget(widget)

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self.layout().addWidget(widget, stretch)

    def add_stretch(self) -> None:
        self.layout().addStretch(1)
