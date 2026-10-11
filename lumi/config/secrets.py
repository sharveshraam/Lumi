"""Credential storage for optional cloud providers.

Priority order:

1. Environment variable named by the provider settings (e.g. ``LUMI_GEMINI_API_KEY``).
   This is the recommended way: nothing sensitive is written to disk.
2. OS keyring (Windows Credential Manager / macOS Keychain / Secret Service) via the
   optional :mod:`keyring` package.
3. A local ``secrets.json`` inside the LUMI home directory with ``0600`` permissions,
   used only if the keyring is unavailable.

Keys are never written to the settings file, never logged, and never included in
support bundles.  See ``docs/SECURITY.md``.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

from lumi.config.paths import AppPaths

log = logging.getLogger("lumi.config.secrets")

KEYRING_SERVICE = "lumi-desktop-assistant"


class SecretStore:
    """Small, dependency-tolerant credential store."""

    def __init__(self, paths: AppPaths, *, use_keyring: bool = True) -> None:
        self.paths = paths
        self.file = paths.home / "secrets.json"
        self._use_keyring = use_keyring
        self._keyring = None
        self._cache: dict[str, str] = {}

    # ----------------------------------------------------------- keyring
    def _keyring_module(self):  # pragma: no cover - depends on host environment
        if self._keyring is None and self._use_keyring:
            try:
                import keyring  # type: ignore

                self._keyring = keyring
            except Exception:
                self._keyring = False
        return self._keyring or None

    # ------------------------------------------------------------ public
    def get(self, name: str, env_var: str = "") -> Optional[str]:
        """Return the secret for ``name`` or ``None``.  Never raises."""
        if env_var:
            value = os.environ.get(env_var)
            if value:
                return value.strip()
        if name in self._cache:
            return self._cache[name]

        module = self._keyring_module()
        if module is not None:
            try:
                value = module.get_password(KEYRING_SERVICE, name)
                if value:
                    self._cache[name] = value
                    return value
            except Exception as exc:  # pragma: no cover - platform specific
                log.debug("keyring lookup failed for %s: %s", name, type(exc).__name__)

        data = self._read_file()
        value = data.get(name)
        if value:
            self._cache[name] = value
            return value
        return None

    def set(self, name: str, value: str) -> str:
        """Store ``value`` and return where it was stored: keyring | file."""
        value = (value or "").strip()
        if not value:
            self.delete(name)
            return "deleted"
        self._cache[name] = value

        module = self._keyring_module()
        if module is not None:
            try:
                module.set_password(KEYRING_SERVICE, name, value)
                self._remove_from_file(name)
                return "keyring"
            except Exception as exc:  # pragma: no cover - platform specific
                log.warning("keyring unavailable (%s); using local secrets file", type(exc).__name__)

        data = self._read_file()
        data[name] = value
        self._write_file(data)
        return "file"

    def delete(self, name: str) -> None:
        self._cache.pop(name, None)
        module = self._keyring_module()
        if module is not None:
            try:
                module.delete_password(KEYRING_SERVICE, name)
            except Exception:  # pragma: no cover - nothing stored
                pass
        self._remove_from_file(name)

    def is_configured(self, name: str, env_var: str = "") -> bool:
        return bool(self.get(name, env_var))

    def managed_names(self) -> list[str]:
        return sorted(self._read_file().keys())

    def storage_backend(self) -> str:
        return "keyring" if self._keyring_module() is not None else "file"

    def clear(self) -> None:
        for name in list(self._read_file().keys()):
            self.delete(name)
        self._cache.clear()

    # ------------------------------------------------------------- file
    def _read_file(self) -> dict[str, str]:
        if not self.file.exists():
            return {}
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read secrets file (%s); ignoring it", type(exc).__name__)
        return {}

    def _write_file(self, data: dict[str, str]) -> None:
        self.paths.ensure()
        payload = json.dumps(data, indent=2, sort_keys=True)
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        _restrict_permissions(tmp)
        os.replace(tmp, self.file)
        _restrict_permissions(self.file)

    def _remove_from_file(self, name: str) -> None:
        data = self._read_file()
        if name in data:
            data.pop(name, None)
            if data:
                self._write_file(data)
            else:
                try:
                    self.file.unlink()
                except OSError:
                    pass


def _restrict_permissions(path: Path) -> None:
    """Best effort owner-only permissions (no-op semantics on Windows ACLs)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - Windows
        pass


def masked(value: Optional[str]) -> str:
    """Render a secret for display: first 3 + last 2 characters."""
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * max(4, len(value) - 6)}{value[-2:]}"
