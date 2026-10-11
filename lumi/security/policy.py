"""Permission policy for tool execution and file access.

This module is the single place where "may LUMI do this?" is decided.  It is
plain, deterministic logic - no model involvement - so the answer is the same
whether the request came from the user's keyboard or from a model proposal.

Rules, in order:

1. If the user switched automation off, only read-only tools run.
2. A tool that needs a capability the user disabled in Settings is refused.
3. Path-based tools may only touch allowed roots; a blocklist of credential,
   browser-profile and system locations is refused outright.
4. Consequential tools always require explicit confirmation immediately before
   execution (a *session grant* the user gave for that exact tool is honoured).
5. ``require_confirmation_for`` in Settings lists extra actions that must ask.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from lumi.tools.base import RiskLevel, Tool

log = logging.getLogger("lumi.security.policy")


@dataclass
class PermissionDecision:
    """Outcome of a policy check for one proposed tool call."""

    allowed: bool
    needs_confirmation: bool
    reason: str
    risk: RiskLevel = RiskLevel.SAFE
    used_session_grant: bool = False

    @property
    def verdict(self) -> str:
        if not self.allowed:
            return "denied"
        return "confirmation-required" if self.needs_confirmation else "allowed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "needs_confirmation": self.needs_confirmation,
            "reason": self.reason,
            "risk": self.risk.value,
            "verdict": self.verdict,
        }


# ---------------------------------------------------------------------------
# path rules
# ---------------------------------------------------------------------------

#: Path fragments that are never readable or writable by a tool.  These are
#: credential stores, browser profiles and OS internals - nothing LUMI needs in
#: order to help the user.
BLOCKED_PATH_FRAGMENTS: tuple[str, ...] = (
    "appdata/local/google/chrome/user data",
    "appdata\\local\\google\\chrome\\user data",
    "appdata/roaming/mozilla/firefox/profiles",
    "appdata\\roaming\\mozilla\\firefox\\profiles",
    "appdata/roaming/microsoft/credentials",
    "appdata/local/microsoft/credentials",
    "appdata/roaming/microsoft/crypto",
    "appdata/local/microsoft/edge/user data",
    "/.ssh/",
    "/.aws/",
    "/.azure/",
    "/.config/gcloud/",
    "/.config/gh/",
    "/.gnupg/",
    "/.kube/",
    "/library/keychains/",
    "/.mozilla/firefox/",
    "/.config/google-chrome/",
    "/.config/chromium/",
    "id_rsa",
    "id_ed25519",
    ".netrc",
    ".git-credentials",
    "cookies.sqlite",
    "login data",
    "key4.db",
    "logins.json",
    "signons.sqlite",
    "ntuser.dat",
    "/etc/shadow",
    "/etc/sudoers",
    "secrets.json",
    "lumi.db",
)

#: Sensitive file *names* (matched case-insensitively against the final component).
BLOCKED_FILE_NAMES: frozenset[str] = frozenset(
    {
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".netrc",
        ".git-credentials",
        "credentials.json",
        "secrets.json",
        "login data",
        "key4.db",
        "logins.json",
        "cookies.sqlite",
        "ntuser.dat",
        "sam",
        "system",
    }
)

WINDOWS_SYSTEM_ROOTS: tuple[str, ...] = (
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
    "/etc",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/system",
    "/library/system",
)

_HOME = Path.home()


@dataclass
class PathDecision:
    allowed: bool
    reason: str = ""
    requires_write_permission: bool = False

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


class PermissionPolicy:
    """Evaluates tool permissions against the user's settings."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._session_grants: set[str] = set()

    # ------------------------------------------------------------ grants
    def grant_for_session(self, tool_name: str) -> None:
        """Remember that the user approved exactly this tool for this run."""
        self._session_grants.add(tool_name)
        log.info("session grant recorded for tool %s", tool_name)

    def revoke_grants(self) -> None:
        self._session_grants.clear()

    def has_session_grant(self, tool_name: str) -> bool:
        return tool_name in self._session_grants

    # --------------------------------------------------------- evaluation
    def evaluate(self, tool: Tool, arguments: dict[str, Any] | None = None) -> PermissionDecision:
        automation = getattr(self.settings, "automation", None)
        risk = tool.risk

        if automation is not None and not getattr(automation, "enabled", True):
            if risk is not RiskLevel.SAFE:
                return PermissionDecision(
                    allowed=False,
                    needs_confirmation=False,
                    reason="Automation is switched off in Settings → Permissions.",
                    risk=risk,
                )

        if tool.requires_permission:
            if not self._capability_enabled(tool.requires_permission):
                return PermissionDecision(
                    allowed=False,
                    needs_confirmation=False,
                    reason=(
                        f"'{tool.name}' needs the '{tool.requires_permission}' permission, "
                        "which is switched off in Settings → Permissions."
                    ),
                    risk=risk,
                )

        # "Bulk" means touching more than one item in a single call.  The user
        # can allow many-at-once work, but until they do, one file at a time is
        # the most LUMI will touch - even with a confirmation dialog.
        touches_many = _bulk_count(arguments) > 1
        always_bulk = getattr(tool, "permission_action", "") == "bulk_file_operation"
        wants_bulk = always_bulk or (getattr(tool, "bulk_capable", False) and touches_many)
        if wants_bulk and not self._capability_enabled("allow_bulk_file_operations"):
            if always_bulk and not touches_many:
                detail = f"'{tool.name}' works on a whole folder at once."
            else:
                detail = f"'{tool.name}' would change {_bulk_count(arguments)} items at once."
            return PermissionDecision(
                allowed=False,
                needs_confirmation=False,
                reason=f"{detail} Bulk file operations are switched off in Settings → Permissions.",
                risk=risk,
            )

        extra_confirmation = False
        action = getattr(tool, "permission_action", None)
        if action and automation is not None:
            listed = {str(item).lower() for item in getattr(automation, "require_confirmation_for", []) or []}
            if action.lower() in listed:
                extra_confirmation = True

        if risk is RiskLevel.CONSEQUENTIAL or tool.always_confirm or extra_confirmation:
            if self.has_session_grant(tool.name):
                return PermissionDecision(
                    allowed=True,
                    needs_confirmation=False,
                    reason="You approved this action for the rest of the session.",
                    risk=risk,
                    used_session_grant=True,
                )
            return PermissionDecision(
                allowed=True,
                needs_confirmation=True,
                reason="This action needs your approval before it runs.",
                risk=risk,
            )

        return PermissionDecision(allowed=True, needs_confirmation=False, reason="Allowed by your settings.", risk=risk)

    def _capability_enabled(self, field_name: str) -> bool:
        automation = getattr(self.settings, "automation", None)
        if automation is None:
            return True
        if not hasattr(automation, field_name):
            log.warning("unknown permission field requested: %s", field_name)
            return False
        return bool(getattr(automation, field_name))

    # --------------------------------------------------------- path checks
    def allowed_roots(self) -> list[Path]:
        automation = getattr(self.settings, "automation", None)
        folders: list[str] = []
        if automation is not None:
            folders.extend(getattr(automation, "workspace_folders", []) or [])
        roots = [Path(folder).expanduser() for folder in folders if folder]
        if not roots:
            from lumi.config.paths import AppPaths

            roots = [AppPaths().default_workspace(), Path.home() / "Documents"]
        resolved: list[Path] = []
        for root in roots:
            try:
                resolved.append(root.resolve())
            except OSError:  # pragma: no cover
                continue
        return resolved

    def search_roots(self) -> list[Path]:
        automation = getattr(self.settings, "automation", None)
        folders = list(getattr(automation, "search_folders", []) or []) if automation else []
        roots = [Path(folder).expanduser() for folder in folders if folder]
        return roots or [Path.home()]

    def blocked_roots(self) -> list[Path]:
        automation = getattr(self.settings, "automation", None)
        folders = list(getattr(automation, "blocklist_folders", []) or []) if automation else []
        return [Path(folder).expanduser() for folder in folders if folder]

    def check_path(self, raw_path: str | os.PathLike[str], *, write: bool = False) -> PathDecision:
        """Decide whether a path may be read (or written) at all."""
        if raw_path is None or str(raw_path).strip() == "":
            return PathDecision(False, "No path was given.")
        try:
            path = Path(str(raw_path)).expanduser()
        except (TypeError, ValueError):
            return PathDecision(False, "That path could not be understood.")

        text = str(path).lower().replace("\\", "/")
        for fragment in BLOCKED_PATH_FRAGMENTS:
            if fragment.replace("\\", "/") in text:
                return PathDecision(False, "That location holds credentials or browser data, which LUMI never opens.")

        name = path.name.lower()
        if name in BLOCKED_FILE_NAMES:
            return PathDecision(False, f"'{path.name}' looks like a credential file, so LUMI will not open it.")

        for blocked in self.blocked_roots() + [Path(root) for root in WINDOWS_SYSTEM_ROOTS]:
            if _is_within(path, blocked):
                return PathDecision(False, "That folder is outside the areas LUMI is allowed to use.")

        if write:
            if not self._capability_enabled("allow_file_writes"):
                return PathDecision(
                    False,
                    "Writing files is switched off. Turn on 'Let LUMI write files' in Settings → Permissions.",
                    requires_write_permission=True,
                )
            if not self._within_any(path, self.allowed_roots()):
                allowed = ", ".join(str(root) for root in self.allowed_roots()[:3])
                return PathDecision(
                    False,
                    f"LUMI only writes inside its workspace folders ({allowed}).",
                    requires_write_permission=False,
                )
            return PathDecision(True)

        if not self._capability_enabled("allow_file_operations"):
            return PathDecision(False, "File access is switched off in Settings → Permissions.")

        # Reads are allowed anywhere except the blocked locations above, but the
        # tool layer marks out-of-root reads so the UI can show where data came from.
        return PathDecision(True)

    def _within_any(self, path: Path, roots: Sequence[Path]) -> bool:
        return any(_is_within(path, root) for root in roots)

    def describe_roots(self) -> str:
        return ", ".join(str(root) for root in self.allowed_roots())


