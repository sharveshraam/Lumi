"""Local audit trail for every tool call.

The tool activity panel in the UI and the ``read_logs`` tool both read from
here.  Records are local, redacted and capped.  The user can clear the trail at
any time from Settings → Privacy.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from lumi.memory.store import Database
from lumi.security.redact import redact_value
from lumi.utils.timeutil import now, to_iso

log = logging.getLogger("lumi.security.audit")

MAX_IN_MEMORY = 200
AUDIT_STATUSES = ("proposed", "allowed", "denied", "running", "ok", "error", "cancelled")


@dataclass
class AuditEntry:
    id: Optional[int]
    ts: Optional[datetime]
    tool: str
    status: str
    permission: str = ""
    duration_ms: int = 0
    message: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": to_iso(self.ts) if self.ts else "",
            "tool": self.tool,
            "status": self.status,
            "permission": self.permission,
            "duration_ms": self.duration_ms,
            "message": self.message,
            "args": self.args,
            "session_id": self.session_id,
        }

    def human_line(self) -> str:
        stamp = self.ts.strftime("%H:%M:%S") if self.ts else "--:--:--"
        detail = f" - {self.message}" if self.message else ""
        return f"{stamp} [{self.status}] {self.tool}{detail}"


class AuditLog:
    """Writes tool activity to SQLite and keeps the recent ones in memory."""

    def __init__(self, db: Database | None = None, *, session_id: str = "session") -> None:
        self.db = db
        self.session_id = session_id
        self._recent: deque[AuditEntry] = deque(maxlen=MAX_IN_MEMORY)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[AuditEntry], None]] = []

    # ----------------------------------------------------------- listeners
    def add_listener(self, callback: Callable[[AuditEntry], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[AuditEntry], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, entry: AuditEntry) -> None:
        for callback in list(self._listeners):
            try:
                callback(entry)
            except Exception:  # pragma: no cover - UI listener must not break logging
                log.debug("audit listener failed", exc_info=True)

    # -------------------------------------------------------------- record
    def record(
        self,
        tool: str,
        *,
        status: str = "proposed",
        args: dict[str, Any] | None = None,
        permission: str = "",
        duration_ms: int = 0,
        message: str = "",
    ) -> AuditEntry:
        safe_args = redact_value(args or {})
        safe_message = str(message)[:500]
        if status not in AUDIT_STATUSES:  # pragma: no cover - defensive
            status = "proposed"
        stamp = now()
        entry = AuditEntry(
            id=None,
            ts=stamp,
            tool=tool,
            status=status,
            permission=permission,
            duration_ms=int(duration_ms),
            message=safe_message,
            args=safe_args if isinstance(safe_args, dict) else {},
            session_id=self.session_id,
        )
        if self.db is not None:
            try:
                entry.id = int(
                    self.db.execute_write(
                        """
                        INSERT INTO tool_audit(ts, session_id, tool, args_json, status, permission, duration_ms, message)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            to_iso(stamp),
                            self.session_id,
                            tool,
                            json.dumps(entry.args, default=str),
                            status,
                            permission,
                            int(duration_ms),
                            safe_message,
                        ),
                    )
                )
            except Exception as exc:  # pragma: no cover - auditing must never break a tool
                log.warning("could not write audit record: %s", exc)

        with self._lock:
            self._recent.append(entry)
        self._notify(entry)
        return entry

    # ---------------------------------------------------------------- read
    def recent(self, *, limit: int = 50) -> list[AuditEntry]:
        with self._lock:
            items = list(self._recent)[-limit:]
        return list(reversed(items))

    def recent_from_db(self, *, limit: int = 200) -> list[AuditEntry]:
        if self.db is None:
            return self.recent(limit=limit)
        rows = self.db.query(
            "SELECT * FROM tool_audit ORDER BY id DESC LIMIT ?", (int(limit),)
        )
        entries: list[AuditEntry] = []
        for row in rows:
            args: dict[str, Any] = {}
            try:
                parsed = json.loads(row["args_json"] or "{}")
                if isinstance(parsed, dict):
                    args = parsed
            except (ValueError, TypeError):
                args = {}
            entries.append(
                AuditEntry(
                    id=row["id"],
                    ts=datetime.fromisoformat(row["ts"]) if row["ts"] else None,
                    tool=row["tool"],
                    status=row["status"],
                    permission=row["permission"] or "",
                    duration_ms=int(row["duration_ms"] or 0),
                    message=row["message"] or "",
                    args=args,
                    session_id=row["session_id"] or "",
                )
            )
        return entries

    def summary(self, *, limit: int = 200) -> dict[str, int]:
        rows = self.db.query(
            "SELECT status, COUNT(*) AS n FROM (SELECT status FROM tool_audit ORDER BY id DESC LIMIT ?) GROUP BY status",
            (int(limit),),
        ) if self.db is not None else []
        return {row["status"]: int(row["n"]) for row in rows}

    def count(self) -> int:
        if self.db is None:
            return len(self._recent)
        return int(self.db.scalar("SELECT COUNT(*) FROM tool_audit", default=0) or 0)

    def clear(self) -> int:
        with self._lock:
            self._recent.clear()
        if self.db is None:
            return 0
        return int(self.db.execute_write("DELETE FROM tool_audit") or 0)

    def format_text(self, *, limit: int = 30) -> str:
        entries = self.recent_from_db(limit=limit)
        if not entries:
            return "No tool activity recorded yet."
        return "\n".join(entry.human_line() for entry in reversed(entries))
