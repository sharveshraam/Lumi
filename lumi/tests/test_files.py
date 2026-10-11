"""File management: search, read, plan/apply writes, duplicates, undo-by-review."""

from __future__ import annotations

from pathlib import Path

import pytest

from lumi.automation.files import FileManager
from lumi.security.policy import PermissionPolicy


@pytest.fixture()
def manager(policy: PermissionPolicy, workspace: Path) -> FileManager:
    policy.settings.automation.workspace_folders = [str(workspace)]
    policy.settings.automation.search_folders = [str(workspace)]
    policy.settings.automation.allow_file_writes = True
    policy.settings.automation.allow_bulk_file_operations = True
    return FileManager(policy)


def make(workspace: Path, name: str, text: str = "hello", *, sub: str = "") -> Path:
    folder = workspace / sub if sub else workspace
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# reading and searching
# ---------------------------------------------------------------------------
def test_find_files_by_pattern(manager: FileManager, workspace: Path) -> None:
    make(workspace, "lab_record.txt")
    make(workspace, "notes.md")
    hits = manager.find_files(name_pattern="lab")
    assert [hit.path.name for hit in hits] == ["lab_record.txt"]


def test_find_files_by_extension(manager: FileManager, workspace: Path) -> None:
    make(workspace, "a.txt")
    make(workspace, "b.pdf")
    hits = manager.find_files(extension=".pdf")
    assert [hit.path.name for hit in hits] == ["b.pdf"]


