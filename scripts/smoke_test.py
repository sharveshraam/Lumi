"""Headless smoke test: start the whole application and check it holds together.

This is what CI would run after the unit tests, because it proves the pieces are
wired to each other - window, pages, services, database, tool registry - not
just that each one works alone.

    python scripts/smoke_test.py

It starts on the Qt "offscreen" platform, so no display is needed.  Exit code 0
means the application started, built every page, answered a briefing and shut
down cleanly.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from lumi.app import create_app
    from lumi.ui.main_window import MainWindow
    from lumi.ui.theme import STYLESHEET
    from lumi.version import APP_NAME, __version__

    from lumi.config.paths import AppPaths

    # Always a throwaway data folder: the smoke test must never touch the real
    # tasks, notes and memory, and it must start from a known-empty database.
    home = Path(tempfile.mkdtemp(prefix="lumi-smoke-"))
    core = create_app(paths=AppPaths(home).ensure(), console_log=False)
    application = QApplication([])
    application.setApplicationName(APP_NAME)
    application.setApplicationVersion(__version__)
    application.setStyleSheet(STYLESHEET)

    services = core.services()
    services.app = core  # the pages reach the application through this
    core.start_scheduler()

    window = MainWindow(services)
    window.show()

    # Touch everything a user would.
    services.tasks.create(title="Smoke test task")
    services.notes.create(title="Smoke note", body="hello from the smoke test")
    window.chat_page.refresh_models()
    window.tasks_page.refresh()
    window.notes_page.refresh()
    window.memory_page.refresh()
    window.settings_page.refresh_activity()
    window.show_briefing()

    failures: list[str] = []
    if window.stack.count() != 5:
        failures.append(f"expected 5 pages, found {window.stack.count()}")
    if len(services.registry.names()) < 60:
        failures.append(f"only {len(services.registry.names())} tools registered")
    if not window.chat_page.status_pill.text():
        failures.append("the status pill is empty")
    if window.tasks_page.task_list.count() != 1:
        failures.append(
            f"the task list shows {window.tasks_page.task_list.count()} rows, expected the 1 just created"
        )
    if window.notes_page.list_widget.count() != 1:
        failures.append(
            f"the note list shows {window.notes_page.list_widget.count()} rows, expected the 1 just created"
        )
    if services.tasks.list(limit=5)[0].title != "Smoke test task":
        failures.append("the task was not stored in this data folder")
    if window.settings_page.activity_list.count() == 0:
        failures.append("the activity tab is empty")

    window.toggle_compact_mode()
    if window.sidebar_card.isVisible():
        failures.append("compact mode did not hide the sidebar")
    window.toggle_compact_mode()

    def finish() -> None:
        window._quitting = True
        window.close()
        core.shutdown()
        application.quit()

    QTimer.singleShot(250, finish)
    application.exec()

    print(f"LUMI {__version__} smoke test")
    print(f"  data folder : {home}")
    print(f"  pages       : {window.stack.count()}")
    print(f"  tools       : {len(services.registry.names())}")
    if failures:
        for problem in failures:
            print(f"  FAILED: {problem}")
        return 1
    print("  everything started, rendered and shut down cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
