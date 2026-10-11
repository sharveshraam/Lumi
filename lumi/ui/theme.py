"""Dark "glass" look and feel for LUMI.

Everything visual lives here: colours, spacing, and the Qt style sheet.  Keeping
it in one module means the UI files stay about behaviour, and re-theming later
does not mean hunting through widget code.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtCore import Qt


class Palette:
    """Named colours, so widgets never hard-code a hex string."""

    BG: Final = "#0b0d14"
    BG_ELEVATED: Final = "#11141f"
    GLASS: Final = "rgba(255, 255, 255, 0.045)"
    GLASS_STRONG: Final = "rgba(255, 255, 255, 0.075)"
    BORDER: Final = "rgba(255, 255, 255, 0.09)"
    BORDER_STRONG: Final = "rgba(255, 255, 255, 0.16)"

    TEXT: Final = "#e8eaf2"
    TEXT_MUTED: Final = "#9aa1b5"
    TEXT_FAINT: Final = "#6b7288"

    ACCENT: Final = "#7c5cff"
    ACCENT_SOFT: Final = "rgba(124, 92, 255, 0.18)"
    ACCENT_TEXT: Final = "#ffffff"

    LOCAL: Final = "#42d392"
    CLOUD: Final = "#f0b429"
    DANGER: Final = "#ff6b6b"
    WARNING: Final = "#ffb15c"
    INFO: Final = "#5aa9ff"
    OK: Final = "#42d392"
    PROGRESS: Final = "#5aa9ff"

    USER_BUBBLE: Final = "rgba(124, 92, 255, 0.22)"
    ASSISTANT_BUBBLE: Final = "rgba(255, 255, 255, 0.05)"
    SYSTEM_BUBBLE: Final = "rgba(90, 169, 255, 0.12)"


STATUS_COLOURS: Final[dict[str, str]] = {
    "ok": Palette.LOCAL,
    "error": Palette.DANGER,
    "invalid": Palette.WARNING,
    "denied": Palette.WARNING,
    "declined": Palette.TEXT_MUTED,
    "cancelled": Palette.TEXT_MUTED,
    "timeout": Palette.WARNING,
    "unavailable": Palette.TEXT_MUTED,
    "running": Palette.INFO,
}


def status_colour(status: str) -> str:
    return STATUS_COLOURS.get(status, Palette.TEXT_MUTED)


def font(size: int = 10, *, weight: QFont.Weight = QFont.Weight.Normal, mono: bool = False) -> QFont:
    family = "Consolas" if mono else "Segoe UI"
    result = QFont(family, size)
    result.setWeight(weight)
    if mono:
        result.setStyleHint(QFont.StyleHint.Monospace)
    return result


STYLESHEET = f"""
* {{
    font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
    outline: none;
}}

QWidget#Root {{
    background: {Palette.BG};
    color: {Palette.TEXT};
}}

QWidget {{
    color: {Palette.TEXT};
}}

QLabel#Title {{ font-size: 19px; font-weight: 600; }}
QLabel#Subtitle {{ color: {Palette.TEXT_MUTED}; font-size: 12px; }}
QLabel#SectionTitle {{ font-size: 14px; font-weight: 600; color: {Palette.TEXT}; }}
QLabel#Muted {{ color: {Palette.TEXT_MUTED}; }}
QLabel#Faint {{ color: {Palette.TEXT_FAINT}; font-size: 11px; }}
QLabel#PageTitle {{ font-size: 22px; font-weight: 600; }}

QFrame#Glass {{
    background: {Palette.GLASS};
    border: 1px solid {Palette.BORDER};
    border-radius: 14px;
}}
QFrame#GlassStrong {{
    background: {Palette.GLASS_STRONG};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 14px;
}}
QFrame#Divider {{
    background: {Palette.BORDER};
    max-height: 1px;
    border: none;
}}

/* ---------------------------------------------------------------- sidebar */
QListWidget#Sidebar {{
    background: transparent;
    border: none;
    padding: 6px;
}}
QListWidget#Sidebar::item {{
    color: {Palette.TEXT_MUTED};
    padding: 9px 12px;
    border-radius: 10px;
    margin: 2px 4px;
}}
QListWidget#Sidebar::item:hover {{
    background: {Palette.GLASS};
    color: {Palette.TEXT};
}}
QListWidget#Sidebar::item:selected {{
    background: {Palette.ACCENT_SOFT};
    color: {Palette.TEXT};
    font-weight: 600;
}}

/* ----------------------------------------------------------------- buttons */
QPushButton {{
    background: {Palette.GLASS};
    border: 1px solid {Palette.BORDER};
    border-radius: 9px;
    padding: 7px 14px;
    color: {Palette.TEXT};
}}
QPushButton:hover {{ background: {Palette.GLASS_STRONG}; border-color: {Palette.BORDER_STRONG}; }}
QPushButton:pressed {{ background: rgba(255,255,255,0.04); }}
QPushButton:disabled {{ color: {Palette.TEXT_FAINT}; border-color: rgba(255,255,255,0.05); }}

