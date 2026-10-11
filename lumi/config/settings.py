"""Settings model and persistence.

Design notes
------------
* Everything is a plain dataclass with an explicit ``to_dict``/``from_dict``
  pair.  Unknown keys are preserved (``extra``) so a newer version of LUMI can
  write a settings file that an older version reads without losing data, and so
  hand-edited files never crash the app.
* Every value is *coerced and clamped* on load.  A malformed settings file is
  backed up (``settings.corrupt-<timestamp>.json``) and defaults are used.
* Secrets never live here.  API keys go to the OS keyring when available and to
  ``secrets.json`` (mode 0600) otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import copy
import shutil
import time
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from lumi.config.paths import AppPaths
from lumi.version import SETTINGS_SCHEMA_VERSION

log = logging.getLogger("lumi.config.settings")

# --------------------------------------------------------------------------
# Value coercion helpers - tolerant by design, strict where it matters.
# --------------------------------------------------------------------------


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "y"}:
            return True
        if lowered in {"0", "false", "no", "off", "n"}:
            return False
    return default


def _as_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_float(value: Any, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_choice(value: Any, choices: tuple[str, ...], default: str) -> str:
    if isinstance(value, str) and value.strip().lower() in choices:
        return value.strip().lower()
    return default


def _as_str(value: Any, default: str = "", *, max_length: int = 4096) -> str:
    if value is None:
        return default
    return str(value)[:max_length]


def _as_str_list(value: Any, default: list[str] | None = None) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [_as_str(item) for item in value if isinstance(item, (str, int, float))][:200]
    return list(default or [])


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _flatten_extra(node: Any) -> Any:
    """Recursively lift ``_extra`` entries into the dict they belong to."""
    if isinstance(node, dict):
        extra = node.pop("_extra", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                node.setdefault(key, value)
        for value in node.values():
            _flatten_extra(value)
    elif isinstance(node, list):
        for item in node:
            _flatten_extra(item)
    return node


def _clean_base_url(value: str) -> str:
    """Keep only a usable http(s) URL; anything else falls back to the default.

    A stale or hand-edited settings file must not be able to point the Ollama
    client at something that is not a URL - the failure would be baffling.
    """
    candidate = (value or "").strip().rstrip("/")
    if candidate.startswith(("http://", "https://")):
        return candidate
    return OllamaSettings.base_url


class _Serialisable:
    """Mixin providing tolerant (de)serialisation for dataclass settings."""

    _extra: Dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)  # type: ignore[arg-type]
        data.update(copy.deepcopy(getattr(self, "_extra", {}) or {}))
        return data

    @classmethod
    def _known_field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}  # type: ignore[arg-type]

    @classmethod
    def _split_extra(cls, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        known = cls._known_field_names()
        known_values = {k: v for k, v in raw.items() if k in known}
        extra = {k: v for k, v in raw.items() if k not in known and not k.startswith("__")}
        return known_values, extra

    def _store_extra(self, extra: dict[str, Any]) -> None:
        self._extra = extra


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


@dataclass
class OllamaSettings(_Serialisable):
    """Connection settings for the local Ollama service."""

    base_url: str = "http://127.0.0.1:11434"
    connect_timeout_s: float = 3.0
    request_timeout_s: float = 180.0
    stream: bool = True
    keep_alive: str = "10m"
    allow_non_loopback_host: bool = False
    unload_on_idle: bool = True
    idle_unload_after_s: int = 600
    auto_pull_models: bool = False
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "OllamaSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        obj = cls(
            base_url=_clean_base_url(_as_str(known.get("base_url", cls.base_url), cls.base_url, max_length=300)),
            connect_timeout_s=_as_float(known.get("connect_timeout_s"), 3.0, minimum=0.5, maximum=30.0),
            request_timeout_s=_as_float(known.get("request_timeout_s"), 180.0, minimum=10.0, maximum=3600.0),
            stream=_as_bool(known.get("stream"), True),
            keep_alive=_as_str(known.get("keep_alive", "10m"), "10m", max_length=20),
            allow_non_loopback_host=_as_bool(known.get("allow_non_loopback_host"), False),
            unload_on_idle=_as_bool(known.get("unload_on_idle"), True),
            idle_unload_after_s=_as_int(known.get("idle_unload_after_s"), 600, minimum=60, maximum=86400),
            auto_pull_models=_as_bool(known.get("auto_pull_models"), False),
        )
        obj._store_extra(extra)
        return obj


@dataclass
class GenerationSettings(_Serialisable):
    """Conservative default generation parameters for a 16 GB / RTX 3050 laptop."""

    temperature: float = 0.6
    top_p: float = 0.9
    top_k: int = 40
    repeat_penalty: float = 1.1
    num_ctx: int = 4096
    num_predict: int = 512
    seed: Optional[int] = None
    stop: List[str] = field(default_factory=list)
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "GenerationSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        seed = known.get("seed", None)
        try:
            seed_val: Optional[int] = None if seed in (None, "", "null") else int(seed)
        except (TypeError, ValueError):
            seed_val = None
        obj = cls(
            temperature=_as_float(known.get("temperature"), 0.6, minimum=0.0, maximum=2.0),
            top_p=_as_float(known.get("top_p"), 0.9, minimum=0.01, maximum=1.0),
            top_k=_as_int(known.get("top_k"), 40, minimum=1, maximum=200),
            repeat_penalty=_as_float(known.get("repeat_penalty"), 1.1, minimum=0.5, maximum=2.0),
            num_ctx=_as_int(known.get("num_ctx"), 4096, minimum=512, maximum=131072),
            num_predict=_as_int(known.get("num_predict"), 512, minimum=32, maximum=16384),
            seed=seed_val,
            stop=_as_str_list(known.get("stop"), []),
        )
        obj._store_extra(extra)
        return obj


#: Presets offered in Settings → Model.  num_ctx / num_predict are deliberately
#: modest: LUMI targets a 6 GB RTX 3050 laptop and must not exhaust system RAM.
QUALITY_PROFILES: Dict[str, Dict[str, int | float]] = {
    "fast": {"num_ctx": 2048, "num_predict": 256, "temperature": 0.5},
    "balanced": {"num_ctx": 4096, "num_predict": 512, "temperature": 0.6},
    "quality": {"num_ctx": 8192, "num_predict": 1024, "temperature": 0.7},
}


@dataclass
class ModelSettings(_Serialisable):
    selected_model: str = ""
    quality_profile: str = "balanced"
    use_profile_limits: bool = True
    hidden_models: List[str] = field(default_factory=list)
    model_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    system_prompt_extra: str = ""
    enable_tools: bool = True
    max_tool_iterations: int = 3
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "ModelSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        overrides_raw = _as_dict(known.get("model_overrides"))
        overrides: Dict[str, Dict[str, Any]] = {}
        for name, values in overrides_raw.items():
            if isinstance(values, dict):
                overrides[_as_str(name)] = _as_dict(values)
        obj = cls(
            selected_model=_as_str(known.get("selected_model"), "", max_length=200),
            quality_profile=_as_choice(known.get("quality_profile"), ("fast", "balanced", "quality"), "balanced"),
            use_profile_limits=_as_bool(known.get("use_profile_limits"), True),
            hidden_models=_as_str_list(known.get("hidden_models"), []),
            model_overrides=overrides,
            system_prompt_extra=_as_str(known.get("system_prompt_extra"), "", max_length=4000),
            enable_tools=_as_bool(known.get("enable_tools"), True),
            max_tool_iterations=_as_int(known.get("max_tool_iterations"), 3, minimum=1, maximum=10),
        )
        obj._store_extra(extra)
        return obj


@dataclass
class AppearanceSettings(_Serialisable):
    theme: str = "dark"
    accent: str = "#7c5cff"
    font_scale: float = 1.0
    compact_mode: bool = False
    window_mode: str = "normal"  # normal | compact (floating assistant)
    window_width: int = 1180
    window_height: int = 760
    window_x: Optional[int] = None
    window_y: Optional[int] = None
    always_on_top: bool = False
    open_panels: List[str] = field(default_factory=lambda: ["tasks"])
    show_timestamps: bool = True
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "AppearanceSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        x = known.get("window_x")
        y = known.get("window_y")
        obj = cls(
            theme=_as_choice(known.get("theme"), ("dark", "light"), "dark"),
            accent=_as_str(known.get("accent", "#7c5cff"), "#7c5cff", max_length=9),
            font_scale=_as_float(known.get("font_scale"), 1.0, minimum=0.75, maximum=1.75),
            compact_mode=_as_bool(known.get("compact_mode"), False),
            window_mode=_as_choice(known.get("window_mode"), ("normal", "compact"), "normal"),
            window_width=_as_int(known.get("window_width"), 1180, minimum=640, maximum=8000),
            window_height=_as_int(known.get("window_height"), 760, minimum=420, maximum=8000),
            window_x=None if x is None else _as_int(x, 0, minimum=-20000, maximum=20000),
            window_y=None if y is None else _as_int(y, 0, minimum=-20000, maximum=20000),
            always_on_top=_as_bool(known.get("always_on_top"), False),
            open_panels=_as_str_list(known.get("open_panels"), ["tasks"]),
            show_timestamps=_as_bool(known.get("show_timestamps"), True),
        )
        obj._store_extra(extra)
        return obj

    def sync_compact_flag(self) -> None:
        self.compact_mode = self.window_mode == "compact"


@dataclass
class SpeechSettings(_Serialisable):
    enabled: bool = False
    tts_enabled: bool = True
    tts_engine: str = "auto"  # auto | piper | sapi | pyttsx3 | espeak | none
    piper_exe: str = ""
    piper_voice: str = ""
    rate: float = 1.0
    volume: float = 1.0
    stt_engine: str = "auto"  # auto | faster-whisper | whisper.cpp | vosk | none
    stt_model: str = "base"
    stt_device: str = "auto"      # auto | cpu | cuda
    stt_compute_type: str = "auto"
    stt_language: str = "auto"
    push_to_talk_hotkey: str = "Ctrl+Alt+Space"
    push_to_talk_enabled: bool = True
    wake_word_enabled: bool = False
    wake_word: str = "lumi"
    microphone_device: str = ""
    input_gain: float = 1.0
    silence_timeout_s: float = 1.2
    max_record_seconds: int = 30
    retain_audio: bool = False
    speak_replies: bool = False
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "SpeechSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        obj = cls(
            enabled=_as_bool(known.get("enabled"), False),
            tts_enabled=_as_bool(known.get("tts_enabled"), True),
            tts_engine=_as_choice(known.get("tts_engine"), ("auto", "piper", "sapi", "pyttsx3", "espeak", "none"), "auto"),
            piper_exe=_as_str(known.get("piper_exe"), "", max_length=500),
            piper_voice=_as_str(known.get("piper_voice"), "", max_length=500),
            rate=_as_float(known.get("rate"), 1.0, minimum=0.5, maximum=2.5),
            volume=_as_float(known.get("volume"), 1.0, minimum=0.0, maximum=1.0),
            stt_engine=_as_choice(known.get("stt_engine"), ("auto", "faster-whisper", "whisper.cpp", "vosk", "none"), "auto"),
            stt_model=_as_str(known.get("stt_model", "base"), "base", max_length=100),
            stt_device=_as_choice(known.get("stt_device"), ("auto", "cpu", "cuda"), "auto"),
            stt_compute_type=_as_str(known.get("stt_compute_type", "auto"), "auto", max_length=40),
            stt_language=_as_str(known.get("stt_language", "auto"), "auto", max_length=20),
            push_to_talk_hotkey=_as_str(known.get("push_to_talk_hotkey", "Ctrl+Alt+Space"), "Ctrl+Alt+Space", max_length=60),
            push_to_talk_enabled=_as_bool(known.get("push_to_talk_enabled"), True),
            wake_word_enabled=_as_bool(known.get("wake_word_enabled"), False),
            wake_word=_as_str(known.get("wake_word", "lumi"), "lumi", max_length=40),
            microphone_device=_as_str(known.get("microphone_device"), "", max_length=200),
            input_gain=_as_float(known.get("input_gain"), 1.0, minimum=0.0, maximum=8.0),
            silence_timeout_s=_as_float(known.get("silence_timeout_s"), 1.2, minimum=0.3, maximum=10.0),
            max_record_seconds=_as_int(known.get("max_record_seconds"), 30, minimum=2, maximum=600),
            retain_audio=_as_bool(known.get("retain_audio"), False),
            speak_replies=_as_bool(known.get("speak_replies"), False),
        )
        obj._store_extra(extra)
        return obj


@dataclass
class AutomationSettings(_Serialisable):
    """Computer-control policy.  Everything risky is opt-in and off by default."""

    #: Master switch.  Even with this on, every capability below is separate and
    #: every one of them ships off: control is granted by the user, never assumed.
    enabled: bool = True
    allow_app_launch: bool = False
    allow_keyboard_mouse: bool = False
    allow_screenshots: bool = False
    allow_file_operations: bool = True      # reading/searching is safe and on
    allow_file_writes: bool = False
    allow_browser_automation: bool = False
    allow_bulk_file_operations: bool = False
    resume_automation_on_launch: bool = False
    emergency_stop_hotkey: str = "Ctrl+Alt+Shift+S"
    action_delay_ms: int = 60
    typing_interval_ms: int = 12
    step_timeout_s: float = 20.0
    max_steps_per_sequence: int = 60
    require_confirmation_for: List[str] = field(
        default_factory=lambda: [
            "send_email",
            "submit_form",
            "delete_file",
            "purchase",
            "install_software",
            "change_security_settings",
            "bulk_file_operation",
        ]
    )
    workspace_folders: List[str] = field(default_factory=list)
    search_folders: List[str] = field(default_factory=list)
    blocklist_folders: List[str] = field(default_factory=list)
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "AutomationSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        defaults = cls()
        obj = cls(
            enabled=_as_bool(known.get("enabled"), True),
            allow_app_launch=_as_bool(known.get("allow_app_launch"), False),
            allow_keyboard_mouse=_as_bool(known.get("allow_keyboard_mouse"), False),
            allow_screenshots=_as_bool(known.get("allow_screenshots"), False),
            allow_file_operations=_as_bool(known.get("allow_file_operations"), True),
            allow_file_writes=_as_bool(known.get("allow_file_writes"), False),
            allow_browser_automation=_as_bool(known.get("allow_browser_automation"), False),
            allow_bulk_file_operations=_as_bool(known.get("allow_bulk_file_operations"), False),
            resume_automation_on_launch=_as_bool(known.get("resume_automation_on_launch"), False),
            emergency_stop_hotkey=_as_str(known.get("emergency_stop_hotkey", "Ctrl+Alt+Shift+S"), "Ctrl+Alt+Shift+S", max_length=60),
            action_delay_ms=_as_int(known.get("action_delay_ms"), 60, minimum=0, maximum=5000),
            typing_interval_ms=_as_int(known.get("typing_interval_ms"), 12, minimum=0, maximum=1000),
            step_timeout_s=_as_float(known.get("step_timeout_s"), 20.0, minimum=1.0, maximum=300.0),
            max_steps_per_sequence=_as_int(known.get("max_steps_per_sequence"), 60, minimum=1, maximum=500),
            require_confirmation_for=_as_str_list(known.get("require_confirmation_for"), defaults.require_confirmation_for),
            workspace_folders=_as_str_list(known.get("workspace_folders"), []),
            search_folders=_as_str_list(known.get("search_folders"), []),
            blocklist_folders=_as_str_list(known.get("blocklist_folders"), []),
        )
        obj._store_extra(extra)
        return obj


@dataclass
class PrivacySettings(_Serialisable):
    save_conversations: bool = True
    conversation_retention_days: int = 30
    remember_from_chat: bool = False
    ask_before_remembering: bool = True
    telemetry: bool = False  # always False; kept for explicitness, no telemetry exists
    log_level: str = "INFO"
    log_prompts: bool = False
    allow_file_indexing: bool = False
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "PrivacySettings":
        known, extra = cls._split_extra(_as_dict(raw))
        obj = cls(
            save_conversations=_as_bool(known.get("save_conversations"), True),
            conversation_retention_days=_as_int(known.get("conversation_retention_days"), 30, minimum=0, maximum=3650),
            remember_from_chat=_as_bool(known.get("remember_from_chat"), False),
            ask_before_remembering=_as_bool(known.get("ask_before_remembering"), True),
            telemetry=False,
            log_level=_as_choice(known.get("log_level"), ("DEBUG", "INFO", "WARNING", "ERROR"), "INFO"),
            log_prompts=_as_bool(known.get("log_prompts"), False),
            allow_file_indexing=_as_bool(known.get("allow_file_indexing"), False),
        )
        obj._store_extra(extra)
        return obj


@dataclass
class NotificationSettings(_Serialisable):
    reminders_enabled: bool = True
    sound_enabled: bool = True
    tray_notifications: bool = True
    focus_length_minutes: int = 25
    break_length_minutes: int = 5
    break_notifications: bool = True
    daily_briefing_enabled: bool = False
    daily_briefing_time: str = "08:30"
    catch_up_missed_reminders: bool = True
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "NotificationSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        briefing = _as_str(known.get("daily_briefing_time", "08:30"), "08:30", max_length=5)
        if not _valid_hhmm(briefing):
            briefing = "08:30"
        obj = cls(
            reminders_enabled=_as_bool(known.get("reminders_enabled"), True),
            sound_enabled=_as_bool(known.get("sound_enabled"), True),
            tray_notifications=_as_bool(known.get("tray_notifications"), True),
            focus_length_minutes=_as_int(known.get("focus_length_minutes"), 25, minimum=5, maximum=180),
            break_length_minutes=_as_int(known.get("break_length_minutes"), 5, minimum=1, maximum=60),
            break_notifications=_as_bool(known.get("break_notifications"), True),
            daily_briefing_enabled=_as_bool(known.get("daily_briefing_enabled"), False),
            daily_briefing_time=briefing,
            catch_up_missed_reminders=_as_bool(known.get("catch_up_missed_reminders"), True),
        )
        obj._store_extra(extra)
        return obj


def _valid_hhmm(value: str) -> bool:
    try:
        hours, minutes = value.split(":")
        return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59
    except (ValueError, AttributeError):
        return False


@dataclass
class StartupSettings(_Serialisable):
    launch_on_login: bool = False
    start_minimised: bool = False
    minimize_to_tray: bool = True
    close_to_tray: bool = True
    check_ollama_on_start: bool = True
    show_briefing_on_start: bool = False
    last_run_version: str = ""
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "StartupSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        obj = cls(
            launch_on_login=_as_bool(known.get("launch_on_login"), False),
            start_minimised=_as_bool(known.get("start_minimised"), False),
            minimize_to_tray=_as_bool(known.get("minimize_to_tray"), True),
            close_to_tray=_as_bool(known.get("close_to_tray"), True),
            check_ollama_on_start=_as_bool(known.get("check_ollama_on_start"), True),
            show_briefing_on_start=_as_bool(known.get("show_briefing_on_start"), False),
            last_run_version=_as_str(known.get("last_run_version"), "", max_length=40),
        )
        obj._store_extra(extra)
        return obj


@dataclass
class ProductivitySettings(_Serialisable):
    default_category: str = "general"
    default_priority: str = "medium"
    show_completed_tasks: bool = False
    overdue_first: bool = True
    #: Handle clear task/reminder commands with plain Python instead of asking
    #: the model.  Saves time, works offline, and costs nothing.
    deterministic_commands: bool = True
    task_categories: List[str] = field(
        default_factory=lambda: ["college", "personal", "coding", "projects", "general"]
    )
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "ProductivitySettings":
        known, extra = cls._split_extra(_as_dict(raw))
        categories = _as_str_list(known.get("task_categories"), ["college", "personal", "coding", "projects", "general"])
        if not categories:
            categories = ["general"]
        obj = cls(
            default_category=_as_choice(
                known.get("default_category"), tuple(categories), categories[-1]
            ),
            default_priority=_as_choice(known.get("default_priority"), ("low", "medium", "high", "urgent"), "medium"),
            show_completed_tasks=_as_bool(known.get("show_completed_tasks"), False),
            overdue_first=_as_bool(known.get("overdue_first"), True),
            deterministic_commands=_as_bool(known.get("deterministic_commands"), True),
            task_categories=categories,
        )
        obj._store_extra(extra)
        return obj


@dataclass
class BrowserSettings(_Serialisable):
    """How LUMI uses the browser (no cookies, no stored credentials, ever)."""

    search_engine: str = "duckduckgo"
    preferred_browser: str = ""
    confirm_before_navigating: bool = False
    read_page_method: str = "auto"   # auto | uia | clipboard
    gmail_compose_url: str = "https://mail.google.com/mail/?view=cm&fs=1"
    max_page_chars: int = 12000
    open_links_in_new_tab: bool = True
    home_page: str = ""
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "BrowserSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        obj = cls(
            search_engine=_as_choice(
                known.get("search_engine"),
                ("google", "duckduckgo", "bing", "brave", "startpage", "ecosia", "youtube", "github", "stackoverflow"),
                "duckduckgo",
            ),
            preferred_browser=_as_str(known.get("preferred_browser"), "", max_length=80),
            confirm_before_navigating=_as_bool(known.get("confirm_before_navigating"), False),
            read_page_method=_as_choice(known.get("read_page_method"), ("auto", "uia", "clipboard"), "auto"),
            gmail_compose_url=_as_str(known.get("gmail_compose_url", "https://mail.google.com/mail/?view=cm&fs=1"), "https://mail.google.com/mail/?view=cm&fs=1", max_length=400),
            max_page_chars=_as_int(known.get("max_page_chars"), 12000, minimum=1000, maximum=100000),
            open_links_in_new_tab=_as_bool(known.get("open_links_in_new_tab"), True),
            home_page=_as_str(known.get("home_page"), "", max_length=500),
        )
        obj._store_extra(extra)
        return obj


@dataclass
class ProfileSettings(_Serialisable):
    """Optional personal details used to fill in forms and sign notes.

    These stay on this machine and are never sent anywhere.  They are used only
    when the user asks LUMI to write something that needs them.
    """

    display_name: str = ""
    email: str = ""
    college: str = ""
    course: str = "Electronics and Communication Engineering"
    signature: str = ""
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "ProfileSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        obj = cls(
            display_name=_as_str(known.get("display_name"), "", max_length=120),
            email=_as_str(known.get("email"), "", max_length=200),
            college=_as_str(known.get("college"), "", max_length=200),
            course=_as_str(known.get("course", "Electronics and Communication Engineering"), "", max_length=200),
            signature=_as_str(known.get("signature"), "", max_length=500),
        )
        obj._store_extra(extra)
        return obj


@dataclass
class CloudProviderSettings(_Serialisable):
    """A single optional cloud provider entry.  Disabled unless configured."""

    name: str = ""
    enabled: bool = False
    api_key_env: str = ""
    model: str = ""
    base_url: str = ""
    max_requests_per_day: int = 50
    allow_automatic_fallback: bool = False
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "CloudProviderSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        obj = cls(
            name=_as_str(known.get("name"), "", max_length=40),
            enabled=_as_bool(known.get("enabled"), False),
            api_key_env=_as_str(known.get("api_key_env"), "", max_length=80),
            model=_as_str(known.get("model"), "", max_length=120),
            base_url=_as_str(known.get("base_url"), "", max_length=300),
            max_requests_per_day=_as_int(known.get("max_requests_per_day"), 50, minimum=1, maximum=100000),
            allow_automatic_fallback=_as_bool(known.get("allow_automatic_fallback"), False),
        )
        obj._store_extra(extra)
        return obj


def _default_cloud_providers() -> Dict[str, CloudProviderSettings]:
    return {
        "gemini": CloudProviderSettings(
            name="gemini", enabled=False, api_key_env="LUMI_GEMINI_API_KEY", model="", base_url=""
        ),
        "openai": CloudProviderSettings(
            name="openai", enabled=False, api_key_env="LUMI_OPENAI_API_KEY", model="", base_url=""
        ),
    }


@dataclass
class CloudSettings(_Serialisable):
    #: ``local`` (default) keeps everything on this machine.  ``cloud`` must be
    #: chosen explicitly by the user; LUMI never switches on its own.
    preferred_provider: str = "local"
    providers: Dict[str, CloudProviderSettings] = field(default_factory=_default_cloud_providers)
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "CloudSettings":
        known, extra = cls._split_extra(_as_dict(raw))
        providers_raw = _as_dict(known.get("providers"))
        providers: Dict[str, CloudProviderSettings] = {}
        for key, value in {**_default_cloud_providers(), **providers_raw}.items():
            entry = CloudProviderSettings.from_dict(value)
            if not entry.name:
                entry.name = key
            providers[key] = entry
        obj = cls(
            # Anything that is not a known provider is treated as "local": the
            # safe default, never an accidental switch to cloud.
            preferred_provider=_as_choice(
                known.get("preferred_provider"), ("local", "cloud", "ask"), "local"
            ),
            providers=providers,
        )
        obj._store_extra(extra)
        return obj

    def enabled_providers(self) -> list[CloudProviderSettings]:
        return [p for p in self.providers.values() if p.enabled]


# --------------------------------------------------------------------------
# Root settings object
# --------------------------------------------------------------------------


@dataclass
class Settings(_Serialisable):
    schema_version: int = SETTINGS_SCHEMA_VERSION
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    model: ModelSettings = field(default_factory=ModelSettings)
    generation: GenerationSettings = field(default_factory=GenerationSettings)
    appearance: AppearanceSettings = field(default_factory=AppearanceSettings)
    speech: SpeechSettings = field(default_factory=SpeechSettings)
    automation: AutomationSettings = field(default_factory=AutomationSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    startup: StartupSettings = field(default_factory=StartupSettings)
    productivity: ProductivitySettings = field(default_factory=ProductivitySettings)
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    profile: ProfileSettings = field(default_factory=ProfileSettings)
    cloud: CloudSettings = field(default_factory=CloudSettings)
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Flatten unknown keys so the file on disk stays readable.

        ``_extra`` is how settings written by a newer LUMI version survive a
        downgrade, but it is an implementation detail: putting ``"_extra": {...}``
        in the user's settings file looks like a bug.  Unknown keys are lifted to
        the level they belong to, and read back into ``_extra`` on load.
        """
        data = super().to_dict()
        own_extra = data.pop("_extra", {}) or {}
        if isinstance(own_extra, dict):
            for key, value in own_extra.items():
                data.setdefault(key, value)
        return _flatten_extra(data)

    # ------------------------------------------------------------ factory
    @classmethod
    def from_dict(cls, raw: Any) -> "Settings":
        data = _as_dict(raw)
        known, extra = cls._split_extra(data)
        obj = cls(
            schema_version=_as_int(known.get("schema_version"), SETTINGS_SCHEMA_VERSION, minimum=1, maximum=9999),
            ollama=OllamaSettings.from_dict(known.get("ollama")),
            model=ModelSettings.from_dict(known.get("model")),
            generation=GenerationSettings.from_dict(known.get("generation")),
            appearance=AppearanceSettings.from_dict(known.get("appearance")),
            speech=SpeechSettings.from_dict(known.get("speech")),
            automation=AutomationSettings.from_dict(known.get("automation")),
            privacy=PrivacySettings.from_dict(known.get("privacy")),
            notifications=NotificationSettings.from_dict(known.get("notifications")),
            startup=StartupSettings.from_dict(known.get("startup")),
            productivity=ProductivitySettings.from_dict(known.get("productivity")),
            browser=BrowserSettings.from_dict(known.get("browser")),
            profile=ProfileSettings.from_dict(known.get("profile")),
            cloud=CloudSettings.from_dict(known.get("cloud")),
        )
        obj._store_extra(extra)
        obj.migrate()
        return obj

    def migrate(self) -> list[str]:
        """Apply schema migrations in place.  Returns a list of notes."""
        notes: list[str] = []
        if self.schema_version < 2:
            # v1 stored the model name in ollama.selected_model.
            legacy = _as_dict(getattr(self.ollama, "_extra", {})).get("selected_model")
            if legacy and not self.model.selected_model:
                self.model.selected_model = _as_str(legacy, "", max_length=200)
                notes.append("migrated ollama.selected_model -> model.selected_model")
            if self.generation.num_ctx == 4096 and self.generation.num_predict == 512:
                pass  # defaults unchanged
            self.schema_version = 2
        if self.schema_version < 3:
            # v3 introduced explicit quality profiles, cloud provider entries,
            # browser preferences and the profile section.
            if self.model.quality_profile not in QUALITY_PROFILES:
                self.model.quality_profile = "balanced"
            self.schema_version = 3
        self.schema_version = SETTINGS_SCHEMA_VERSION
        self.appearance.sync_compact_flag()
        return notes

    # ------------------------------------------------------------ helpers
    def effective_generation(self, model: str | None = None) -> GenerationSettings:
        """Merge profile defaults, per-model overrides and user values."""
        merged = GenerationSettings.from_dict(self.generation.to_dict())
        profile = QUALITY_PROFILES.get(self.model.quality_profile, QUALITY_PROFILES["balanced"])
        if self.model.use_profile_limits:
            if "num_ctx" in profile:
                merged.num_ctx = int(profile["num_ctx"])
            if "num_predict" in profile:
                merged.num_predict = int(profile["num_predict"])
        name = model or self.model.selected_model
        override = self.model.model_overrides.get(name) if name else None
        if override:
            merged = GenerationSettings.from_dict({**merged.to_dict(), **override})
        return merged

    def model_options(self, model: str | None = None) -> dict[str, Any]:
        """Ollama ``options`` payload built from the effective generation config."""
        gen = self.effective_generation(model)
        options: dict[str, Any] = {
            "temperature": gen.temperature,
            "top_p": gen.top_p,
            "top_k": gen.top_k,
            "repeat_penalty": gen.repeat_penalty,
            "num_ctx": gen.num_ctx,
            "num_predict": gen.num_predict,
        }
        if gen.seed is not None:
            options["seed"] = gen.seed
        if gen.stop:
            options["stop"] = list(gen.stop)
        return options

    def workspace_folders(self) -> list[Path]:
        folders = [Path(p) for p in self.automation.workspace_folders if p.strip()]
        return folders or [AppPaths().default_workspace()]

    def is_configured(self) -> bool:
        return bool(self.model.selected_model)


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class SettingsStore:
    """Loads, validates and persists :class:`Settings`.

    The store owns the single instance of ``Settings`` used by the running
    application and notifies listeners when something changes, which lets the UI
    react (theme switch, model change) without polling.
    """

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._settings = Settings()
        self._listeners: list[Callable[[Settings, str], None]] = []
        self._loaded = False

    # ------------------------------------------------------------- loading
    @property
    def settings(self) -> Settings:
        if not self._loaded:
            self.load()
        return self._settings

    def load(self) -> Settings:
        path = self.paths.settings_file
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("settings root must be a JSON object")
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                log.warning("settings file unreadable (%s); using defaults", exc)
                self._backup_corrupt(path)
                raw = {}
        self._settings = Settings.from_dict(raw)
        self._loaded = True
        return self._settings

    def _backup_corrupt(self, path: Path) -> None:
        try:
            target = path.with_name(f"settings.corrupt-{int(time.time())}.json")
            shutil.copy2(path, target)
            log.warning("kept a copy of the unreadable settings at %s", target)
        except OSError:  # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------- saving
    def save(self) -> Path:
        """Atomic write so a crash cannot leave a half-written settings file."""
        self.paths.ensure()
        path = self.paths.settings_file
        tmp = path.with_suffix(".json.tmp")
        payload = json.dumps(self.settings.to_dict(), indent=2, sort_keys=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - Windows/POSIX differences
            pass
        return path

    # ---------------------------------------------------------- mutating
    def update(self, **section_changes: Any) -> Settings:
        """Shallow-update top level settings sections.

        Example::

            store.update(appearance={"theme": "dark"})
        """
        for section, change in section_changes.items():
            current = getattr(self.settings, section, None)
            if current is None:
                setattr(self.settings, section, change)
                continue
            if is_dataclass(current) and isinstance(change, dict):
                merged = {**current.to_dict(), **change}  # type: ignore[call-arg]
                setattr(self.settings, section, type(current).from_dict(merged))
            else:
                setattr(self.settings, section, change)
        self.notify(section=",".join(section_changes.keys()) or "all")
        return self.settings

    def replace(self, settings: Settings) -> None:
        self._settings = settings
        self._loaded = True
        self.notify(section="all")

    def reset_to_defaults(self, *, keep_secret_free: bool = True) -> Settings:
        del keep_secret_free  # settings never contain secrets
        return Settings.from_dict({"schema_version": SETTINGS_SCHEMA_VERSION})

    # ----------------------------------------------------------- listeners
    def add_listener(self, callback: Callable[[Settings, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Settings, str], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def notify(self, section: str = "all") -> None:
        for callback in list(self._listeners):
            try:
                callback(self.settings, section)
            except Exception:  # pragma: no cover - a listener must not break saving
                log.exception("settings listener failed")
