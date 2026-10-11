"""SQLite access layer.

A single :class:`Database` instance is shared by the whole application.  SQLite
is used from multiple threads (UI thread plus worker threads for reminders and
inference), so the connection is created with ``check_same_thread=False`` and
every statement is serialised behind a re-entrant lock.  Writes use short
transactions so the file stays healthy even if the process is killed.

The class deliberately exposes raw SQL helpers rather than an ORM: the schema is
small, the queries are explicit, and this keeps the dependency list short.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from lumi.memory import schema as schema_module
from lumi.version import DB_SCHEMA_VERSION

log = logging.getLogger("lumi.memory.store")

BUSY_TIMEOUT_MS = 5000


class Database:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self.fts_enabled = False
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------- lifecycle
    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.read_only:
                uri = f"file:{self.path}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
            else:
                conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if not self.read_only:
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                except sqlite3.DatabaseError:  # pragma: no cover - network filesystems
                    pass
                conn.execute("PRAGMA synchronous = NORMAL")
            self._conn = conn
            if not self.read_only:
                self._migrate()
                self._setup_fts()
            return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                except sqlite3.Error:  # pragma: no cover - defensive
                    pass
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------ migrations
    def _migrate(self) -> None:
        conn = self.connection
        current = self._read_schema_version()
        if current >= DB_SCHEMA_VERSION:
            return
        log.info("migrating database from schema v%s to v%s", current, DB_SCHEMA_VERSION)
        for version in sorted(schema_module.MIGRATIONS):
            if version <= current:
                continue
            for statement in schema_module.MIGRATIONS[version]:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    if schema_module.ignorable_migration_error(str(exc)):
                        log.debug("skipping already-applied migration statement: %s", exc)
                        continue
                    raise
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(version),),
            )
            conn.commit()
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(DB_SCHEMA_VERSION),),
        )
        conn.commit()

    def _read_schema_version(self) -> int:
        conn = self.connection
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError:
            return 0
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    def _setup_fts(self) -> None:
        conn = self.connection
        try:
            for statement in schema_module.FTS_STATEMENTS:
                conn.execute(statement)
            conn.commit()
            self.fts_enabled = True
        except sqlite3.OperationalError as exc:
            log.warning("full-text search unavailable (%s); using LIKE fallback", exc)
            self.fts_enabled = False
        self.set_meta("fts_enabled", "1" if self.fts_enabled else "0")

    # ------------------------------------------------------------- meta kv
    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    # --------------------------------------------------------------- queries
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run statements inside a transaction, retrying briefly if locked."""
        conn = self.connection
        with self._lock:
            attempts = 0
            while True:
                try:
                    conn.execute("BEGIN")
                    break
                except sqlite3.OperationalError as exc:
                    if "within a transaction" in str(exc):
                        break
                    attempts += 1
                    if attempts > 4:
                        raise
                    time.sleep(0.05 * attempts)
            try:
                yield conn
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:  # pragma: no cover - defensive
                    pass
                raise
            else:
                conn.commit()

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.execute(sql, tuple(params or ()))

    def execute_write(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Execute a write and return ``lastrowid`` for INSERTs, else rowcount.

        SQLite keeps a connection-wide ``lastrowid``, so a DELETE would otherwise
        report the id of an unrelated earlier INSERT.
        """
        with self.transaction() as conn:
            cursor = conn.execute(sql, tuple(params or ()))
            command = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
            if command in {"INSERT", "REPLACE"}:
                return int(cursor.lastrowid or 0)
            return int(cursor.rowcount or 0)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self.transaction() as conn:
            conn.executemany(sql, [tuple(row) for row in seq])

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self.connection.execute(sql, tuple(params or ()))
            return list(cursor.fetchall())

    def query_one(self, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Row | None:
        with self._lock:
            cursor = self.connection.execute(sql, tuple(params or ()))
            return cursor.fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | None = None, default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # ------------------------------------------------------- maintenance ops
    def vacuum(self) -> None:
        with self._lock:
            self.connection.execute("VACUUM")

    def checkpoint(self) -> None:
        try:
            with self._lock:
                self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            log.debug("wal checkpoint failed: %s", exc)

    def table_names(self) -> list[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [row["name"] for row in rows]

    def table_count(self, table: str) -> int:
        if not table.replace("_", "").isalnum():
            raise ValueError(f"invalid table name: {table!r}")
        return int(self.scalar(f"SELECT COUNT(*) FROM {table}", default=0) or 0)

    def size_bytes(self) -> int:
        try:
            total = self.path.stat().st_size
            for suffix in ("-wal", "-shm"):
                extra = self.path.with_name(self.path.name + suffix)
                if extra.exists():
                    total += extra.stat().st_size
            return total
        except OSError:
            return 0

    # -------------------------------------------------------------- helpers
    @staticmethod
    def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        return dict(row) if row is not None else {}

    @staticmethod
    def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def fts_available(self) -> bool:
        if self._conn is None:
            self.connect()
        return self.fts_enabled

    def secure_delete(self) -> None:
        """Wipe deleted pages - used by 'delete all data'."""
        try:
            with self._lock:
                self.connection.execute("PRAGMA secure_delete = ON")
                self.connection.execute("VACUUM")
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            log.debug("secure delete failed: %s", exc)


def open_database(path: Path | str) -> Database:
    """Open (and migrate) a database, returning a ready-to-use object."""
    db = Database(path)
    db.connect()
    return db
