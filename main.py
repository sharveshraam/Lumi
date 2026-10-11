"""LUMI entry point.

Run from source:

    python main.py

The window can also be started with ``python -m lumi`` (see ``lumi/__main__.py``).
Everything heavy is imported lazily so that ``--version`` and ``--check`` work
even on a machine where PySide6 is not installed yet.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:  # allow running the file directly from a checkout
    sys.path.insert(0, str(REPO_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lumi",
        description="LUMI - a local-first AI desktop assistant.",
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--check", action="store_true", help="check the installation and exit")
    parser.add_argument("--home", type=str, default=None, help="use a different data folder")
    parser.add_argument("--console-log", action="store_true", help="also write the log to this console")
    parser.add_argument("--log-level", type=str, default=None, help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument("--no-window", action="store_true", help="start the core services without the window (for testing)")
    parser.add_argument("--briefing", action="store_true", help="print today's briefing and exit")
    return parser


def _check(paths) -> int:
    """Report what is and is not available, without starting the UI."""
    from lumi.app import create_app
    from lumi.ai.hardware import detect as detect_hardware

    app = create_app(paths=paths)
    summary = app.system_summary()
    hardware = detect_hardware()
    ollama = summary["ollama"]

    print(f"LUMI {summary['version']}")
    print(f"  data folder : {summary['home']}")
    print(f"  python      : {summary['python']} on {summary['platform']}")
    print(f"  hardware    : {hardware.summary_line()}")
    print(f"  ollama      : {'running' if ollama.get('running') else 'not reachable'} - {ollama.get('message', '')}")
    print(f"  model       : {summary['selected_model'] or 'none selected'}")
    print(f"  tools       : {summary['tools'].get('total', 0)} registered")
    automation = summary["automation"]
    print(f"  automation  : backend={automation.get('backend')} ready={automation.get('desktop_ready')}")
    if not ollama.get("running"):
        print("")
        print("  LUMI still works without Ollama: tasks, notes, reminders, memory and file search need no model.")
        print("  To enable chat: install Ollama (https://ollama.com/download), start it, then run")
        print("    ollama pull llama3.2:3b")
    app.shutdown()
    return 0 if ollama.get("running") else 1


def _briefing(paths) -> int:
    from lumi.app import create_app

    app = create_app(paths=paths)
    context = app.tool_context()
    from lumi.tools.builtin.productivity import _briefing_text  # type: ignore[attr-defined]

    print(_briefing_text(context))
    app.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        from lumi.version import APP_NAME, __version__

        print(f"{APP_NAME} {__version__}")
        return 0

    paths = None
    if args.home:
        from lumi.config.paths import AppPaths

        paths = AppPaths(args.home)

    if args.check:
        return _check(paths)
    if args.briefing:
        return _briefing(paths)
    if args.no_window:
        from lumi.app import create_app

        app = create_app(paths=paths, console_log=True, log_level=args.log_level)
        print("Core services started. Press Ctrl+C to stop.")
        try:
            import time

            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        try:
            print("\nStopping…")
            app.shutdown()
        except KeyboardInterrupt:  # a second Ctrl+C while closing is fine
            print("Stopped immediately.")
        return 0

    # The windowed application.
    from lumi.ui.main_window import run

    return run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Ctrl+C is a normal way to stop a console run, not a crash.
        print("Interrupted.")
        raise SystemExit(130) from None
