"""The confirmation dialog.

Every consequential action passes through here: sending mail, deleting files,
overwriting something, installing a model, submitting a form.  The dialog shows
exactly what is about to happen and defaults to *no*.

The executor calls :class:`DialogConfirmationProvider` from a worker thread, so
the dialog is always raised on the Qt thread and the worker blocks until there is
an answer.  If there is no window, or the window cannot be reached, the answer is
**no** - never a silent yes.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from lumi.tools.base import ConfirmationRequest, RiskLevel
from lumi.ui.theme import Palette

log = logging.getLogger("lumi.ui.confirm")

ANSWER_YES = "yes"
ANSWER_NO = "no"
ANSWER_SESSION = "session"


class ConfirmationDialog(QDialog):
    """One question, with the details laid out plainly."""

    def __init__(self, request: ConfirmationRequest, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.request = request
        self.answer = ANSWER_NO
        self.remember = False

        self.setWindowTitle(request.title or "LUMI needs your permission")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(12)

        heading = QLabel(request.title or "Allow this action?")
        heading.setObjectName("H2")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        risk = QLabel(self._risk_line(request.risk))
        risk.setObjectName("Faint")
        risk.setWordWrap(True)
        layout.addWidget(risk)

        details = QPlainTextEdit()
        details.setPlainText(request.details or self._describe(request))
        details.setReadOnly(True)
        details.setMaximumHeight(180)
        details.setObjectName("Mono")
        layout.addWidget(details)

        if request.preview:
            preview_label = QLabel("Preview")
            preview_label.setObjectName("Faint")
            layout.addWidget(preview_label)
            preview = QPlainTextEdit(request.preview)
            preview.setReadOnly(True)
            preview.setMaximumHeight(140)
            preview.setObjectName("Mono")
            layout.addWidget(preview)

        self.remember = QCheckBox("Allow this for the rest of this session")
        self.remember.setChecked(False)
        self.remember.setVisible(bool(request.allow_session_grant))
        self.remember.setEnabled(bool(request.allow_session_grant))
        layout.addWidget(self.remember)

        buttons = QDialogButtonBox()
        self.deny_button = buttons.addButton("Don't allow", QDialogButtonBox.ButtonRole.RejectRole)
        self.allow_button = buttons.addButton("Allow once", QDialogButtonBox.ButtonRole.AcceptRole)
        self.allow_button.setObjectName("Danger")
        buttons.rejected.connect(self._deny)
        buttons.accepted.connect(self._approve)
        layout.addWidget(buttons)

        # The safe answer is the default: Enter must not approve something.
        self.deny_button.setDefault(True)
        self.deny_button.setAutoDefault(True)
        self.allow_button.setAutoDefault(False)
        self.allow_button.setDefault(False)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _risk_line(risk: RiskLevel) -> str:
        if risk is RiskLevel.SAFE:
            return "This action does not change anything."
        if risk is RiskLevel.MODERATE:
            return "This changes something, but it can be undone."
        return "This is hard to undo. Read the details before you decide."

    @staticmethod
    def _describe(request: ConfirmationRequest) -> str:
        if not request.arguments:
            return "LUMI wants to run one of its tools and needs your permission."
        lines = [f"Tool: {request.tool}", ""]
        for key, value in request.arguments.items():
            lines.append(f"{key}: {value}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ answers
    def _approve(self) -> None:
        """Allow: once, or for the rest of the session if the box is ticked."""
        remembered = bool(self.remember.isChecked() and self.request.allow_session_grant)
        self.answer = ANSWER_SESSION if remembered else ANSWER_YES
        self.accept()

    def _deny(self) -> None:
        self.answer = ANSWER_NO
        self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Closing the dialog means no."""
        if self.answer not in (ANSWER_YES, ANSWER_SESSION):
            self.answer = ANSWER_NO
        super().closeEvent(event)


class DialogConfirmationProvider:
    """Ask the user, on the Qt thread, and block the worker until they answer."""

    def __init__(self, window: Optional[Any] = None) -> None:
        self.window = window
        self.last_answer: Optional[str] = None
        #: Every question and its answer, in order: ``[(tool, answer), ...]``.
        self.decisions: list[tuple[str, str]] = []
        self.asked: list[ConfirmationRequest] = []

    def ask_on_ui_thread(self, request: ConfirmationRequest) -> str:
        """Show the dialog.  Called on the Qt thread (overridable in tests)."""
        dialog = ConfirmationDialog(request, parent=self.window)
        dialog.exec()
        return dialog.answer

    def __call__(self, request: ConfirmationRequest) -> str:
        self.asked.append(request)
        runner = getattr(self.window, "run_on_ui_thread", None)
        if self.window is None or not callable(runner):
            # Nothing to ask the user with: refuse.  Failing closed is the only
            # safe direction for a permission system.
            log.info("no window available to confirm %s - refusing", request.tool)
            return self._record(request, ANSWER_NO)

        # The answer is captured in the closure: a runner may return the value,
        # or simply run the callable on the UI thread and return nothing.
        captured: dict[str, str] = {}

        def ask() -> str:
            captured["answer"] = self.ask_on_ui_thread(request)
            return captured["answer"]

        try:
            returned = runner(ask)
        except Exception as exc:  # noqa: BLE001 - a broken UI must not become a yes
            log.warning("could not ask for confirmation: %s", exc)
            return self._record(request, ANSWER_NO)

        answer = captured.get("answer")
        if answer is None and returned in (ANSWER_YES, ANSWER_SESSION, ANSWER_NO):
            answer = returned
        if answer not in (ANSWER_YES, ANSWER_SESSION, ANSWER_NO):
            # The dialog never ran (a window that closed, or an owner that only
            # queues the work): refusing is the only safe reading.
            answer = ANSWER_NO
        return self._record(request, answer)

    def _record(self, request: ConfirmationRequest, answer: str) -> str:
        self.last_answer = answer
        self.decisions.append((request.tool, answer))
        return answer


__all__ = [
    "ANSWER_NO",
    "ANSWER_SESSION",
    "ANSWER_YES",
    "ConfirmationDialog",
    "DialogConfirmationProvider",
    "ConfirmationRequest",
]
