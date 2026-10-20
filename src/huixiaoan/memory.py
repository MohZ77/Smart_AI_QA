from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .storage.db import Database
from .utils import now_iso, stable_id


class ConversationStore:
    """会话记忆：默认保留最近 5 轮，供 Backtrack 策略做指代消解。"""

    def __init__(self, db_url: str) -> None:
        self._db = Database(db_url)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id VARCHAR(64) PRIMARY KEY,
                session_id VARCHAR(128) NOT NULL,
                user_id VARCHAR(128) NOT NULL,
                role VARCHAR(16) NOT NULL,
                content TEXT NOT NULL,
                created_at VARCHAR(64) NOT NULL
            )
            """
        )
        if self._db.kind == "sqlite":
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at)"
            )
        else:
            try:
                self._db.execute(
                    "CREATE INDEX idx_messages_session ON messages(session_id, created_at)"
                )
            except Exception:
                pass

    def append(self, session_id: str, user_id: str, role: str, content: str) -> None:
        self._db.execute(
            "REPLACE INTO messages(id, session_id, user_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                stable_id(session_id, role, content, now_iso(), length=24),
                session_id,
                user_id,
                role,
                content,
                now_iso(),
            ),
        )

    def recent(self, session_id: str, turns: int = 5) -> list[dict[str, str]]:
        limit = max(1, turns) * 2
        rows = self._db.query(
            "SELECT role, content FROM messages WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        )
        rows.reverse()
        return rows

    def clear(self, session_id: str) -> None:
        self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))

    def prune_older_than(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._db._lock:
            cursor = self._db._conn.cursor()
            cursor.execute(
                self._db._sql("DELETE FROM messages WHERE created_at < ?"), (cutoff,)
            )
            count = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            if self._db.kind == "sqlite":
                self._db._conn.commit()
            cursor.close()
        return count

    def close(self) -> None:
        self._db.close()
