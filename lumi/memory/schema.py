"""SQLite schema for LUMI's local storage.

One database file holds every structured thing LUMI knows: conversations, tasks,
reminders, notes, memories, focus sessions, the tool audit trail and cloud usage
counters.  Keeping it in one file makes backup, export and "delete everything"
trivial for the user.

Migrations are append-only: :data:`MIGRATIONS` maps a schema version to a list of
statements.  Never edit an existing migration - add a new one.
"""

from __future__ import annotations

BASE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at  TEXT NOT NULL,
        ended_at    TEXT,
        version     TEXT NOT NULL,
        clean_exit  INTEGER NOT NULL DEFAULT 0,
        platform    TEXT,
        note        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL DEFAULT '',
        source     TEXT NOT NULL DEFAULT 'chat',
        model      TEXT,
        provider   TEXT NOT NULL DEFAULT 'local',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived   INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        role            TEXT NOT NULL,
        content         TEXT NOT NULL DEFAULT '',
        model           TEXT,
        provider        TEXT NOT NULL DEFAULT 'local',
        created_at      TEXT NOT NULL,
        meta_json       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id)",
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        title          TEXT NOT NULL,
        notes          TEXT NOT NULL DEFAULT '',
        category       TEXT NOT NULL DEFAULT 'general',
        priority       TEXT NOT NULL DEFAULT 'medium',
        status         TEXT NOT NULL DEFAULT 'pending',
        due_at         TEXT,
        remind_at      TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        completed_at   TEXT,
        recurrence     TEXT,
        recurrence_end TEXT,
        parent_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        tags           TEXT NOT NULL DEFAULT '',
        source         TEXT NOT NULL DEFAULT 'manual'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_at)",
    """
    CREATE TABLE IF NOT EXISTS reminders (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id         INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
        title           TEXT NOT NULL,
        body            TEXT NOT NULL DEFAULT '',
        fire_at         TEXT NOT NULL,
        recurrence      TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        last_fired_at   TEXT,
        missed_count    INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reminders_fire ON reminders(enabled, fire_at)",
    """
    CREATE TABLE IF NOT EXISTS reminder_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id INTEGER REFERENCES reminders(id) ON DELETE CASCADE,
        fire_at     TEXT NOT NULL,
        shown_at    TEXT,
        was_late    INTEGER NOT NULL DEFAULT 0,
        late_by_s   INTEGER NOT NULL DEFAULT 0,
        delivered   INTEGER NOT NULL DEFAULT 0,
        note        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reminder_log_fire ON reminder_log(fire_at)",
    """
    CREATE TABLE IF NOT EXISTS notes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL DEFAULT '',
        body       TEXT NOT NULL DEFAULT '',
        tags       TEXT NOT NULL DEFAULT '',
        pinned     INTEGER NOT NULL DEFAULT 0,
        source     TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notes_pinned ON notes(pinned, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS memories (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL DEFAULT 'fact',
        content      TEXT NOT NULL,
        key          TEXT,
        source       TEXT NOT NULL DEFAULT 'manual',
        confidence   REAL NOT NULL DEFAULT 1.0,
        pinned       INTEGER NOT NULL DEFAULT 0,
        use_count    INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        last_used_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS focus_sessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id         INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        label           TEXT NOT NULL DEFAULT '',
        planned_minutes INTEGER NOT NULL DEFAULT 25,
        started_at      TEXT NOT NULL,
        ended_at        TEXT,
        completed       INTEGER NOT NULL DEFAULT 0,
        notes           TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_audit (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        session_id  TEXT,
        tool        TEXT NOT NULL,
        args_json   TEXT NOT NULL DEFAULT '{}',
        status      TEXT NOT NULL DEFAULT 'proposed',
        permission  TEXT,
        duration_ms INTEGER,
        message     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tool_audit_ts ON tool_audit(ts)",
    """
    CREATE TABLE IF NOT EXISTS cloud_usage (
        provider     TEXT NOT NULL,
        day          TEXT NOT NULL,
        requests     INTEGER NOT NULL DEFAULT 0,
        tokens_in    INTEGER NOT NULL DEFAULT 0,
        tokens_out   INTEGER NOT NULL DEFAULT 0,
        last_error   TEXT,
        quota_hit    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (provider, day)
    )
    """,
)

#: Full-text search tables.  Created separately because not every SQLite build
#: ships FTS5; the repositories fall back to ``LIKE`` queries when unavailable.
FTS_STATEMENTS: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
        title, body, tags, content='notes', content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON notes BEGIN
        INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON notes BEGIN
        INSERT INTO notes_fts(notes_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS notes_fts_au AFTER UPDATE ON notes BEGIN
        INSERT INTO notes_fts(notes_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
        INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
    END
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content, key, content='memories', content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, key) VALUES (new.id, new.content, coalesce(new.key, ''));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, key) VALUES ('delete', old.id, old.content, coalesce(old.key, ''));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, key) VALUES ('delete', old.id, old.content, coalesce(old.key, ''));
        INSERT INTO memories_fts(rowid, content, key) VALUES (new.id, new.content, coalesce(new.key, ''));
    END
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        content, content='messages', content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
)

#: version -> statements applied when upgrading *to* that version
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: BASE_STATEMENTS,
    2: (
        # v2: reminder delivery tracking + note pinning shortcut index
        "ALTER TABLE reminder_log ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0",
    ),
}

#: Statements that must run only when the column is missing (defensive re-run).
_IGNORABLE_MIGRATION_ERRORS = ("duplicate column name",)


def ignorable_migration_error(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in _IGNORABLE_MIGRATION_ERRORS)
