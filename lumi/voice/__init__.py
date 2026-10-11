"""Voice input and output.

Everything here is optional: if neither ``faster-whisper`` nor ``piper`` is
installed, the functions raise :class:`VoiceUnavailable` with a message the UI
shows instead of failing silently.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("lumi.voice")


class VoiceUnavailable(RuntimeError):
    """Raised when a voice component (STT or TTS) is not installed."""


def stt_available() -> tuple[bool, str]:
    """Can LUMI transcribe speech?  Returns (available, explanation)."""
    try:
        import faster_whisper  # type: ignore  # noqa: F401

        return True, "faster-whisper is installed."
    except Exception:  # noqa: BLE001 - any import problem means "not available"
        pass
    import shutil

    if shutil.which("whisper-cli") or shutil.which("main"):
        return True, "whisper.cpp is installed."
    return False, (
        "Speech input needs faster-whisper. Install it with "
        '"pip install faster-whisper" — LUMI works normally without it.'
    )


def tts_available(settings: Any = None) -> tuple[bool, str]:
    """Can LUMI speak?  Piper is preferred, the OS voice is the fallback."""
    import shutil

    piper = getattr(settings, "piper_exe", "") if settings is not None else ""
    if piper and shutil.which(str(piper)):
        return True, f"Piper is configured at {piper}."
    if shutil.which("piper"):
        return True, "piper is on PATH."
    try:
        import pyttsx3  # type: ignore  # noqa: F401

        return True, "pyttsx3 will use the system voice."
    except Exception:  # noqa: BLE001
        pass
    if shutil.which("espeak") or shutil.which("espeak-ng"):
        return True, "espeak is installed."
    return False, (
        "Speech output needs Piper (recommended) or pyttsx3. "
        "LUMI works normally without it."
    )


def push_to_talk(parent: Any = None) -> None:
    """Start or stop push-to-talk.

    A global hotkey fires on the key press, not on the key release, so this is
    a toggle: the first press starts recording, the second stops it, transcribes
    locally and hands the text to the window.  The session is kept on the window
    so a second press can find it, and the audio file is deleted as soon as it
    has been transcribed unless the user asked to keep it.
    """
    session = getattr(parent, "_voice_session", None)
    if session is not None:
        setattr(parent, "_voice_session", None)
        text = session.finish()
        deliver = getattr(parent, "_on_transcribed", None)
        if callable(deliver) and text:
            deliver(text)
        return

    available, reason = stt_available()
    if not available:
        raise VoiceUnavailable(reason)

    from lumi.voice.recorder import PushToTalkSession  # imported lazily on purpose

    session = PushToTalkSession(parent=parent)
    session.begin()
    setattr(parent, "_voice_session", session)


def speak(text: str, *, settings: Any = None, target: Any = None) -> str:
    """Say ``text`` out loud.  Returns the engine that was used.

    Piper is preferred when it is configured, because it sounds much better than
    the system voice.  On Windows the built-in SAPI voice is used through
    PowerShell, which needs no extra package at all.  Anything that fails raises
    :class:`VoiceUnavailable` with a message the UI can show.
    """
    import shutil
    import subprocess
    import tempfile

    text = (text or "").strip()
    if not text:
        return "none"

    speech = getattr(settings, "speech", None)
    engine = (getattr(speech, "tts_engine", "") or "auto").lower()
    piper_exe = getattr(speech, "piper_exe", "") or ""
    piper_voice = getattr(speech, "piper_voice", "") or ""
    rate = int(getattr(speech, "rate", 175) or 175)
    volume = float(getattr(speech, "volume", 1.0) or 1.0)

    if engine in ("auto", "piper") and piper_exe and piper_voice and Path(piper_exe).exists():
        if _speak_with_piper(text, piper_exe, piper_voice):
            return "piper"
        if engine == "piper":
            raise VoiceUnavailable("Piper ran but produced no audio; check the voice file.")

    if engine in ("auto", "sapi") and _windows_sapi_available():
        if _speak_with_sapi(text, rate, volume):
            return "sapi"
        if engine == "sapi":
            raise VoiceUnavailable("The Windows voice (SAPI) could not speak.")

    if engine in ("auto", "pyttsx3", "espeak"):
        try:
            import pyttsx3  # type: ignore

            driver = pyttsx3.init()
            driver.setProperty("rate", rate)
            driver.setProperty("volume", max(0.0, min(1.0, volume)))
            driver.say(text)
            driver.runAndWait()
            return "pyttsx3"
        except Exception as exc:  # noqa: BLE001 - optional dependency
            log.debug("pyttsx3 could not speak: %s", exc)

    if engine in ("auto", "espeak"):
        for name in ("espeak-ng", "espeak"):
            binary = shutil.which(name)
            if binary:
                subprocess.run(  # noqa: S603 - a fixed binary from PATH
                    [binary, "-s", str(rate), text],
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
                return name

    raise VoiceUnavailable(
        "Speech output needs Piper (recommended), pyttsx3, or espeak. "
        "Set the engine in Settings → Voice."
    )


def _windows_sapi_available() -> bool:
    import sys

    return sys.platform.startswith("win")


def _speak_with_sapi(text: str, rate: int, volume: float) -> bool:
    """Use the voice Windows already has - no pip package required."""
    import subprocess

    escaped = text.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {max(-10, min(10, int((rate - 175) / 25)))}; "
        f"$s.Volume = {int(max(0.0, min(1.0, volume)) * 100)}; "
        f"$s.Speak('{escaped}')"
    )
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable, quoted script
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("SAPI failed: %s", exc)
        return False
    return result.returncode == 0


def _speak_with_piper(text: str, piper_exe: str, voice: str) -> bool:
    import subprocess
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix="lumi-voice-") as folder:
            wav = Path(folder) / "speech.wav"
            result = subprocess.run(  # noqa: S603 - user-configured executable
                [piper_exe, "--model", voice, "--output_file", str(wav)],
                input=text.encode("utf-8"),
                check=False,
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0 or not wav.exists():
                return False
            return _play_file(wav)
    except Exception as exc:  # noqa: BLE001
        log.debug("Piper failed: %s", exc)
        return False


def _play_file(path: Path) -> bool:
    """Play a WAV through whatever the platform has."""
    import subprocess
    import sys

    if sys.platform.startswith("win"):
        import winsound  # type: ignore

        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return True
    for command in (["paplay", str(path)], ["aplay", "-q", str(path)], ["afplay", str(path)], ["ffplay", "-nodisp", "-autoexit", str(path)]):
        import shutil

        if shutil.which(command[0]):
            subprocess.run(command, check=False, capture_output=True, timeout=120)  # noqa: S603
            return True
    return False


__all__ = ["VoiceUnavailable", "speak", "stt_available", "tts_available", "push_to_talk"]
