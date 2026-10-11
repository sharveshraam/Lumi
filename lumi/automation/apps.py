"""Known applications and how to open them.

"Open Notepad" must be a cheap, deterministic operation - not an AI request and
not a fragile guess.  This module resolves an application name to something
concrete:

1. an executable on ``PATH`` (``shutil.which``),
2. a Start Menu shortcut on Windows (``.lnk`` files, which ``os.startfile``
   launches exactly like the user clicking them),
3. a well-known install location (``Program Files``, ``AppData\\Local``),
4. a URI scheme (``ms-settings:``, ``spotify:``).

Only applications in this catalogue - or paths the user themselves allowed -
can be launched by a tool.  Unknown names produce a suggestion list rather than
a blind ``cmd /c start``.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("lumi.automation.apps")


@dataclass
class AppSpec:
    """One launchable application."""

    key: str
    display_name: str
    category: str = "general"
    aliases: tuple[str, ...] = ()
    windows_executables: tuple[str, ...] = ()
    windows_paths: tuple[str, ...] = ()
    windows_uri: str = ""
    linux_executables: tuple[str, ...] = ()
    mac_apps: tuple[str, ...] = ()
    description: str = ""
    arg_style: str = "plain"  # plain | uri
    allow_extra_args: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "category": self.category,
            "aliases": list(self.aliases),
            "description": self.description,
        }


@dataclass
class AppResolution:
    """Result of resolving a name to a launchable target."""

    spec: Optional[AppSpec]
    target: str = ""
    method: str = ""  # path | shortcut | uri | shell | executable
    matched_name: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(self.target)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.spec.key if self.spec else "",
            "display_name": self.spec.display_name if self.spec else self.matched_name,
            "target": self.target,
            "method": self.method,
            "notes": list(self.notes),
        }


CATALOGUE: tuple[AppSpec, ...] = (
    AppSpec(
        key="notepad", display_name="Notepad", category="tools",
        aliases=("text editor", "notepadpp", "plain text editor"),
        windows_executables=("notepad.exe", "notepad++.exe"),
        linux_executables=("gedit", "kate", "nano", "mousepad"),
        mac_apps=("TextEdit",),
        description="Simple text editor; useful for quick notes and .txt files.",
    ),
    AppSpec(
        key="calc", display_name="Calculator", category="tools",
        aliases=("calculator", "calc"),
        windows_executables=("calc.exe",), windows_uri="calculator:",
        linux_executables=("gnome-calculator", "kcalc", "xcalc"),
        mac_apps=("Calculator",),
        description="Windows Calculator for quick arithmetic.",
    ),
    AppSpec(
        key="explorer", display_name="File Explorer", category="files",
        aliases=("explorer", "files", "my computer", "file manager", "this pc"),
        windows_executables=("explorer.exe",),
        linux_executables=("nautilus", "dolphin", "thunar", "nemo"),
        mac_apps=("Finder",),
        description="Windows File Explorer for browsing folders.",
    ),
    AppSpec(
        key="settings", display_name="Windows Settings", category="system",
        aliases=("settings", "system settings", "windows settings"),
        windows_uri="ms-settings:",
        description="Opens the Windows Settings app.",
    ),
    AppSpec(
        key="taskmgr", display_name="Task Manager", category="system",
        aliases=("task manager", "taskmgr"),
        windows_executables=("taskmgr.exe",),
        description="Shows running processes and resource usage.",
    ),
    AppSpec(
        key="chrome", display_name="Google Chrome", category="browser",
        aliases=("chrome", "google chrome", "browser"),
        windows_paths=(
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ),
        linux_executables=("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
        mac_apps=("Google Chrome",),
        description="Chrome. LUMI can open pages here; it never reads your saved passwords.",
    ),
    AppSpec(
        key="edge", display_name="Microsoft Edge", category="browser",
        aliases=("edge", "msedge", "microsoft edge"),
        windows_paths=(
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        ),
        linux_executables=("microsoft-edge", "microsoft-edge-stable"),
        mac_apps=("Microsoft Edge",),
        description="Microsoft Edge browser.",
    ),
    AppSpec(
        key="firefox", display_name="Mozilla Firefox", category="browser",
        aliases=("firefox",),
        windows_paths=(
            r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
            r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
        ),
        linux_executables=("firefox",), mac_apps=("Firefox",),
        description="Mozilla Firefox browser.",
    ),
    AppSpec(
        key="code", display_name="Visual Studio Code", category="development",
        aliases=("vscode", "vs code", "code", "visual studio code"),
        windows_executables=("code.cmd", "code.exe"),
        windows_paths=(
            r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
        ),
        linux_executables=("code", "codium"), mac_apps=("Visual Studio Code",),
        description="VS Code editor. LUMI can open a folder or file in it.",
    ),
    AppSpec(
        key="word", display_name="Microsoft Word", category="office",
        aliases=("word", "microsoft word", "docx"),
        windows_executables=("winword.exe",),
        windows_paths=(r"%ProgramFiles%\Microsoft Office\root\Office16\WINWORD.EXE",),
        mac_apps=("Microsoft Word",),
        description="Word documents. Availability depends on your Office install.",
    ),
    AppSpec(
        key="excel", display_name="Microsoft Excel", category="office",
        aliases=("excel", "spreadsheet", "xlsx"),
        windows_executables=("excel.exe",),
        windows_paths=(r"%ProgramFiles%\Microsoft Office\root\Office16\EXCEL.EXE",),
        mac_apps=("Microsoft Excel",),
        description="Excel workbooks (lab readings, marks, budgeting).",
    ),
    AppSpec(
        key="powerpoint", display_name="Microsoft PowerPoint", category="office",
        aliases=("powerpoint", "ppt", "slides"),
        windows_executables=("powerpnt.exe",),
        mac_apps=("Microsoft PowerPoint",),
        description="Slide decks for seminars and project presentations.",
    ),
    AppSpec(
        key="spotify", display_name="Spotify", category="media",
        aliases=("spotify", "music"),
        windows_executables=("Spotify.exe",),
        windows_paths=(r"%AppData%\Spotify\Spotify.exe", r"%LocalAppData%\Microsoft\WindowsApps\Spotify.exe"),
        windows_uri="spotify:",
        linux_executables=("spotify",), mac_apps=("Spotify",),
        description="Music during study sessions.",
    ),
    AppSpec(
        key="vlc", display_name="VLC Media Player", category="media",
        aliases=("vlc", "media player"),
        windows_executables=("vlc.exe",),
        windows_paths=(r"%ProgramFiles%\VideoLAN\VLC\vlc.exe", r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe"),
        linux_executables=("vlc",), mac_apps=("VLC",),
        description="Plays local audio and video files.",
    ),
    AppSpec(
        key="terminal", display_name="Terminal", category="development",
        aliases=("terminal", "cmd", "command prompt", "powershell", "console", "shell"),
        windows_executables=("wt.exe", "powershell.exe", "cmd.exe"),
        linux_executables=("gnome-terminal", "konsole", "xterm"),
        mac_apps=("Terminal",),
        description=(
            "Opens a terminal. LUMI opens it for you but never types commands into it on its own."
        ),
    ),
    AppSpec(
        key="snippingtool", display_name="Snipping Tool", category="tools",
        aliases=("snipping tool", "screenshot tool", "snip"),
        windows_executables=("SnippingTool.exe",), windows_uri="ms-screenclip:",
        description="Windows screenshot tool.",
    ),
    AppSpec(
        key="paint", display_name="Paint", category="tools",
        aliases=("paint", "mspaint"),
        windows_executables=("mspaint.exe",),
        description="Quick image edits and diagrams.",
    ),
    AppSpec(
        key="notepad_plus", display_name="Notepad++", category="tools",
        aliases=("notepad++", "notepad plus"),
        windows_paths=(r"%ProgramFiles%\Notepad++\notepad++.exe", r"%ProgramFiles(x86)%\Notepad++\notepad++.exe"),
        description="Notepad++ for larger text files and code.",
    ),
    AppSpec(
        key="outlook", display_name="Outlook", category="office",
        aliases=("outlook", "mail"),
        windows_executables=("outlook.exe",),
        windows_paths=(r"%ProgramFiles%\Microsoft Office\root\Office16\OUTLOOK.EXE",),
        description="Desktop mail client, if you use it instead of Gmail in the browser.",
    ),
    AppSpec(
        key="arduino", display_name="Arduino IDE", category="development",
        aliases=("arduino", "arduino ide"),
        windows_paths=(r"%LocalAppData%\Programs\Arduino IDE\Arduino IDE.exe", r"%ProgramFiles%\Arduino\arduino.exe"),
        linux_executables=("arduino", "arduino-ide"),
        description="For microcontroller labs.",
    ),
    AppSpec(
        key="tasklist", display_name="Task List", category="system",
        aliases=("process list", "running programs"),
        windows_executables=("tasklist.exe",),
        description="Lists running processes in a console window.",
    ),
)


def catalogue() -> list[AppSpec]:
    return list(CATALOGUE)


def find_spec(name: str) -> Optional[AppSpec]:
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for spec in CATALOGUE:
        if needle == spec.key or needle == spec.display_name.lower():
            return spec
    for spec in CATALOGUE:
        if needle in spec.aliases:
            return spec
    base = Path(needle).stem.lower()
    for spec in CATALOGUE:
        if base in {Path(exe).stem.lower() for exe in spec.windows_executables}:
            return spec
        if base == spec.key:
            return spec
    return None


def suggestions(name: str, *, limit: int = 5) -> list[AppSpec]:
    """Fuzzy-ish suggestions for an unknown name (prefix/substr on aliases)."""
    needle = (name or "").strip().lower()
    if not needle:
        return catalogue()[:limit]
    scored: list[tuple[int, AppSpec]] = []
    for spec in CATALOGUE:
        candidates = (spec.key, spec.display_name.lower()) + tuple(alias.lower() for alias in spec.aliases)
        best = 0
        for candidate in candidates:
            if candidate.startswith(needle) or needle.startswith(candidate):
                best = max(best, 3)
            elif needle in candidate or candidate in needle:
                best = max(best, 2)
            else:
                share = len(set(needle) & set(candidate)) / max(1, len(set(candidate)))
                if share > 0.6:
                    best = max(best, 1)
        if best:
            scored.append((best, spec))
    scored.sort(key=lambda item: (-item[0], item[1].display_name))
    return [spec for _, spec in scored[:limit]]


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def resolve(name: str, *, allowed_paths: Iterable[Path] = ()) -> AppResolution:
    """Turn a user-supplied application name into a launchable target."""
    raw = (name or "").strip().strip('"')
    if not raw:
        return AppResolution(spec=None, notes=["No application name was given."])

    # An explicit path ("./thing.exe", "C:\\Tools\\app.exe") always wins - as long
    # as the user allowed that folder.
    candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
    if candidate.is_absolute() or raw.startswith((".", "~")):
        if candidate.exists() and candidate.is_file():
            resolution = AppResolution(spec=find_spec(candidate.name), target=str(candidate), method="path", matched_name=raw)
            if _within_allowed(candidate, allowed_paths):
                return resolution
            resolution.notes.append(
                "That file is outside your allowed folders, so LUMI will ask before launching it."
            )
            return resolution
        return AppResolution(spec=None, notes=[f"The file '{raw}' does not exist."])

    spec = find_spec(raw)
    if spec is not None:
        resolution = _resolve_spec(spec)
        if resolution.resolved:
            return resolution
        resolution.notes.append(
            f"'{spec.display_name}' was not found in the usual locations. It may not be installed."
        )
        return resolution

    # Not in the catalogue: only accept an executable that is already on PATH.
    exe = shutil.which(raw)
    if exe:
        return AppResolution(spec=None, target=exe, method="executable", matched_name=raw)

    return AppResolution(spec=None, matched_name=raw, notes=[f"LUMI does not know an application called '{raw}'."])


def _resolve_spec(spec: AppSpec) -> AppResolution:
    is_windows = sys.platform.startswith("win")
    is_mac = sys.platform == "darwin"

    executables = spec.windows_executables if is_windows else (() if is_mac else spec.linux_executables)
    for exe in executables:
        found = shutil.which(exe)
        if found:
            return AppResolution(spec=spec, target=found, method="executable", matched_name=spec.key)

    if is_windows:
        for template in spec.windows_paths:
            expanded = os.path.expandvars(template)
            path = Path(expanded)
            if path.exists():
                return AppResolution(spec=spec, target=str(path), method="path", matched_name=spec.key)
        shortcut = _find_start_menu_shortcut(spec)
        if shortcut is not None:
            return AppResolution(spec=spec, target=str(shortcut), method="shortcut", matched_name=spec.key)
        if spec.windows_uri:
            return AppResolution(spec=spec, target=spec.windows_uri, method="uri", matched_name=spec.key)
        return AppResolution(spec=spec, matched_name=spec.key)

    if is_mac:
        apps = spec.mac_apps or (spec.display_name,)
        for app in apps:
            path = Path("/Applications") / f"{app}.app"
            if path.exists():
                return AppResolution(spec=spec, target=str(path), method="path", matched_name=spec.key)
        return AppResolution(spec=spec, matched_name=spec.key)

    for exe in spec.linux_executables:
        found = shutil.which(exe)
        if found:
            return AppResolution(spec=spec, target=found, method="executable", matched_name=spec.key)
    return AppResolution(spec=spec, matched_name=spec.key)


def _find_start_menu_shortcut(spec: AppSpec) -> Optional[Path]:
    """Search the Start Menu for a matching .lnk (Windows only)."""
    if not sys.platform.startswith("win"):
        return None
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    needles = {spec.display_name.lower(), spec.key.lower()}
    needles.update(alias.lower() for alias in spec.aliases)
    for root in roots:
        if not root.exists():
            continue
        try:
            for shortcut in root.rglob("*.lnk"):
                stem = shortcut.stem.lower()
                if any(needle == stem or needle in stem for needle in needles):
                    return shortcut
        except OSError:  # pragma: no cover
            continue
    return None


def _within_allowed(path: Path, allowed_paths: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover
        resolved = path
    for root in allowed_paths:
        try:
            resolved.relative_to(Path(root).expanduser().resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def launch_command(resolution: AppResolution, *, extra_args: str = "") -> list[str] | str:
    """Build the command used by the launcher (kept separate for testability)."""
    target = resolution.target
    if resolution.method == "uri":
        return target
    if resolution.method in {"shortcut", "path", "executable"}:
        if extra_args:
            return [target, *extra_args.split()]
        return [target] if os.name != "nt" else target
    if resolution.method == "shell" and os.name != "nt":
        return ["xdg-open", target]
    return target


def describe_catalogue() -> str:
    lines = []
    for spec in CATALOGUE:
        lines.append(f"{spec.display_name} ({spec.key}) - {spec.description}")
    return "\n".join(lines)
