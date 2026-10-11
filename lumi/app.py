"""Application wiring: build every service once, in one predictable order.

The UI, the CLI tools and the tests all use this class, so there is exactly one
definition of "how LUMI is assembled".  Nothing here touches PySide6 - the UI is
layered on top - which keeps the core importable and testable headless.
"""

from __future__ import annotations

import logging
import platform
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from lumi.ai.assistant import AssistantEngine
from lumi.ai.hardware import detect as detect_hardware
from lumi.automation.browser import BrowserController
from lumi.automation.controller import AutomationController
from lumi.automation.files import FileManager
from lumi.config.logging_setup import configure_logging, get_logger, reset_for_tests
from lumi.config.paths import AppPaths
from lumi.config.secrets import SecretStore
from lumi.config.settings import SettingsStore
from lumi.memory.service import ConversationStore, MemoryService
from lumi.memory.store import Database
from lumi.notes.repository import NoteRepository
from lumi.reminders.focus import FocusRepository
from lumi.reminders.service import ReminderDelivery, ReminderRepository, ReminderScheduler, ReminderService
from lumi.security.audit import AuditLog
from lumi.security.policy import PermissionPolicy
from lumi.tasks.repository import TaskRepository
from lumi.tools.base import StopToken, ToolContext
from lumi.tools.executor import ToolExecutor
from lumi.tools.registry import ToolRegistry
from lumi.version import APP_NAME, __version__
from lumi.utils.timeutil import now, to_iso

log = logging.getLogger("lumi.app")


@dataclass
class AppServices:
    """Plain container so callers can grab what they need without a service locator."""

    paths: AppPaths
    settings_store: SettingsStore
    database: Database
    audit: AuditLog
    policy: PermissionPolicy
    registry: ToolRegistry
    executor: ToolExecutor
    memory: MemoryService
    conversations: ConversationStore
    tasks: TaskRepository
    notes: NoteRepository
    reminders: ReminderService
    reminder_repo: ReminderRepository
    focus: FocusRepository
    automation: AutomationController
    files: FileManager
    browser: BrowserController
    engine: AssistantEngine
    secrets: SecretStore
    scheduler: Optional[ReminderScheduler] = None

    def to_context(
        self,
        *,
        stop_token: StopToken | None = None,
        confirm: Callable[..., Any] | None = None,
        notify: Callable[[str, str], None] | None = None,
        session_id: str | None = None,
    ) -> ToolContext:
        services: dict[str, Any] = {
            "tasks": self.tasks,
            "notes": self.notes,
            "reminders": self.reminders,
            "reminder_repo": self.reminder_repo,
            "memory": self.memory,
            "conversations": self.conversations,
            "files": self.files,
            "automation": self.automation,
            "browser": self.browser,
            "focus": self.focus,
            "engine": self.engine,
            "executor": self.executor,
            "policy": self.policy,
            "audit": self.audit,
            "settings_store": self.settings_store,
            "scheduler": self.scheduler,
            "paths": self.paths,
        }
        return ToolContext(
            settings=self.settings_store.settings,
            paths=self.paths,
            db=self.database,
            audit=self.audit,
            stop_token=stop_token or StopToken(),
            confirm=confirm,
            notify=notify,
            services=services,
            session_id=session_id or f"session-{uuid.uuid4().hex[:8]}",
        )


