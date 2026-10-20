from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


class Database:
    """SQLite / MySQL 双后端薄封装。

    业务 SQL 统一使用 `?` 占位符；MySQL 下自动转换为 `%s`。
    这样开发环境零依赖，生产环境可通过 DSN 平滑切换。
    """

    def __init__(self, url: str) -> None:
        self.url = str(url)
        self.kind = "mysql" if self.url.startswith(("mysql://", "mysql+pymysql://")) else "sqlite"
        self._lock = threading.RLock()
        if self.kind == "mysql":
            try:
                import pymysql
                from pymysql.cursors import DictCursor
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("使用 MySQL 需要安装 pymysql：pip install pymysql") from exc
            parsed = urlparse(self.url)
            self._conn = pymysql.connect(
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 3306,
                user=unquote(parsed.username or "root"),
                password=unquote(parsed.password or ""),
                database=(parsed.path or "/").lstrip("/"),
                charset="utf8mb4",
                cursorclass=DictCursor,
                autocommit=True,
            )
        else:
            path = Path(self.url)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

    def _sql(self, sql: str) -> str:
        return sql if self.kind == "sqlite" else sql.replace("?", "%s")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(self._sql(sql), tuple(params))
            if self.kind == "sqlite":
                self._conn.commit()
            cursor.close()

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        with self._lock:
            cursor = self._conn.cursor()
            cursor.executemany(self._sql(sql), rows)
            if self.kind == "sqlite":
                self._conn.commit()
            cursor.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(self._sql(sql), tuple(params))
            rows = cursor.fetchall()
            cursor.close()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def executescript(self, script: str) -> None:
        with self._lock:
            if self.kind == "sqlite":
                self._conn.executescript(script)
                self._conn.commit()
            else:
                for statement in script.split(";"):
                    statement = statement.strip()
                    if statement:
                        self._conn.cursor().execute(statement)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
