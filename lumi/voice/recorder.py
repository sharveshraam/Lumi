"""Microphone capture for push-to-talk.

Kept deliberately small and dependency-tolerant:

* ``sounddevice`` records when it is installed (it is the least fussy option on
  Windows and honours the chosen microphone);
* otherwise the module says so plainly - it never pretends to record.

Audio is held in memory and deleted as soon as it has been transcribed, unless
``speech.retain_audio`` is switched on.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("lumi.voice.recorder")

SAMPLE_RATE = 16000
CHANNELS = 1


class Recorder:
    """Records mono 16 kHz audio while :meth:`stop` has not been called."""

    def __init__(self, *, device: str = "", gain: float = 1.0, max_seconds: int = 30) -> None:
        self.device = device
        self.gain = max(0.1, float(gain or 1.0))
        self.max_seconds = max(1, min(int(max_seconds or 30), 300))
        self._frames: list[Any] = []
        self._stream: Any = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- recording
    def start(self) -> None:
        try:
            import sounddevice  # type: ignore
            import numpy  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Recording needs the 'sounddevice' and 'numpy' packages. "
                "Install them with: pip install sounddevice numpy"
            ) from exc

        self._numpy = numpy
        device = None
        if self.device:
            device = self.device

        def callback(indata, _frames, _time_info, status) -> None:  # pragma: no cover - audio thread
            if status:
                log.debug("audio status: %s", status)
            with self._lock:
                self._frames.append(indata.copy())

        try:
            self._stream = sounddevice.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                device=device,
                callback=callback,
            )
            self._stream.start()
            log.info("recording started")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Could not open the microphone: {exc}") from exc

    def stop(self) -> Optional[Path]:
        """Stop recording and write a temporary WAV file.  Returns None if empty."""
        self._stopping.set()
        stream = self._stream
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                log.debug("closing the audio stream failed", exc_info=True)
            self._stream = None

        with self._lock:
            frames = list(self._frames)
            self._frames.clear()
        if not frames:
            return None

        numpy = self._numpy
        audio = numpy.concatenate(frames, axis=0)
        if self.gain != 1.0:
            audio = numpy.clip(audio * self.gain, -1.0, 1.0)

        handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        path = Path(handle.name)
        handle.close()
        _write_wav(path, audio, SAMPLE_RATE)
        log.info("recording stopped (%.1f s)", len(audio) / SAMPLE_RATE)
        return path

    @property
    def running(self) -> bool:
        return self._stream is not None


class PushToTalkSession:
    """One push-to-talk interaction, tied to the settings and the window."""

    def __init__(self, *, parent: Any = None, settings: Any = None, paths: Any = None) -> None:
        self.parent = parent
        self.settings = settings or _settings_from(parent)
        self.paths = paths or getattr(parent, "services", None)
        self.recorder: Optional[Recorder] = None
        self.started_at = 0.0

    def begin(self) -> None:
        speech = getattr(self.settings, "speech", None)
        self.recorder = Recorder(
            device=getattr(speech, "microphone_device", "") or "",
            gain=getattr(speech, "input_gain", 1.0) or 1.0,
            max_seconds=getattr(speech, "max_record_seconds", 30) or 30,
        )
        self.recorder.start()
        self.started_at = time.monotonic()
        _set_status(self.parent, "Listening… release the key to send")

    def finish(self) -> str:
        """Stop recording, transcribe, and return the text ('' if nothing heard)."""
        if self.recorder is None:
            return ""
        path = self.recorder.stop()
        self.recorder = None
        _set_status(self.parent, "Transcribing…")
        if path is None:
            _set_status(self.parent, "I did not hear anything")
            return ""
        try:
            text = transcribe(path, settings=self.settings)
        finally:
            if not getattr(getattr(self.settings, "speech", None), "retain_audio", False):
                path.unlink(missing_ok=True)
        _set_status(self.parent, text or "I did not catch that")
        return text


def transcribe(path: Path, *, settings: Any = None, language: str = "auto") -> str:
    """Transcribe a WAV file locally with faster-whisper."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Speech input needs faster-whisper: pip install faster-whisper"
        ) from exc

    speech = getattr(settings, "speech", None)
    model_name = getattr(speech, "stt_model", "base") or "base"
    device = getattr(speech, "stt_device", "auto") or "auto"
    compute = getattr(speech, "stt_compute_type", "auto") or "auto"
    if device == "auto":
        device = "cuda" if _cuda_available() else "cpu"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"

    model = WhisperModel(model_name, device=device, compute_type=compute)
    lang = None if language in ("", "auto") else language
    segments, _info = model.transcribe(str(path), language=lang, vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments).strip()


def _cuda_available() -> bool:
    try:
        import ctranslate2  # type: ignore

        return int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:  # noqa: BLE001
        return False


def _write_wav(path: Path, audio: Any, sample_rate: int) -> None:
    import wave

    import numpy  # type: ignore

    clipped = numpy.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def _settings_from(parent: Any) -> Any:
    services = getattr(parent, "services", None)
    store = getattr(services, "settings_store", None)
    return getattr(store, "settings", None)


def _set_status(parent: Any, text: str) -> None:
    if parent is None:
        return
    page = getattr(parent, "chat_page", None)
    label = getattr(page, "context_label", None)
    if label is not None:
        try:
            label.setText(text)
        except Exception:  # noqa: BLE001 - cosmetic
            pass