class LumiApp:
    """Owns the object graph and the lifecycle of background work."""

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        console_log: bool = False,
        log_level: str | None = None,
    ) -> None:
        self.paths = (paths or AppPaths()).ensure()
        self.session_id = f"session-{uuid.uuid4().hex[:8]}"
        self._shutdown_done = False
        self._stop_event = threading.Event()

        self.settings_store = SettingsStore(self.paths)
        settings = self.settings_store.load()
        configure_logging(
            self.paths,
            level=log_level or settings.privacy.log_level or "INFO",
            console=console_log,
        )
        log.info("%s %s starting (home=%s)", APP_NAME, __version__, self.paths.home)

        self.database = Database(self.paths.database_file)
        self.database.connect()
        self._record_start()

        self.audit = AuditLog(self.database, session_id=self.session_id)
        self.policy = PermissionPolicy(settings)
        self.registry = ToolRegistry.build_default()
        self.executor = ToolExecutor(self.registry, self.policy, self.audit)

        self.memory = MemoryService(self.database)
        self.conversations = ConversationStore(self.database)
        self.tasks = TaskRepository(self.database)
        self.notes = NoteRepository(self.database)
        self.reminder_repo = ReminderRepository(self.database)
        self.reminders = ReminderService(self.reminder_repo)
        self.focus = FocusRepository(self.database)

        self.automation = AutomationController(
            settings,
            paths=self.paths,
            audit=self.audit,
            external_stop_event=self._stop_event,
        )
        self.files = FileManager(self.policy)
        self.browser = BrowserController(self.automation, settings)
        self.secrets = SecretStore(self.paths)

        self.engine = AssistantEngine(
            settings_store=self.settings_store,
            paths=self.paths,
            db=self.database,
            registry=self.registry,
            executor=self.executor,
            memory_service=self.memory,
            conversation_store=self.conversations,
            audit=self.audit,
        )

        self.scheduler: Optional[ReminderScheduler] = None
        self._delivery_listeners: list[Callable[[ReminderDelivery], None]] = []
        self._rebuild_stop_event_bindings()

        self.settings_store.add_listener(self._on_settings_changed)
        self._run_startup_housekeeping()

    # -------------------------------------------------------------- services
    def services(self) -> AppServices:
        return AppServices(
            paths=self.paths,
            settings_store=self.settings_store,
            database=self.database,
            audit=self.audit,
            policy=self.policy,
            registry=self.registry,
            executor=self.executor,
            memory=self.memory,
            conversations=self.conversations,
            tasks=self.tasks,
            notes=self.notes,
            reminders=self.reminders,
            reminder_repo=self.reminder_repo,
            focus=self.focus,
            automation=self.automation,
            files=self.files,
            browser=self.browser,
            engine=self.engine,
            secrets=self.secrets,
            scheduler=self.scheduler,
        )

    @property
    def settings(self):
        return self.settings_store.settings

    def tool_context(self, **kwargs: Any) -> ToolContext:
        return self.services().to_context(session_id=self.session_id, **kwargs)

    # ------------------------------------------------------------- lifecycle
    def _record_start(self) -> None:
        """Note this run so an unclean exit can be reported next time."""
        try:
            previous = self.database.query_one(
                "SELECT id, started_at, version FROM app_runs WHERE clean_exit = 0 ORDER BY id DESC LIMIT 1"
            )
            if previous is not None:
                log.warning("previous run (%s, v%s) did not shut down cleanly", previous["started_at"], previous["version"])
            self._previous_run = dict(previous) if previous else None
            run_id = self.database.execute_write(
                "INSERT INTO app_runs(started_at, version, clean_exit, platform, note) VALUES(?, ?, 0, ?, ?)",
                (to_iso(now()), __version__, platform.platform()[:200], f"pid={__import__('os').getpid()}"),
            )
            self._run_id = int(run_id)
        except Exception:  # pragma: no cover - bookkeeping only
            log.debug("could not record the application run", exc_info=True)
            self._run_id = None
            self._previous_run = None

    @property
    def previous_run_was_unclean(self) -> bool:
        return bool(getattr(self, "_previous_run", None))

    def _run_startup_housekeeping(self) -> None:
        settings = self.settings
        if settings.privacy.conversation_retention_days:
            try:
                removed = self.conversations.prune(retention_days=settings.privacy.conversation_retention_days)
                if removed:
                    log.info("retention policy removed %s old conversation(s)", removed)
            except Exception:  # pragma: no cover
                log.debug("conversation pruning failed", exc_info=True)
        try:
            self.memory.prune(stale_days=730)
        except Exception:  # pragma: no cover
            log.debug("memory pruning failed", exc_info=True)
        try:
            self.database.set_meta("last_version", __version__)
        except Exception:  # pragma: no cover
            pass

    def _rebuild_stop_event_bindings(self) -> None:
        self.automation.set_external_stop_event(self._stop_event)

    # ------------------------------------------------------------ scheduler
    def add_delivery_listener(self, callback: Callable[[ReminderDelivery], None]) -> None:
        self._delivery_listeners.append(callback)

    def _deliver_reminder(self, delivery: ReminderDelivery) -> None:
        for callback in list(self._delivery_listeners):
            try:
                callback(delivery)
            except Exception:  # pragma: no cover - UI listeners must not break delivery
                log.exception("reminder listener failed")

    def start_scheduler(self) -> ReminderScheduler:
        """Start the reminder ticker (idempotent)."""
        if self.scheduler is not None and self.scheduler.running:
            return self.scheduler
        self.scheduler = ReminderScheduler(
            self.reminder_repo,
            deliver=self._deliver_reminder,
            catch_up=self.settings.notifications.catch_up_missed_reminders,
        )
        self.scheduler.start()
        return self.scheduler

    def stop_scheduler(self) -> None:
        if self.scheduler is not None:
            self.scheduler.stop()

    # --------------------------------------------------------------- control
    def request_stop(self) -> None:
        """The Stop button and the global emergency-stop hotkey both land here."""
        self._stop_event.set()
        self.automation.request_stop()
        log.info("stop requested")

    def clear_stop(self) -> None:
        self._stop_event.clear()
        self.automation.clear_stop()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def system_summary(self) -> dict[str, Any]:
        hardware = detect_hardware()
        status = self.engine.status()
        return {
            "version": __version__,
            "home": str(self.paths.home),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "hardware": hardware.to_dict(),
            "hardware_summary": hardware.summary_line(),
            "ollama": status.to_dict(),
            "selected_model": self.settings.model.selected_model,
            "tools": self.registry.stats(),
            "automation": self.automation.status(),
            "database_bytes": self.database.size_bytes(),
            "previous_run_unclean": self.previous_run_was_unclean,
        }

    def select_model(self, name: str) -> None:
        self.settings_store.update(model={"selected_model": name})
        self.settings_store.save()
        self.engine.invalidate_model_cache()

    def _on_settings_changed(self, settings: Any, section: str) -> None:
        log.debug("settings changed: %s", section)
        if "ollama" in section or section == "all":
            self.engine.invalidate_model_cache()
        if "automation" in section or section == "all" or section == "speech":
            self.policy.settings = settings
            self.automation.settings = settings
            self.browser.settings = settings
        if "privacy" in section or section == "all":
            level = getattr(logging, str(settings.privacy.log_level).upper(), logging.INFO)
            get_logger("lumi").setLevel(level)

    def save_settings(self) -> None:
        try:
            self.settings_store.save()
        except OSError as exc:  # pragma: no cover - disk problems
            log.warning("could not save settings: %s", exc)

    # -------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        """Flush everything.  Safe to call more than once."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        log.info("shutting down")
        self.stop_scheduler()
        self.save_settings()
        try:
            if getattr(self, "_run_id", None):
                self.database.execute_write(
                    "UPDATE app_runs SET ended_at = ?, clean_exit = 1 WHERE id = ?",
                    (to_iso(now()), self._run_id),
                )
        except Exception:  # pragma: no cover
            log.debug("could not mark a clean exit", exc_info=True)
        try:
            self.database.checkpoint()
        except Exception:  # pragma: no cover
            pass
        self.database.close()
        log.info("shutdown complete")

    # --------------------------------------------------------------- exports
    def export_memory(self, destination: Path | None = None) -> Path:
        import json

        target = destination or self.paths.memory_export_file
        payload = {
            "type": "lumi.full-export",
            "version": __version__,
            "exported_at": to_iso(now()),
            "memory": self.memory.export_payload(),
            "notes": [note.to_dict(body_limit=20000) for note in self.notes.list(limit=5000)],
            "tasks": [task.to_dict() for task in self.tasks.list(status="all", limit=5000)],
            "reminders": [reminder.to_dict() for reminder in self.reminder_repo.list(limit=5000)],
        }
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return target


def create_app(**kwargs: Any) -> LumiApp:
    """Convenience factory used by ``main`` and the tests."""
    return LumiApp(**kwargs)
