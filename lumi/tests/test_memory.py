"""Persistent memory: storage, retrieval, editing, deletion, privacy guards."""

from __future__ import annotations

import json

import pytest

from lumi.memory.models import KIND_FACT, KIND_PREFERENCE, KIND_PROJECT, KIND_SESSION
from lumi.memory.service import ConversationStore, MemoryService, looks_sensitive
from lumi.memory.store import Database


def test_add_and_retrieve(memory: MemoryService) -> None:
    entry = memory.add("Prefers short answers", kind=KIND_PREFERENCE, key="answer_style")
    assert entry.id is not None
    fetched = memory.get(int(entry.id))
    assert fetched is not None and fetched.content == "Prefers short answers"
    assert memory.find_by_key("answer_style") is not None


def test_empty_and_oversized_content_is_rejected(memory: MemoryService) -> None:
    with pytest.raises(ValueError):
        memory.add("   ")
    with pytest.raises(ValueError):
        memory.add("x" * 3000)
    with pytest.raises(ValueError):
        memory.add("valid text", kind="nonsense")


def test_secrets_are_refused(memory: MemoryService) -> None:
    with pytest.raises(ValueError) as info:
        memory.add("my password is hunter2")
    assert "credential" in str(info.value).lower() or "password" in str(info.value).lower()
    with pytest.raises(ValueError):
        memory.add("api_key = sk-abcdefghijklmnopqrstuvwx")
    with pytest.raises(ValueError):
        memory.add("card 4111111111111111")
    assert looks_sensitive("pin: 4321") is not None
    assert looks_sensitive("likes filter coffee") is None


def test_update_and_pin(memory: MemoryService) -> None:
    entry = memory.add("Studies ECE", kind=KIND_FACT)
    updated = memory.update(int(entry.id), content="Studies electronics engineering", pinned=True)
    assert updated is not None
    assert updated.content.startswith("Studies electronics")
    assert updated.pinned is True
    with pytest.raises(ValueError):
        memory.update(int(entry.id), content="token: abc123456")


def test_delete_and_clear(memory: MemoryService) -> None:
    first = memory.add("One")
    memory.add("Two")
    assert memory.delete(int(first.id)) is True
    assert memory.total() == 1
    memory.clear()
    assert memory.total() == 0
    assert memory.list() == []


def test_search_uses_text_match(memory: MemoryService) -> None:
    memory.add("Uses a Lenovo LOQ laptop", kind=KIND_FACT)
    memory.add("Prefers dark themes", kind=KIND_PREFERENCE)
    results = memory.search("lenovo")
    assert len(results) == 1
    assert "LOQ" in results[0].content
    assert memory.search("nothing like this") == []


def test_session_context_is_not_persisted(memory: MemoryService, db: Database) -> None:
    memory.set_session_context("current_topic", "revising Thevenin")
    assert memory.session_context()[0].content == "revising Thevenin"
    assert db.table_count("memories") == 0
    assert any(entry.kind == KIND_SESSION for entry in memory.list())


def test_prompt_selection_prefers_pinned_and_relevant(memory: MemoryService) -> None:
    memory.add("Always on fact", kind=KIND_FACT, pinned=True)
    memory.add("Physics project context", kind=KIND_PROJECT)
    memory.add("Prefers bullet points", kind=KIND_PREFERENCE)
    selected = memory.relevant_for_prompt("physics project")
    contents = [entry.content for entry in selected]
    assert "Always on fact" in contents
    assert any("Physics" in content for content in contents)


def test_touch_increments_use_count(memory: MemoryService) -> None:
    entry = memory.add("Counted")
    memory.touch(int(entry.id))
    refreshed = memory.get(int(entry.id))
    assert refreshed is not None and refreshed.use_count == 1 and refreshed.last_used_at is not None


