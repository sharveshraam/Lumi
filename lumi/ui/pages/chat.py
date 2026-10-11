"""The chat page: the transcript, the composer and the tool activity panel.

Everything slow happens in :class:`ChatWorker`; this module only draws what the
worker reports.  Confirmations are raised from the worker thread through
``run_on_ui_thread`` on the main window.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumi.ui.chat_worker import ChatWorker
from lumi.ui.theme import Palette, font
from lumi.ui.widgets.bubbles import ChatBubble, PendingQuestion, SuggestionCard, SystemNote, ToolCallCard
from lumi.ui.widgets.common import GlassCard, IconButton, Pill, Row, copy_to_clipboard

log = logging.getLogger("lumi.ui.chat")


class _SpeakTask(QRunnable):
    """A one-shot job for the global thread pool (speech output)."""

    def __init__(self, work) -> None:
        super().__init__()
        self._work = work
        self.setAutoDelete(True)

    def run(self) -> None:  # noqa: D102 - QRunnable
        try:
            self._work()
        except Exception:  # noqa: BLE001 - a background thread must never raise
            log.debug("background speech task failed", exc_info=True)


class Composer(QPlainTextEdit):
    """The message box: Enter sends, Shift+Enter makes a new line."""

    submitted = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("Ask LUMI anything, or give it a task…  (Enter to send, Shift+Enter for a new line)")
        self.setFixedHeight(74)
        self.setTabChangesFocus(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit(self.toPlainText())
            return
        super().keyPressEvent(event)

    def submit(self) -> str:
        return self.toPlainText()

    def take_text(self) -> str:
        text = self.toPlainText()
        self.setPlainText("")
        return text


class ChatPage(QWidget):
    """Everything the user sees while talking to LUMI."""

    stop_requested = Signal()
    memory_accepted = Signal(str)

    def __init__(self, services: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.services = services
        self.conversation_id: Optional[int] = None
        self.worker: Optional[ChatWorker] = None
        self._streaming_bubble: Optional[ChatBubble] = None
        self._pending_card: Optional[PendingQuestion] = None
        self._tool_cards: dict[str, ToolCallCard] = {}
        self._last_arguments: dict[str, dict] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(self._build_header())

        middle = QHBoxLayout()
        middle.setSpacing(10)
        middle.addWidget(self._build_transcript(), 1)
        middle.addWidget(self._build_tool_panel())
        layout.addLayout(middle, 1)

        layout.addWidget(self._build_composer())

        QTimer.singleShot(0, self.show_greeting)

    # ------------------------------------------------------------------ header
    def _build_header(self) -> QWidget:
        card = GlassCard(padding=10)
        row = Row(spacing=8)

        self.title_label = QLabel("New conversation")
        self.title_label.setObjectName("H3")
        row.add(self.title_label)
        row.add_stretch()

        self.status_pill = Pill("checking…", Palette.TEXT_MUTED, "Ollama status")
        row.add(self.status_pill)

        self.model_box = QComboBox()
        self.model_box.setMinimumWidth(190)
        self.model_box.setToolTip("Which local model answers your questions")
        self.model_box.currentIndexChanged.connect(self._on_model_changed)
        row.add(self.model_box)

        self.refresh_button = IconButton("↻", "Check Ollama and refresh the model list", small=True)
        self.refresh_button.clicked.connect(self.refresh_models)
        row.add(self.refresh_button)

        self.new_button = IconButton("＋", "Start a new conversation (Ctrl+N)", small=True)
        self.new_button.clicked.connect(self.start_new_conversation)
        row.add(self.new_button)

        card.body().addWidget(row)
        return card

    # -------------------------------------------------------------- transcript
    def _build_transcript(self) -> QWidget:
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("Transcript")

        inner = QWidget()
        inner.setObjectName("TranscriptInner")
        self.transcript = QVBoxLayout(inner)
        self.transcript.setContentsMargins(4, 4, 10, 4)
        self.transcript.setSpacing(10)
        self.transcript.addStretch(1)

        self.scroll.setWidget(inner)
        return self.scroll

    def _build_tool_panel(self) -> QWidget:
        card = GlassCard(padding=10)
        card.setFixedWidth(250)
        row = Row(spacing=8)
        label = QLabel("Activity")
        label.setObjectName("H4")
        row.add(label)
        row.add_stretch()
        self.activity_pill = Pill("idle", Palette.TEXT_MUTED, "What LUMI is doing right now")
        row.add(self.activity_pill)
        card.body().addWidget(row)

        self.activity_text = QLabel("Nothing yet. Tool calls appear here as they happen.")
        self.activity_text.setObjectName("Faint")
        self.activity_text.setWordWrap(True)
        card.body().addWidget(self.activity_text)
        return card

    # ---------------------------------------------------------------- composer
    def _build_composer(self) -> QWidget:
        card = GlassCard(padding=10)
        column = QVBoxLayout()
        column.setSpacing(8)

        self.composer = Composer()
        self.composer.submitted.connect(self.send_message)
        self.composer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        column.addWidget(self.composer)

        row = Row(spacing=8)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("Primary")
        self.send_button.clicked.connect(lambda: self.send_message(self.composer.take_text()))
        self.send_button.setEnabled(True)
        row.add(self.send_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.on_stop_clicked)
        row.add(self.stop_button)

        self.tools_toggle = QToolButton()
        self.tools_toggle.setText("Tools: on")
        self.tools_toggle.setCheckable(True)
        self.tools_toggle.setChecked(True)
        self.tools_toggle.setToolTip("Let LUMI use its registered tools (never arbitrary code)")
        self.tools_toggle.clicked.connect(self._on_tools_toggled)
        row.add(self.tools_toggle)

        row.add_stretch()
        self.context_label = QLabel("Ready")
        self.context_label.setObjectName("Faint")
        row.add(self.context_label)

        column.addWidget(row)
        card.body().addLayout(column)
        return card

    # ------------------------------------------------------------------ widget
    def _add_widget(self, widget: QWidget) -> None:
        self.transcript.insertWidget(self.transcript.count() - 1, widget)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def add_bubble(self, role: str, text: str = "", *, streaming: bool = False) -> ChatBubble:
        bubble = ChatBubble(role, text)
        bubble.copy_requested.connect(copy_to_clipboard)
        bubble.link_requested.connect(self.open_link)
        if streaming:
            bubble.set_state("streaming", Palette.INFO)
        # Read the setting each time so the toggle applies to new messages.
        try:
            bubble.set_timestamp_visible(self.services.settings_store.settings.appearance.show_timestamps)
        except Exception:  # noqa: BLE001 - a bubble is not worth a crash
            pass
        self._add_widget(bubble)
        return bubble

    def add_note(self, text: str, colour: str = Palette.TEXT_MUTED) -> SystemNote:
        note = SystemNote(text, colour=colour)
        self._add_widget(note)
        return note

    # ------------------------------------------------------------------ models
    def refresh_models(self) -> None:
        try:
            models = self.services.engine.list_models()
        except Exception as exc:  # noqa: BLE001 - Ollama may simply be off
            log.info("could not list models: %s", exc)
            models = []
        current = self.services.engine.selected_model()

        self.model_box.blockSignals(True)
        self.model_box.clear()
        names = [item.name for item in models]
        if not names:
            self.model_box.addItem("no model installed", "")
        for name in names:
            self.model_box.addItem(name, name)
        index = self.model_box.findData(current)
        if index >= 0:
            self.model_box.setCurrentIndex(index)
        self.model_box.blockSignals(False)

        try:
            status = self.services.engine.status()
            running = getattr(status, "running", False)
            message = getattr(status, "message", "") or ""
        except Exception as exc:  # noqa: BLE001
            running, message = False, str(exc)

        if running and names:
            self.status_pill.set_state(f"{len(names)} model(s) ready", Palette.OK, message)
        elif running:
            self.status_pill.set_state("no model installed", Palette.WARNING, message)
        else:
            self.status_pill.set_state("Ollama offline", Palette.TEXT_MUTED, message)

        if not names:
            self.context_label.setText("No local model yet — LUMI still handles tasks, notes and reminders.")

    def _on_model_changed(self, _index: int) -> None:
        name = self.model_box.currentData()
        if not name:
            return
        try:
            self.services.settings_store.update(model={"selected_model": name}, save=False)
            self.services.engine.invalidate_model_cache()
            self.services.settings_store.save()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not change the model: %s", exc)

    # --------------------------------------------------------------- greeting
    def show_greeting(self) -> None:
        self.add_note(
            "Local-first. Your prompts stay on this computer, and LUMI asks before it "
            "changes anything.",
            Palette.INFO,
        )

    # -------------------------------------------------------------- messaging
    def start_new_conversation(self) -> None:
        self.conversation_id = None
        self.title_label.setText("New conversation")
        self._streaming_bubble = None
        self._tool_cards.clear()
        self._last_arguments.clear()
        while self.transcript.count() > 1:
            item = self.transcript.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.show_greeting()
        self.composer.setFocus()

    def send_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self.worker is not None:
            return
        if self.conversation_id is None:
            try:
                conversation = self.services.conversations.start(title=text[:60])
                self.conversation_id = conversation.id
                self.title_label.setText(conversation.title)
            except Exception as exc:  # noqa: BLE001 - history is optional
                log.info("could not start a conversation: %s", exc)

        self.add_bubble("user", text)
        self.composer.setPlainText("")
        self._start_worker(text)

    def _start_worker(self, text: str) -> None:
        context = self.services.app.tool_context()
        # The window owns the confirmation dialog, so route tool approvals there
        # instead of letting the executor run without a human.
        provider = getattr(self.services, "confirm_provider", None)
        if provider is not None:
            context.confirm = provider
            context.services["confirm_provider"] = provider

        self.worker = ChatWorker(
            self.services.engine,
            text,
            conversation_id=self.conversation_id,
            tool_context=context,
            allow_tools=self.tools_toggle.isChecked(),
            parent=self,
        )
        self.worker.message_started.connect(self._on_message_started)
        self.worker.token_received.connect(self._on_token)
        self.worker.message_finished.connect(lambda _text: self._on_message_finished())
        self.worker.reply_ready.connect(self._on_reply)
        self.worker.tool_proposed.connect(self._on_tool_proposed)
        self.worker.tool_started.connect(self._on_tool_started)
        self.worker.tool_finished.connect(self._on_tool_finished)
        self.worker.memory_suggested.connect(self._on_memory_suggestion)
        self.worker.status_changed.connect(self.context_label.setText)
        self.worker.failed.connect(self._on_error)
        self.worker.finished.connect(self._on_worker_finished)

        self.send_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.context_label.setText("Thinking…")
        self._streaming_bubble = None
        self.worker.start()

    def is_busy(self) -> bool:
        """True while a turn is being produced."""
        return self.worker is not None

    def stop(self) -> None:
        """Stop the current turn (called by the window's Stop button)."""
        self.on_stop_clicked()

    def on_stop_clicked(self) -> None:
        self.stop_requested.emit()
        if self.worker is not None:
            self.worker.cancel()
        self.context_label.setText("Stopped.")
        self.stop_button.setEnabled(False)

    def _on_tools_toggled(self, checked: bool) -> None:
        self.tools_toggle.setText("Tools: on" if checked else "Tools: off")

    # ------------------------------------------------------------ worker slots
    def _on_message_started(self, role: str) -> None:
        self._streaming_bubble = self.add_bubble("assistant", "", streaming=True)

    def _on_token(self, chunk: str) -> None:
        if self._streaming_bubble is None:
            self._streaming_bubble = self.add_bubble("assistant", "", streaming=True)
        self._streaming_bubble.append(chunk)

    def _on_message_finished(self) -> None:
        if self._streaming_bubble is not None:
            self._streaming_bubble.finish()
            self._streaming_bubble = None
        self._scroll_to_bottom()

    def _on_reply(self, reply: Any) -> None:
        text = getattr(reply, "text", "") or ""
        if text and self._streaming_bubble is None:
            self._streaming_bubble = self.add_bubble("assistant", "")
        if self._streaming_bubble is not None:
            if not self._streaming_bubble.text():
                self._streaming_bubble.set_text(text)
            self._streaming_bubble.show_meta(self._meta_line(reply))
            self._streaming_bubble.finish()
            self._streaming_bubble = None

        if getattr(reply, "error", ""):
            self._on_error(reply.error, getattr(reply, "hint", ""))
        if getattr(reply, "pending_question", ""):
            self._show_pending(reply.pending_question)
        if getattr(reply, "stopped", False):
            self.add_note("Stopped at your request.", Palette.WARNING)
        if text and not getattr(reply, "error", ""):
            self._maybe_speak(text)

    @staticmethod
    def _meta_line(reply: Any) -> str:
        parts = []
        if getattr(reply, "model", ""):
            parts.append(str(reply.model))
        if getattr(reply, "provider", ""):
            parts.append("local" if reply.provider == "local" else str(reply.provider))
        duration = getattr(reply, "duration_ms", 0) or 0
        if duration:
            parts.append(f"{duration / 1000:.1f}s")
        speed = getattr(reply, "tokens_per_second", 0) or 0
        if speed:
            parts.append(f"{speed:.0f} tok/s")
        if getattr(reply, "deterministic", False):
            parts.append("instant")
        return " · ".join(parts)

    def _maybe_speak(self, text: str) -> None:
        """Read the reply out loud if the user asked for that."""
        try:
            speech = self.services.settings_store.settings.speech
        except Exception:  # noqa: BLE001
            return
        if not (speech.enabled and speech.tts_enabled and speech.speak_replies):
            return

        def work() -> None:
            try:
                from lumi.voice import speak

                speak(text, settings=self.services.settings_store.settings)
            except Exception as exc:  # noqa: BLE001 - voice is optional
                log.info("could not speak the reply: %s", exc)

        QThreadPool.globalInstance().start(_SpeakTask(work))

    # ------------------------------------------------------------- tool panel
    def _card_for(self, name: str, arguments: dict) -> ToolCallCard:
        """One card per tool call, created on demand and kept in ``_tool_cards``."""
        card = self._tool_cards.get(name)
        if card is None:
            card = ToolCallCard(name, arguments)
            self._tool_cards[name] = card
            self._add_widget(card)
        return card

    def _on_tool_proposed(self, proposal: Any) -> None:
        name = getattr(proposal, "name", "") or getattr(proposal, "tool", "")
        arguments = getattr(proposal, "arguments", {}) or {}
        self._last_arguments[name] = arguments
        self._set_activity(f"proposed: {name}", Palette.WARNING)
        self._card_for(name, arguments).state.set_state("proposed", Palette.WARNING)

    def _on_tool_started(self, name: str, arguments: dict) -> None:
        self._last_arguments[name] = arguments or self._last_arguments.get(name, {})
        self._set_activity(f"running: {name}", Palette.PROGRESS)
        self._card_for(name, self._last_arguments.get(name, {})).state.set_state(
            "running", Palette.PROGRESS
        )

    def _on_tool_finished(self, outcome: Any) -> None:
        name = getattr(outcome, "tool", "tool")
        status = getattr(outcome, "status", "ok") or "ok"
        colour, label = activity_colour(status)
        self._set_activity(f"{name}: {label}", colour)
        card = self._card_for(name, self._last_arguments.get(name, {}))
        summary = getattr(outcome, "message", "") or ""
        result = getattr(outcome, "result", None)
        if not summary and result is not None:
            summary = getattr(result, "summary", "") or ""
        card.finish(status, summary, colour)

    def _set_activity(self, text: str, colour: str) -> None:
        label = text.split(":")[0]
        self.activity_pill.setText(label)
        self.activity_pill.set_colour(colour)
        self.activity_text.setText(text)

    def _on_memory_suggestion(self, text: str) -> None:
        card = SuggestionCard(text)
        card.accepted.connect(self.memory_accepted.emit)
        self._add_widget(card)

    def _show_pending(self, question: str) -> None:
        card = PendingQuestion(question)
        card.answered.connect(self._on_pending_answered)
        self._pending_card = card
        self._add_widget(card)

    def _on_pending_answered(self, yes: bool) -> None:
        if self._pending_card is not None:
            self._pending_card.setVisible(False)
            card = self._pending_card
            self._pending_card = None
            card.deleteLater()
        self.send_message("yes" if yes else "no")

    def _on_error(self, message: str, hint: str) -> None:
        text = message if not hint else f"{message}\n{hint}"
        self.add_note(text, Palette.DANGER)

    def _on_worker_finished(self) -> None:
        self.worker = None
        self.send_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.context_label.setText("Ready")
        self.composer.setFocus()

    def open_link(self, url: str) -> None:
        """Open a link the transcript produced, asking the policy first."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(url))


def activity_colour(status: str) -> tuple[str, str]:
    """Colour and short label for one tool outcome."""
    mapping = {
        "ok": (Palette.OK, "done"),
        "proposed": (Palette.TEXT_MUTED, "suggested"),
        "denied": (Palette.DANGER, "not allowed"),
        "declined": (Palette.WARNING, "you said no"),
        "cancelled": (Palette.WARNING, "stopped"),
        "timeout": (Palette.WARNING, "timed out"),
        "invalid": (Palette.DANGER, "bad request"),
        "error": (Palette.DANGER, "failed"),
    }
    return mapping.get(status, (Palette.TEXT_MUTED, status))
