"""Start-with-the-system support.

LUMI does this the visible way: it writes a launcher into your own Startup
folder instead of touching the registry.  Windows runs anything in

    %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup

at sign-in, so a small ``LUMI.cmd`` there is all it takes.  On Linux/macOS the
equivalent is a freedesktop ``.desktop`` file in ``~/.config/autostart``.

Both are just files: deleting the launcher turns auto-start off, and
``disable()`` does exactly that.  Nothing here needs administrator rights.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

LAUNCHER_NAME = "LUMI.cmd"
DESKTOP_ENTRY_NAME = "lumi.desktop"


def startup_folder(*, platform: str | None = None) -> Path:
    """Where the launcher belongs for this platform."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    if platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "autostart"


def launcher_path(*, platform: str | None = None) -> Path:
    platform = platform or sys.platform
    name = LAUNCHER_NAME if platform.startswith("win") else DESKTOP_ENTRY_NAME
    return startup_folder(platform=platform) / name


def is_enabled(*, platform: str | None = None) -> bool:
    return launcher_path(platform=platform).exists()


def launch_command(
    *,
    python_exe: str | None = None,
    main_script: Path | str | None = None,
) -> list[str]:
    """The command the launcher should run.

    A frozen build is its own executable; a source checkout is
    ``pythonw.exe main.py`` (``pythonw`` so no console window flashes up).
    """
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable))]
    interpreter = Path(python_exe) if python_exe else Path(sys.executable)
    if interpreter.name.lower().startswith("python") and interpreter.suffix.lower() == ".exe":
        windowless = interpreter.with_name("pythonw.exe")
        if windowless.exists():
            interpreter = windowless
    script = Path(main_script) if main_script else Path(__file__).resolve().parents[2] / "main.py"
    return [str(interpreter), str(script)]


def _windows_launcher(command: list[str]) -> str:
    quoted = " ".join(f'"{part}"' for part in command)
    return (
        "@echo off\r\n"
        "REM Written by LUMI (Settings -> Startup).  Delete this file to stop LUMI\r\n"
        "REM starting with Windows.\r\n"
        f"start \"\" {quoted}\r\n"
    )


def _linux_launcher(command: list[str]) -> str:
    quoted = " ".join(f'"{part}"' if " " in part else part for part in command)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=LUMI\n"
        "Comment=Local-first AI desktop assistant\n"
        f"Exec={quoted}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def enable(
    *,
    folder: Path | None = None,
    platform: str | None = None,
    command: list[str] | None = None,
) -> Path:
    """Write the launcher.  Safe to call repeatedly; it just rewrites the file."""
    platform = platform or sys.platform
    command = command or launch_command()
    target_dir = Path(folder) if folder is not None else startup_folder(platform=platform)
    target_dir.mkdir(parents=True, exist_ok=True)

    if platform.startswith("win"):
        path = target_dir / LAUNCHER_NAME
        path.write_text(_windows_launcher(command), encoding="utf-8", newline="")
    else:
        path = target_dir / DESKTOP_ENTRY_NAME
        path.write_text(_linux_launcher(command), encoding="utf-8")
        try:  # only meaningful where the bit exists (POSIX)
            path.chmod(0o755)
        except OSError:  # pragma: no cover - filesystem dependent
            pass
    return path


def disable(*, folder: Path | None = None, platform: str | None = None) -> bool:
    """Remove the launcher.  Returns True when something was actually removed."""
    platform = platform or sys.platform
    if folder is not None:
        candidates = [Path(folder) / LAUNCHER_NAME, Path(folder) / DESKTOP_ENTRY_NAME]
    else:
        candidates = [launcher_path(platform=platform)]
    removed = False
    for path in candidates:
        try:
            if path.exists():
                path.unlink()
                removed = True
        except OSError:
            continue
    return removed