QPushButton#Primary {{
    background: {Palette.ACCENT};
    border: none;
    color: {Palette.ACCENT_TEXT};
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: #8d70ff; }}
QPushButton#Primary:disabled {{ background: rgba(124, 92, 255, 0.35); color: rgba(255,255,255,0.6); }}

QPushButton#Danger {{ color: {Palette.DANGER}; }}
QPushButton#Danger:hover {{ background: rgba(255, 107, 107, 0.14); }}

QPushButton#Ghost {{
    background: transparent;
    border: none;
    color: {Palette.TEXT_MUTED};
    padding: 6px 8px;
}}
QPushButton#Ghost:hover {{ color: {Palette.TEXT}; background: {Palette.GLASS}; }}

QPushButton#IconButton {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 6px;
}}
QPushButton#IconButton:hover {{ background: {Palette.GLASS_STRONG}; }}

/* ------------------------------------------------------------------ inputs */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: rgba(0, 0, 0, 0.28);
    border: 1px solid {Palette.BORDER};
    border-radius: 9px;
    padding: 7px 10px;
    selection-background-color: {Palette.ACCENT};
    selection-color: white;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {Palette.ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {Palette.BG_ELEVATED};
    border: 1px solid {Palette.BORDER};
    selection-background-color: {Palette.ACCENT_SOFT};
    color: {Palette.TEXT};
    padding: 4px;
}}
QCheckBox {{ spacing: 8px; color: {Palette.TEXT}; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border-radius: 5px;
    border: 1px solid {Palette.BORDER_STRONG};
    background: rgba(0,0,0,0.3);
}}
QCheckBox::indicator:checked {{
    background: {Palette.ACCENT};
    border-color: {Palette.ACCENT};
}}

/* ------------------------------------------------------------------ scroll */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: rgba(255,255,255,0.14);
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,0.22); }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: rgba(255,255,255,0.14); border-radius: 5px; min-width: 30px; }}

/* -------------------------------------------------------------------- tabs */
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: transparent;
    color: {Palette.TEXT_MUTED};
    padding: 8px 14px;
    margin-right: 4px;
    border-radius: 8px;
}}
QTabBar::tab:selected {{ background: {Palette.GLASS_STRONG}; color: {Palette.TEXT}; }}
QTabBar::tab:hover {{ color: {Palette.TEXT}; }}

/* ------------------------------------------------------------------ lists */
QListWidget, QTreeWidget, QTableWidget {{
    background: transparent;
    border: none;
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: 9px;
    margin: 2px 0;
}}
QListWidget::item:hover {{ background: {Palette.GLASS}; }}
QListWidget::item:selected {{ background: {Palette.ACCENT_SOFT}; color: {Palette.TEXT}; }}
QHeaderView::section {{
    background: transparent;
    color: {Palette.TEXT_MUTED};
    border: none;
    padding: 4px;
}}

/* ------------------------------------------------------------------ misc */
QToolTip {{
    background: {Palette.BG_ELEVATED};
    color: {Palette.TEXT};
    border: 1px solid {Palette.BORDER};
    padding: 6px;
    border-radius: 8px;
}}
QProgressBar {{
    background: rgba(0,0,0,0.3);
    border: none;
    border-radius: 6px;
    height: 8px;
    text-align: center;
    color: {Palette.TEXT_MUTED};
}}
QProgressBar::chunk {{ background: {Palette.ACCENT}; border-radius: 6px; }}
QMenu {{
    background: {Palette.BG_ELEVATED};
    border: 1px solid {Palette.BORDER};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{ padding: 7px 16px; border-radius: 7px; }}
QMenu::item:selected {{ background: {Palette.ACCENT_SOFT}; }}
QSplitter::handle {{ background: transparent; }}
QGroupBox {{
    border: 1px solid {Palette.BORDER};
    border-radius: 12px;
    margin-top: 14px;
    padding: 12px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {Palette.TEXT_MUTED}; }}
"""


def make_app_icon(size: int = 64, colour: str = Palette.ACCENT) -> QIcon:
    """A simple generated icon: a soft ring with a filled core.

    Better than shipping a binary asset in the repo, and it scales cleanly.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    glow = QColor(colour)
    glow.setAlpha(70)
    painter.setBrush(glow)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, size - 4, size - 4)

    painter.setBrush(QColor(colour))
    inner = int(size * 0.34)
    painter.drawEllipse((size - inner) // 2, (size - inner) // 2, inner, inner)

    painter.setBrush(Qt.BrushStyle.NoBrush)
    pen = painter.pen()
    pen.setColor(QColor("#ffffff"))
    pen.setWidthF(max(1.5, size * 0.035))
    painter.setPen(pen)
    ring = int(size * 0.62)
    painter.drawEllipse((size - ring) // 2, (size - ring) // 2, ring, ring)
    painter.end()
    return QIcon(pixmap)
