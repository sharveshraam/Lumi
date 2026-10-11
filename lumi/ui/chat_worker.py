"""Background workers: keep every slow call off the UI thread.

The assistant loop is synchronous and can take a while (a local model streams for
seconds, a tool can hash a big folder).  All of it runs inside QThreads and talks
back to the interface through signals.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QThread, Signal

from lumi.tools.base import StopToken
from lumi.ui.confirm import ConfirmationRequest

log = logging.getLogger("lumi.ui.worker")


class TaskWorker(QThread):
    """Runs one callable off the UI thread and reports the result."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, work: Callable[[], Any], parent: Optional[Any] = None, *, name: str = "task") -> None:
        super().__init__(parent)
        self._work = work
        self._name = name
        self.error: str = ""

    def run(self) -> None:  # pragma: no cover - exercised through the UI
        try:
            result = self._work()
        except Exception as exc:  # noqa: BLE001 - the UI must never crash on a worker
            log.exception("worker %s failed", self._name)
            self.error = str(exc)
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)


class ChatWorker(QThread):
    """One assistant turn, streamed back to the interface."""

    status_changed = Signal(str)
    message_started = Signal(str)
    token_received = Signal(str)
    message_finished = Signal(str)
    reply_ready = Signal(object)  # AssistantReply
    tool_proposed = Signal(object)
    tool_started = Signal(str, dict)
    tool_finished = Signal(object)
    memory_suggested = Signal(str)
    failed = Signal(str, str)
    confirm_requested = Signal(object)

    def __init__(
        self,
        engine: Any,
        message: str,
        *,
        conversation_id: Optional[int],
        tool_context: Any,
        allow_tools: bool = True,
        parent: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self.engine = engine
        self.message = message
        self.conversation_id = conversation_id
        self.tool_context = tool_context
        self.allow_tools = allow_tools
        self.stop_token: StopToken = tool_context.stop_token
        self._confirmation: Optional[ConfirmationRequest] = None
        self._answer: Any = None
        self._answer_ready = threading.Event()

    # ------------------------------------------------------------- confirm API
    def request_confirmation(self, request: ConfirmationRequest) -> Any:
        """Called from the worker thread; blocks until the UI answers."""
        self._confirmation = request
        self._answer = None
        self._answer_ready.clear()
        self.status_changed.emit(f"waiting for your decision: {request.title}")
        self.confirm_requested.emit(request)
        # The UI may take as long as the user needs.  The stop button still wins:
        # it sets the stop token, and the executor treats that as a refusal.
        while not self._answer_ready.wait(0.25):
            if self.stop_token.cancelled:
                return None
        return self._answer

    def answer_confirmation(self, answer: Any) -> None:
        self._answer = answer
        self._answer_ready.set()

    def cancel(self) -> None:
        self.stop_token.cancel()
        self._answer_ready.set()

    # -------------------------------------------------------------- Qt thread
    def run(self) -> None:  # pragma: no cover - exercised through the UI
        from lumi.ai.assistant import ChatCallbacks

        callbacks = ChatCallbacks(
            on_status=self.status_changed.emit,
            on_token=self.token_received.emit,
            on_message_start=self.message_started.emit,
            on_message_end=self.message_finished.emit,
            on_tool_proposed=self.tool_proposed.emit,
            on_tool_started=lambda name, arguments: self.tool_started.emit(name, arguments),
            on_tool_finished=self.tool_finished.emit,
            on_memory_suggestion=self.memory_suggested.emit,
            on_error=lambda message, hint: self.failed.emit(message, hint),
        )
        try:
            reply = self.engine.send(
                self.message,
                conversation_id=self.conversation_id,
                callbacks=callbacks,
                stop_token=self.stop_token,
                tool_context=self.tool_context,
                allow_tools=self.allow_tools,
            )
        except Exception as exc:  # noqa: BLE001 - never let a thread die silently
            log.exception("chat worker crashed")
            self.failed.emit(str(exc), "This is a LUMI bug - the details are in the log file.")
            return
        self.reply_ready.emit(reply)
