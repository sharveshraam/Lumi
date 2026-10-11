"""Filesystem locations used by LUMI.

Everything LUMI writes lives in a single application home directory so that the
user can inspect, back up or delete it in one place.  The location follows the
platform convention:

=============================  ====================================================
Windows                        ``%APPDATA%\\LUMI``
macOS                          ``~/Library/Application Support/LUMI``
Linux/other                    ``$XDG_DATA_HOME/lumi`` or ``~/.local/share/lumi``
=============================  ====================================================

Setting the ``LUMI_HOME`` environment variable overrides the default, which is
what the automated tests use to isolate themselves from the real profile.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from lumi.version import APP_SLUG


def _default_home() -> Path:
    env = os.environ.get("LUMI_HOME")
    if env:
        return Path(env).expanduser()

    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return Path(base) / "LUMI"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LUMI"

    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_SLUG
    return Path.home() / ".local" / "share" / APP_SLUG


class AppPaths:
    """Resolved application directories.

    The object is cheap to construct and is used as a value object throughout
    the codebase instead of scattering ``Path.home()`` style logic everywhere.
    """

    def __init__(self, home: Path | str | None = None) -> None:
        self.home = Path(home).expanduser().resolve() if home else _default_home().expanduser()

    # ------------------------------------------------------------------ dirs
    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def assets_dir(self) -> Path:
        """Static assets shipped with the application (icon, voices metadata)."""
        return Path(__file__).resolve().parent.parent / "assets"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    # ----------------------------------------------------------------- files
    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"

    @property
    def database_file(self) -> Path:
        return self.home / "lumi.db"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "lumi.log"

    @property
    def memory_export_file(self) -> Path:
        return self.home / "memory_export.json"

    def default_workspace(self) -> Path:
        """Folder LUMI is allowed to read/write by default (documents area)."""
        docs = Path.home() / "Documents"
        if not docs.exists():
            docs = Path.home()
        return docs / "LUMI"

    # ------------------------------------------------------------- behaviour
    def ensure(self) -> "AppPaths":
        """Create every directory LUMI needs.  Safe to call repeatedly."""
        for directory in (self.home, self.logs_dir, self.cache_dir, self.data_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"AppPaths(home={str(self.home)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AppPaths) and other.home == self.home

    def __hash__(self) -> int:
        return hash(self.home)


#: Convenience singleton.  Tests create their own :class:`AppPaths` instead.
DEFAULT_PATHS = AppPaths()
