"""LUMI - a private, local-first AI desktop assistant for Windows.

The package is intentionally import-light: importing :mod:`lumi` must never
require PySide6, pyautogui, Ollama or any other optional dependency.  Heavy
components are imported lazily by the modules that need them so that the core
(testable) logic keeps working on a machine where a component is missing.
"""

from lumi.version import __version__, APP_NAME, APP_SLUG

__all__ = ["__version__", "APP_NAME", "APP_SLUG"]
