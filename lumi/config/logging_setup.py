"""Logging configuration.

Rules implemented here (see ``docs/SECURITY.md``):

* Logs stay on the local machine, inside the LUMI home directory.
* Rotating file handler, so the log cannot grow without bound.
* Every record is redacted before it reaches a handler.
* Noisy third-party loggers are quieted by default.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from lumi.config.paths import AppPaths
from lumi.security.redact import redact_text

_CONFIGURED = False
ROOT_LOGGER_NAME = "lumi"

_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "PIL",
    "asyncio",
    "multipart",
    "faster_whisper",
    "numba",
    "urllib3.connectionpool",
)


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs credential-looking substrings from messages."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102 - inherited
        # Both the file and the console handler run this formatter over the same
        # record, so the original message and arguments must be put back exactly
        # as they were - otherwise the second handler prints the raw template
        # ("%s starting") instead of the filled-in text.
        original_msg = record.msg
        original_args = record.args
        try:
            record.msg = redact_text(record.getMessage(), max_length=8000)
            record.args = ()
            return super().format(record)
        finally:
            record.msg = original_msg
            record.args = original_args


def configure_logging(
    paths: AppPaths,
    *,
    level: int | str = logging.INFO,
    console: bool = False,
    quiet_third_party: bool = True,
) -> logging.Logger:
    """Configure the ``lumi`` logger tree.  Idempotent."""
    global _CONFIGURED

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(level)

    if _CONFIGURED:
        return logger

    paths.ensure()
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        Path(paths.log_file),
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    if console and not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in logger.handlers):
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(level)
        logger.addHandler(stream)

    logger.propagate = False

    if quiet_third_party:
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True
    logger.debug("logging configured at %s (file=%s)", logging.getLevelName(level), paths.log_file)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger such as ``lumi.ai.ollama``."""
    if name.startswith(ROOT_LOGGER_NAME) or name == ROOT_LOGGER_NAME:
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def reset_for_tests() -> None:
    """Detach handlers so a test can reconfigure logging for a temp home."""
    global _CONFIGURED
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - defensive
            pass
    _CONFIGURED = False
