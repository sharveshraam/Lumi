"""Settings, paths, secrets, redaction and the audit trail."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from lumi.config.paths import AppPaths
from lumi.config.secrets import SecretStore
from lumi.config.settings import QUALITY_PROFILES, Settings, SettingsStore
from lumi.security.audit import AuditLog
from lumi.security.policy import PermissionPolicy, RiskLevel
from lumi.security.redact import redact_mapping, redact_text, redact_value


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def test_app_paths_are_created_under_home(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "lumi-home").ensure()
    assert paths.home.exists()
    assert paths.settings_file.parent == paths.home
    assert paths.logs_dir.exists()
    assert paths.cache_dir.exists()
    assert paths.database_file.name.endswith(".db")


def test_app_paths_never_escape_the_home(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "lumi-home").ensure()
    for path in (paths.settings_file, paths.log_file, paths.memory_export_file, paths.database_file):
        assert paths.home in path.parents


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def test_defaults_are_safe(settings_store: SettingsStore) -> None:
    settings = settings_store.settings
    assert settings.schema_version >= 1
    assert settings.cloud.preferred_provider == "local"
    assert settings.privacy.telemetry is False
    assert settings.automation.enabled is True
    assert settings.automation.allow_keyboard_mouse is False
    assert settings.automation.allow_file_writes is False
    assert settings.automation.allow_bulk_file_operations is False
    assert settings.privacy.save_conversations is True
    assert settings.ollama.base_url.startswith("http://127.0.0.1")


def test_settings_round_trip(tmp_path: Path) -> None:
    store = SettingsStore(AppPaths(tmp_path / "home").ensure())
    store.load()
    store.update(profile={"display_name": "Aarav", "college": "NIT Trichy"})
    store.update(model={"selected_model": "llama3.2:3b", "max_tool_iterations": 5})
    store.update(speech={"push_to_talk_hotkey": "Ctrl+Shift+L"})
    store.save()

    reloaded = SettingsStore(AppPaths(tmp_path / "home"))
    settings = reloaded.load()
    assert settings.profile.display_name == "Aarav"
    assert settings.profile.college == "NIT Trichy"
    assert settings.model.selected_model == "llama3.2:3b"
    assert settings.model.max_tool_iterations == 5
    assert settings.speech.push_to_talk_hotkey == "Ctrl+Shift+L"


def test_invalid_values_fall_back_instead_of_crashing(tmp_path: Path) -> None:
    home = AppPaths(tmp_path / "home").ensure()
    payload = {
        "schema_version": 3,
        "ollama": {"base_url": 42, "connect_timeout_s": "soon"},
        "generation": {"temperature": 99, "top_k": -5, "num_ctx": 10},
        "model": {"max_tool_iterations": 500},
        "automation": {"enabled": "maybe", "action_delay_ms": "x"},
        "cloud": {"preferred_provider": "somewhere"},
    }
    home.settings_file.write_text(json.dumps(payload), encoding="utf-8")
    settings = SettingsStore(home).load()
    assert settings.ollama.base_url == "http://127.0.0.1:11434"
    assert 0.0 <= settings.generation.temperature <= 2.0
    assert settings.generation.num_ctx >= 512
    assert 1 <= settings.model.max_tool_iterations <= 10
    assert settings.automation.enabled in (True, False)
    assert settings.cloud.preferred_provider in {"local", "cloud", "ask"}


def test_unknown_keys_are_preserved(tmp_path: Path) -> None:
    home = AppPaths(tmp_path / "home").ensure()
    home.settings_file.write_text(
        json.dumps({"schema_version": 3, "future_section": {"keep": 1}, "model": {"unknown": True}}),
        encoding="utf-8",
    )
    store = SettingsStore(home)
    store.load()
    store.update(model={"selected_model": "llama3.2:1b"})
    store.save()
    written = json.loads(home.settings_file.read_text(encoding="utf-8"))
    assert written["future_section"] == {"keep": 1}
    assert written["model"]["unknown"] is True
    assert written["model"]["selected_model"] == "llama3.2:1b"
    assert '"_extra"' not in home.settings_file.read_text(encoding="utf-8")


def test_corrupt_settings_file_is_backed_up(tmp_path: Path) -> None:
    home = AppPaths(tmp_path / "home").ensure()
    home.settings_file.write_text("{not json at all", encoding="utf-8")
    store = SettingsStore(home)
    settings = store.load()
    assert isinstance(settings, Settings)  # defaults, no crash
    backups = [path for path in home.home.glob("settings.corrupt*") if path != home.settings_file]
    assert backups, "the broken file must be kept for the user to inspect"
    assert "not json" in backups[0].read_text(encoding="utf-8")


def test_listeners_are_called_on_change(settings_store: SettingsStore) -> None:
    seen: list[tuple[str, str]] = []
    settings_store.add_listener(lambda settings, section: seen.append((settings.model.selected_model, section)))
    settings_store.update(model={"selected_model": "qwen2.5:3b"})
    assert seen == [("qwen2.5:3b", "model")]

    seen.clear()
    settings_store.remove_listener(settings_store._listeners[-1])
    settings_store.update(model={"selected_model": "gemma2:2b"})
    assert seen == []


def test_quality_profiles_are_sane() -> None:
    for name, values in QUALITY_PROFILES.items():
        assert values["num_ctx"] >= 1024, name
        assert values["num_ctx"] <= 16384, name
        assert values["num_predict"] <= 4096, name


def test_settings_to_dict_is_json_serialisable(settings_store: SettingsStore) -> None:
    payload = settings_store.settings.to_dict()
    json.dumps(payload)  # must not raise


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------
def test_secret_store_is_private_and_round_trips(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "home").ensure()
    store = SecretStore(paths, use_keyring=False)
    assert store.set("openai", "sk-test-not-a-real-key") == "file"
    secrets_file = paths.home / "secrets.json"
    assert secrets_file.exists()
    if os.name == "posix":
        assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    assert store.get("openai") == "sk-test-not-a-real-key"
    assert SecretStore(paths, use_keyring=False).get("openai") == "sk-test-not-a-real-key"
    store.delete("openai")
    assert store.get("openai") is None


def test_secrets_never_land_in_settings(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "home").ensure()
    store = SecretStore(paths, use_keyring=False)
    store.set("groq", "gsk_abcdefghijklmnop")
    text = paths.settings_file.read_text(encoding="utf-8") if paths.settings_file.exists() else ""
    assert "gsk_abcdefghijklmnop" not in text


def test_cloud_is_disabled_by_default(settings_store: SettingsStore) -> None:
    cloud = settings_store.settings.cloud
    assert cloud.preferred_provider == "local"
    for provider in cloud.providers.values():
        assert provider.enabled is False


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,secret",
    [
        ("my key is sk-abcdefghijklmnopqrstuvwx", "sk-abcdefghijklmnopqrstuvwx"),
        ("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
        (
            "Authorization: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        ),
        ("api_key=supersecretvalue123", "supersecretvalue123"),
    ],
)
def test_redact_removes_secrets(text: str, secret: str) -> None:
    cleaned = redact_text(text)
    assert secret not in cleaned
    assert "redacted" in cleaned.lower()


def test_redact_mapping_hides_sensitive_values() -> None:
    cleaned = redact_mapping({"api_key": "abcdefghijklmnopqrstuvwxyz", "note": "fine"})
    assert "abcdefghijklmnopqrstuvwxyz" not in json.dumps(cleaned)
    assert cleaned["note"] == "fine"


def test_redact_value_walks_the_whole_structure() -> None:
    payload = {"headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz"}, "items": [{"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}]}
    cleaned = json.dumps(redact_value(payload))
    assert "abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in cleaned


def test_redact_text_truncates_predictably() -> None:
    cleaned = redact_text("word " * 200, max_length=100)
    assert len(cleaned) < 200
    assert "truncated" in cleaned
    assert redact_text("short", max_length=100) == "short"


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------
def test_audit_records_actions_in_order(db) -> None:
    audit = AuditLog(db, session_id="test-audit")
    audit.record("add_task", status="ok", args={"title": "Read chapter 4"}, permission="safe")
    audit.record("delete_files", status="denied", args={"paths": ["C:/important"]}, message="needs write permission")
    events = [entry.to_dict() for entry in audit.recent(limit=10)]
    assert [event["tool"] for event in events][:2] == ["delete_files", "add_task"]
    assert any(event["status"] == "denied" for event in events)


def test_audit_redacts_argument_values(db) -> None:
    audit = AuditLog(db, session_id="test-audit")
    audit.record("remember", status="ok", args={"text": "api_key: sk-abcdefghijklmnopqrst"})
    stored = json.dumps([entry.to_dict() for entry in audit.recent(limit=5)])
    assert "sk-abcdefghijklmnopqrst" not in stored


# ---------------------------------------------------------------------------
# start with the system
# ---------------------------------------------------------------------------
def test_startup_launcher_is_written_removed_and_idempotent(tmp_path: Path) -> None:
    from lumi.config import startup

    folder = tmp_path / "Startup"
    command = [sys.executable, str(tmp_path / "main.py")]

    assert not (folder / startup.LAUNCHER_NAME).exists()

    path = startup.enable(folder=folder, platform="win32", command=command)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "main.py" in text
    assert "start " in text, "the launcher must not block the login sequence"

    # Writing it twice must not append a second copy.
    startup.enable(folder=folder, platform="win32", command=command)
    assert path.read_text(encoding="utf-8") == text

    assert startup.disable(folder=folder, platform="win32") is True
    assert not path.exists()
    assert startup.disable(folder=folder, platform="win32") is False


def test_startup_launcher_on_posix_is_a_desktop_entry(monkeypatch, tmp_path: Path) -> None:
    from lumi.config import startup

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    folder = startup.startup_folder(platform="linux")
    assert folder == tmp_path / "config" / "autostart"

    path = startup.enable(platform="linux", command=["/usr/bin/python3", "/opt/lumi/main.py"])
    assert path.name == "lumi.desktop"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]")
    assert "Exec=/usr/bin/python3 /opt/lumi/main.py" in text
    assert startup.is_enabled(platform="linux") is True
    assert startup.disable(platform="linux") is True
    assert startup.is_enabled(platform="linux") is False


def test_launch_command_points_at_a_real_interpreter_and_script() -> None:
    from lumi.config import startup

    command = startup.launch_command()
    assert len(command) == 2
    assert Path(command[1]).name == "main.py"
    assert Path(command[1]).exists(), "the launcher must point at a file that exists"


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def test_log_records_are_interpolated_and_redacted_for_every_handler(tmp_path: Path) -> None:
    """Both the file and the console handler format the same record.

    The first formatter used to leave ``record.args`` empty, so the second
    handler printed the raw template ("%s starting") instead of the message.
    """
    import io
    import logging

    from lumi.config.logging_setup import RedactingFormatter

    formatter = RedactingFormatter("%(message)s")
    logger = logging.getLogger("lumi.test.formatter")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)

    outputs = [io.StringIO(), io.StringIO()]
    for target in outputs:
        handler = logging.StreamHandler(target)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.info("starting with home=%s and %s tools", "/tmp/x", 69)
    for stream in outputs:
        text = stream.getvalue().strip()
        assert "%s" not in text, "the template leaked into the log"
        assert "home=/tmp/x and 69 tools" in text

    logger.info("key sk-abcdefghijklmnopqrstuvwxyz123456 was rejected")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in outputs[0].getvalue()
    assert "redacted" in outputs[0].getvalue()

    logger.handlers.clear()


# ---------------------------------------------------------------------------
# voice helpers (they must fail clearly, never silently)
# ---------------------------------------------------------------------------
def test_speak_with_nothing_installed_explains_what_to_install() -> None:
    from lumi.voice import VoiceUnavailable, speak

    settings = Settings()
    settings.speech.tts_engine = "auto"
    settings.speech.piper_exe = ""
    settings.speech.speak_replies = True
    # Empty text is a no-op, not a failure.
    assert speak("   ", settings=settings) == "none"
    try:
        engine = speak("hello", settings=settings)
    except VoiceUnavailable as exc:
        assert "Piper" in str(exc) or "pyttsx3" in str(exc) or "espeak" in str(exc)
    else:  # a machine that does have a voice is allowed to succeed
        assert engine in {"piper", "sapi", "pyttsx3", "espeak", "espeak-ng"}


def test_push_to_talk_toggles_instead_of_recording_forever() -> None:
    """The hotkey fires on press, so the second press must stop and transcribe."""
    from lumi.voice import VoiceUnavailable, push_to_talk

    class FakeWindow:
        def __init__(self) -> None:
            self.transcribed: list[str] = []

        def _on_transcribed(self, text: str) -> None:
            self.transcribed.append(text)

    window = FakeWindow()

    class FakeSession:
        def __init__(self) -> None:
            self.finished = False

        def finish(self) -> str:
            self.finished = True
            return "turn the kettle on"

    window._voice_session = FakeSession()  # type: ignore[attr-defined]
    push_to_talk(window)
    assert window.transcribed == ["turn the kettle on"]
    assert getattr(window, "_voice_session", None) is None

    # With no session and no audio stack, it must say so rather than hang.
    try:
        push_to_talk(window)
    except VoiceUnavailable:
        pass
