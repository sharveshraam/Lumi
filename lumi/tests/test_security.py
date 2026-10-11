"""The permission policy: what LUMI refuses, and why."""

from __future__ import annotations

from pathlib import Path

import pytest

from lumi.security.policy import PermissionPolicy, SensitiveTarget
from lumi.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from lumi.tools.registry import ToolRegistry


class _FakeTool(Tool):
    name = "fake_tool"
    description = "test tool"
    parameters = {"type": "object", "properties": {}}
    category = "test"

    def run(self, arguments: dict, context: ToolContext) -> ToolResult:  # pragma: no cover - never executed
        return ToolResult.success("ok")


def tool(risk: RiskLevel = RiskLevel.SAFE, *, permission: str = "", action: str = "", confirm: bool = False) -> _FakeTool:
    instance = _FakeTool()
    instance.risk = risk
    instance.requires_permission = permission
    instance.permission_action = action
    instance.always_confirm = confirm
    return instance


# ---------------------------------------------------------------------------
# capability switches
# ---------------------------------------------------------------------------
def test_safe_tool_is_allowed(policy: PermissionPolicy) -> None:
    decision = policy.evaluate(tool(RiskLevel.SAFE))
    assert decision.allowed is True
    assert decision.needs_confirmation is False


def test_missing_permission_is_denied(policy: PermissionPolicy) -> None:
    decision = policy.evaluate(tool(RiskLevel.MODERATE, permission="allow_screenshots"))
    assert decision.allowed is False
    assert "allow_screenshots" in decision.reason


def test_unknown_permission_field_is_denied(policy: PermissionPolicy) -> None:
    decision = policy.evaluate(tool(RiskLevel.MODERATE, permission="allow_teleportation"))
    assert decision.allowed is False, "an unknown switch must fail closed, not open"


def test_automation_off_denies_everything_that_is_not_safe(policy: PermissionPolicy) -> None:
    policy.settings.automation.enabled = False
    assert policy.evaluate(tool(RiskLevel.SAFE)).allowed is True
    assert policy.evaluate(tool(RiskLevel.MODERATE)).allowed is False
    assert policy.evaluate(tool(RiskLevel.CONSEQUENTIAL)).allowed is False


def test_consequential_tools_always_ask(policy: PermissionPolicy) -> None:
    decision = policy.evaluate(tool(RiskLevel.CONSEQUENTIAL))
    assert decision.allowed is True
    assert decision.needs_confirmation is True


def test_permission_action_forces_a_question(policy: PermissionPolicy) -> None:
    decision = policy.evaluate(tool(RiskLevel.MODERATE, action="send_email"))
    assert decision.needs_confirmation is True


def test_session_grant_skips_the_question_once_given(policy: PermissionPolicy) -> None:
    policy.grant_for_session("fake_tool")
    decision = policy.evaluate(tool(RiskLevel.CONSEQUENTIAL))
    assert decision.allowed is True
    assert decision.needs_confirmation is False
    assert decision.used_session_grant is True

    policy.revoke_grants()
    assert policy.evaluate(tool(RiskLevel.CONSEQUENTIAL)).needs_confirmation is True


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def test_paths_outside_the_allowed_roots_are_read_only(policy: PermissionPolicy, tmp_path: Path) -> None:
    """Reading is allowed so the model can be asked about a file the user names,
    but writing is confined to the workspace folders - and only when enabled."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy.settings.automation.workspace_folders = [str(workspace)]
    policy.settings.automation.allow_file_writes = True

    outside = tmp_path / "elsewhere" / "notes.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("hello", encoding="utf-8")

    assert policy.check_path(outside, write=False).allowed is True
    decision = policy.check_path(outside, write=True)
    assert decision.allowed is False
    assert "workspace" in decision.reason.lower()
    assert policy.check_path(workspace / "inside.txt", write=True).allowed is True


def test_writing_inside_the_workspace_is_allowed_when_enabled(policy: PermissionPolicy, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy.settings.automation.workspace_folders = [str(workspace)]
    policy.settings.automation.allow_file_writes = True
    target = workspace / "lab.txt"
    assert policy.check_path(target, write=True).allowed is True


def test_writes_are_refused_when_the_switch_is_off(policy: PermissionPolicy, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy.settings.automation.workspace_folders = [str(workspace)]
    policy.settings.automation.allow_file_writes = False
    decision = policy.check_path(workspace / "lab.txt", write=True)
    assert decision.allowed is False
    assert "write" in decision.reason.lower()


@pytest.mark.parametrize(
    "path",
    [
        "/home/user/.ssh/id_rsa",
        "/home/user/.aws/credentials",
        "/etc/passwd",
        "/usr/bin/python3",
    ],
)
def test_sensitive_locations_are_never_opened(policy: PermissionPolicy, path: str) -> None:
    assert policy.check_path(path).allowed is False


def test_credential_files_are_refused_by_name(policy: PermissionPolicy, tmp_path: Path) -> None:
    target = tmp_path / ".git-credentials"
    target.write_text("https://user:token@example.com", encoding="utf-8")
    assert policy.check_path(target).allowed is False


def test_blocked_folder_overrides_the_workspace(policy: PermissionPolicy, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    secret = workspace / "private"
    secret.mkdir(parents=True)
    policy.settings.automation.workspace_folders = [str(workspace)]
    policy.settings.automation.allow_file_writes = True
    policy.settings.automation.blocklist_folders = [str(secret)]
    assert policy.check_path(secret / "diary.txt", write=True).allowed is False


def test_the_filesystem_root_is_never_writable(policy: PermissionPolicy) -> None:
    policy.settings.automation.allow_file_writes = True
    assert policy.check_path("/", write=True).allowed is False


def test_sensitive_target_collects_reasons() -> None:
    target = SensitiveTarget(blocked=True, reasons=["first"])
    target.add("second")
    assert target.reasons == ["first", "second"]
    assert target.blocked is True


# ---------------------------------------------------------------------------
# the tools really are wired to those switches
# ---------------------------------------------------------------------------
def test_registry_tools_requiring_permissions_are_gated(registry: ToolRegistry, policy: PermissionPolicy) -> None:
    gated = [t for t in registry.all() if t.requires_permission]
    assert gated, "expected several tools to be permission-gated"
    for entry in gated:
        decision = policy.evaluate(entry)
        assert decision.allowed is False, f"{entry.name} should be off by default"


def test_gated_tool_names_are_exactly_the_expected_set(registry: ToolRegistry) -> None:
    """A new tool must not quietly join this list without a decision being made."""
    gated = {t.requires_permission for t in registry.all() if t.requires_permission}
    assert gated <= {
        "allow_app_launch",
        "allow_keyboard_mouse",
        "allow_screenshots",
        "allow_browser_automation",
        "allow_file_writes",
        "allow_bulk_file_operations",
    }