def _is_within(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except OSError:  # pragma: no cover - unreadable paths
        resolved_path, resolved_root = path, root
    try:
        resolved_path.relative_to(resolved_root)
        return True
    except ValueError:
        return False


_SHELL_METACHARACTERS = re.compile(r"[;&|`$><\n\r\t]|(\$\()|(\.\.\\)")


def looks_like_shell_injection(text: str) -> bool:
    """True when text contains shell metacharacters we never want in arguments."""
    return bool(_SHELL_METACHARACTERS.search(text or ""))


def sanitise_search_term(term: str, *, max_length: int = 120) -> str:
    """Clean a user/model supplied search term used in file globs and SQL LIKE."""
    cleaned = re.sub(r"[*?\[\]%_\\/]", " ", term or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_length]


def summarise_decision(decision: PermissionDecision) -> str:
    return f"{decision.verdict}: {decision.reason}"


def merge_decisions(decisions: Iterable[PermissionDecision]) -> PermissionDecision:
    """Combine several checks (all must pass; any confirmation request wins)."""
    items = list(decisions)
    if not items:
        return PermissionDecision(True, False, "No restrictions apply.")
    for item in items:
        if not item.allowed:
            return item
    needs_confirmation = any(item.needs_confirmation for item in items)
    reason = next((item.reason for item in items if item.needs_confirmation), items[0].reason)
    return PermissionDecision(True, needs_confirmation, reason, risk=max(items, key=lambda item: item.risk.value).risk)


def _bulk_count(arguments: dict[str, Any] | None) -> int:
    """How many distinct items would one call touch?

    Only list-shaped arguments count: a single path in a list is still one item.
    """
    if not isinstance(arguments, dict):
        return 1
    largest = 0
    for value in arguments.values():
        if isinstance(value, (list, tuple, set)):
            largest = max(largest, len(value))
        elif isinstance(value, str) and ";" in value:
            largest = max(largest, len([part for part in value.split(";") if part.strip()]))
    return largest or 1


@dataclass
class SensitiveTarget:
    """Result of scanning a proposed action for sensitive targets."""

    blocked: bool
    reasons: list[str] = field(default_factory=list)

    def add(self, reason: str) -> None:
        self.blocked = True
        self.reasons.append(reason)