def test_export_and_import_round_trip(memory: MemoryService) -> None:
    memory.add("Prefers Python", kind=KIND_PREFERENCE, key="language")
    memory.add("Electronics student", kind=KIND_FACT, key="course")
    payload = memory.export_payload()
    assert payload["type"] == "lumi.memory.export"
    assert len(payload["memories"]) == 2

    memory.clear()
    assert memory.total() == 0
    stats = memory.import_payload(json.loads(json.dumps(payload)))
    assert stats["added"] == 2
    assert memory.total() == 2

    # Importing again updates keyed entries instead of duplicating them.
    stats = memory.import_payload(json.loads(json.dumps(payload)))
    assert stats["updated"] == 2
    assert memory.total() == 2

    # "replace" starts from an empty database.
    stats = memory.import_payload(json.loads(json.dumps(payload)), replace=True)
    assert stats["added"] == 2
    assert memory.total() == 2


def test_import_refuses_sensitive_entries(memory: MemoryService) -> None:
    payload = {"memories": [{"content": "password: swordfish", "kind": "fact"}, {"content": "fine", "kind": "fact"}]}
    stats = memory.import_payload(payload)
    assert stats["added"] == 1
    assert stats["skipped"] == 1


def test_prune_keeps_pinned(memory: MemoryService, db) -> None:
    entry = memory.add("Old pinned", pinned=True)
    memory.add("Old unpinned")
    memory.add("Recently used")  # use_count > 0 protects it from pruning

    db.execute_write("UPDATE memories SET updated_at = '2020-01-01T00:00:00'")
    db.execute_write("UPDATE memories SET use_count = 1 WHERE content = 'Recently used'")

    removed = memory.prune(stale_days=365, keep_pinned=True)
    assert removed == 1
    remaining = {item.content for item in memory.list()}
    assert remaining == {"Old pinned", "Recently used"}


def test_counts_by_kind(memory: MemoryService) -> None:
    memory.add("A", kind=KIND_FACT)
    memory.add("B", kind=KIND_PREFERENCE)
    memory.set_session_context("c", "session only")
    counts = memory.counts_by_kind()
    assert counts[KIND_FACT] == 1
    assert counts[KIND_PREFERENCE] == 1
    assert counts[KIND_SESSION] == 1


# ---------------------------------------------------------------------------
# conversation history
# ---------------------------------------------------------------------------


def test_conversation_history_round_trip(conversations: ConversationStore) -> None:
    conversation = conversations.start(title="", model="llama3.2:3b")
    conversations.add_message(int(conversation.id), "user", "How do I bias a transistor?")
    conversations.add_message(int(conversation.id), "assistant", "Start with the base-emitter junction...")
    messages = conversations.messages(int(conversation.id))
    assert [message.role for message in messages] == ["user", "assistant"]
    assert conversations.message_count() == 2

    reloaded = conversations.get(int(conversation.id))
    assert reloaded is not None
    assert reloaded.title.startswith("How do I bias")


def test_conversation_search(conversations: ConversationStore) -> None:
    conversation = conversations.start(source="test")
    conversations.add_message(int(conversation.id), "user", "Explain Thevenin's theorem")
    hits = conversations.search_messages("thevenin")
    assert hits and "Thevenin" in hits[0].content


def test_retention_prunes_old_conversations(conversations: ConversationStore, db: Database) -> None:
    conversation = conversations.start()
    conversations.add_message(int(conversation.id), "user", "old message")
    db.execute_write(
        "UPDATE conversations SET updated_at = '2020-01-01T00:00:00' WHERE id = ?", (int(conversation.id),)
    )
    removed = conversations.prune(retention_days=30)
    assert removed == 1
    assert conversations.message_count() == 0


def test_retention_zero_keeps_everything(conversations: ConversationStore) -> None:
    conversation = conversations.start()
    conversations.add_message(int(conversation.id), "user", "keep me")
    assert conversations.prune(retention_days=0) == 0
    assert conversations.message_count() == 1


def test_delete_conversation_removes_messages(conversations: ConversationStore) -> None:
    conversation = conversations.start()
    conversations.add_message(int(conversation.id), "user", "hello")
    conversations.delete(int(conversation.id))
    assert conversations.list() == []
    assert conversations.message_count() == 0