def test_find_files_never_leaves_the_search_roots(manager: FileManager, workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret" / "important.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("do not read me", encoding="utf-8")
    hits = manager.find_files(name_pattern="important")
    assert hits == []


def test_search_inside_files(manager: FileManager, workspace: Path) -> None:
    make(workspace, "chapter4.txt", "Thevenin's theorem states that any linear circuit...")
    make(workspace, "chapter5.txt", "Norton's theorem is the dual.")
    hits = manager.search_in_files("theorem")
    assert {hit.path.name for hit in hits} == {"chapter4.txt", "chapter5.txt"}
    assert "theorem" in hits[0].line.lower()


def test_read_text_refuses_a_file_that_is_too_large(manager: FileManager, workspace: Path) -> None:
    huge = workspace / "huge.txt"
    huge.write_text("x" * (2 * 1024 * 1024 + 10), encoding="utf-8")
    with pytest.raises(ValueError) as info:
        manager.read_text(huge, max_bytes=1024)
    assert "too large" in str(info.value).lower()


def test_read_text_of_a_missing_file_reports_that_it_is_missing(manager: FileManager, workspace: Path) -> None:
    with pytest.raises(FileNotFoundError):
        manager.read_text(workspace / "nope.txt")


def test_read_text_never_touches_a_credential_file(manager: FileManager, workspace: Path) -> None:
    target = workspace / ".git-credentials"
    target.write_text("https://user:token@example.com", encoding="utf-8")
    with pytest.raises(PermissionError):
        manager.read_text(target)


# ---------------------------------------------------------------------------
# plans: nothing happens until the plan is applied
# ---------------------------------------------------------------------------
def test_write_plan_reports_a_new_file(manager: FileManager, workspace: Path) -> None:
    plan = manager.plan_write(workspace / "notes.txt")
    assert plan.is_empty is False
    assert plan.destructive is False
    assert "create" in plan.preview().lower()


def test_write_plan_refuses_to_overwrite_unless_asked(manager: FileManager, workspace: Path) -> None:
    make(workspace, "notes.txt", "old")
    plan = manager.plan_write(workspace / "notes.txt")
    assert plan.blocked, "an existing file must not be replaced silently"
    assert "already exists" in plan.blocked[0]

    explicit = manager.plan_write(workspace / "notes.txt", overwrite=True)
    assert explicit.destructive is True
    assert [change.kind for change in explicit.changes] == ["overwrite"]


def test_write_plan_refuses_outside_the_workspace(manager: FileManager, tmp_path: Path) -> None:
    plan = manager.plan_write(tmp_path / "elsewhere" / "notes.txt")
    assert plan.blocked
    assert "workspace" in plan.blocked[0].lower()


def test_write_text_creates_the_file(manager: FileManager, workspace: Path) -> None:
    target = workspace / "summary.txt"
    result = manager.write_text(target, "line one\nline two")
    assert result["path"] == str(target)
    assert target.read_text(encoding="utf-8") == "line one\nline two"


def test_write_text_refuses_a_silent_overwrite(manager: FileManager, workspace: Path) -> None:
    target = make(workspace, "notes.txt", "important")
    with pytest.raises(PermissionError):
        manager.write_text(target, "replacement")
    assert target.read_text(encoding="utf-8") == "important"


def test_write_text_overwrites_when_explicitly_allowed(manager: FileManager, workspace: Path) -> None:
    target = make(workspace, "notes.txt", "old")
    manager.write_text(target, "new", overwrite=True)
    assert target.read_text(encoding="utf-8") == "new"


def test_append_text_keeps_the_old_content(manager: FileManager, workspace: Path) -> None:
    target = make(workspace, "log.txt", "first\n")
    manager.append_text(target, "second")
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"


# ---------------------------------------------------------------------------
# delete plans need the bulk switch and always preview
# ---------------------------------------------------------------------------
def test_delete_plan_previews_without_deleting(manager: FileManager, workspace: Path) -> None:
    first = make(workspace, "a.txt")
    second = make(workspace, "b.txt")
    plan = manager.plan_delete([first, second])
    assert first.exists() and second.exists(), "planning must not touch the disk"
    assert plan.destructive is True
    assert "delete" in plan.preview().lower()
    assert len(plan.changes) == 2


def test_delete_plan_is_refused_when_bulk_operations_are_off(manager: FileManager, workspace: Path) -> None:
    """The manager plans; the *policy* is what enforces the bulk switch."""
    manager.policy.settings.automation.allow_bulk_file_operations = False
    first = make(workspace, "a.txt")
    second = make(workspace, "b.txt")
    plan = manager.plan_delete([first, second])
    assert len(plan.changes) == 2  # a preview is still allowed

    from lumi.tools.registry import ToolRegistry

    tool = ToolRegistry.build_default().get("delete_files")
    decision = manager.policy.evaluate(tool, {"paths": [str(first), str(second)]})
    assert decision.allowed is False
    assert "bulk" in decision.reason.lower()

    manager.policy.settings.automation.allow_bulk_file_operations = True
    assert manager.policy.evaluate(tool, {"paths": [str(first), str(second)]}).allowed is True


def test_apply_delete_removes_the_files(manager: FileManager, workspace: Path) -> None:
    first = make(workspace, "a.txt")
    plan = manager.plan_delete([first])
    results = manager.apply_delete(plan, use_recycle_bin=False)
    assert results and not first.exists()


def test_delete_of_a_protected_file_is_blocked(manager: FileManager, workspace: Path) -> None:
    protected = workspace / ".git-credentials"
    protected.write_text("x", encoding="utf-8")
    plan = manager.plan_delete([protected])
    assert plan.changes == []


# ---------------------------------------------------------------------------
# move / rename / organise
# ---------------------------------------------------------------------------
def test_move_plan_and_apply(manager: FileManager, workspace: Path) -> None:
    source = make(workspace, "photo.png")
    target = workspace / "images"
    target.mkdir()
    plan = manager.plan_move([source], target)
    assert plan.changes
    manager.apply_move(plan)
    assert (target / "photo.png").exists()
    assert not source.exists()


def test_rename_plan_and_apply(manager: FileManager, workspace: Path) -> None:
    source = make(workspace, "draft.txt")
    plan = manager.plan_rename(source, "final.txt")
    manager.apply_rename(plan)
    assert (workspace / "final.txt").exists()
    assert not source.exists()


def test_organise_sorts_by_type(manager: FileManager, workspace: Path) -> None:
    make(workspace, "a.pdf")
    make(workspace, "b.jpg")
    make(workspace, "c.pdf")
    plan = manager.plan_organise(workspace)
    assert len(plan.changes) == 3
    manager.apply_move(plan)
    assert (workspace / "Documents" / "a.pdf").exists() or (workspace / "PDFs" / "a.pdf").exists()


# ---------------------------------------------------------------------------
# duplicates
# ---------------------------------------------------------------------------
def test_find_duplicates_reports_identical_files(manager: FileManager, workspace: Path) -> None:
    payload = b"identical payload for hashing" * 400
    (workspace / "one.bin").write_bytes(payload)
    (workspace / "copy.bin").write_bytes(payload)
    (workspace / "different.bin").write_bytes(b"something else entirely" * 200)

    report = manager.find_duplicates(str(workspace), min_size_kb=1)
    assert report["groups"], report
    group = report["groups"][0]
    assert group["count"] == 2
    # "duplicate_files" counts the *extra* copies, so a pair counts once.
    assert report["duplicate_files"] == 1
    assert report["wasted_bytes"] > 0
    # Everything is still on disk: finding duplicates never deletes.
    assert (workspace / "one.bin").exists() and (workspace / "copy.bin").exists()


def test_find_duplicates_ignores_small_files_by_default(manager: FileManager, workspace: Path) -> None:
    (workspace / "tiny1.txt").write_text("x", encoding="utf-8")
    (workspace / "tiny2.txt").write_text("x", encoding="utf-8")
    report = manager.find_duplicates(str(workspace))
    assert report["groups"] == []


def test_find_duplicates_minimum_size_comes_from_the_tool_schema(registry) -> None:
    """The tool schema - not the manager - is where min_size_kb is bounded."""
    schema = registry.get("find_duplicates").parameters["properties"]["min_size_kb"]
    assert schema["minimum"] == 1


def test_duplicate_scan_never_deletes_and_skips_blocked_locations(manager: FileManager, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    payload = b"x" * 20000
    (outside / "a.bin").write_bytes(payload)
    (outside / "b.bin").write_bytes(payload)

    report = manager.find_duplicates(str(outside), min_size_kb=1)
    assert report["duplicate_files"] == 1
    assert (outside / "a.bin").exists() and (outside / "b.bin").exists()

    # A blocked location is never scanned, even when it is full of duplicates.
    blocked = tmp_path / "elsewhere" / ".ssh"
    blocked.mkdir()
    (blocked / "a.bin").write_bytes(payload)
    (blocked / "b.bin").write_bytes(payload)
    blocked_report = manager.find_duplicates(str(blocked), min_size_kb=1)
    assert blocked_report["groups"] == []
