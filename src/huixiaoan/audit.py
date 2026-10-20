from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .storage.db import Database
from .utils import now_iso

logger = logging.getLogger(__name__)


class AuditStore:
    """问答审计：每条建议均可追溯到请求、引用与合规结论。"""

    def __init__(self, db_url: str) -> None:
        self._db = Database(db_url)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS audit (
                id VARCHAR(64) PRIMARY KEY,
                request_id VARCHAR(64) NOT NULL,
                session_id VARCHAR(128) DEFAULT '',
                user_id VARCHAR(128) DEFAULT '',
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                intent VARCHAR(64) DEFAULT '',
                strategy VARCHAR(32) DEFAULT '',
                citations TEXT DEFAULT '',
                compliance TEXT DEFAULT '',
                profile_snapshot VARCHAR(1024) DEFAULT '',
                confidence DOUBLE DEFAULT 0,
                latency_ms DOUBLE DEFAULT 0,
                model VARCHAR(128) DEFAULT '',
                created_at VARCHAR(64) NOT NULL
            )
            """
        )
        try:
            self._db.execute(
                "ALTER TABLE audit ADD COLUMN profile_snapshot VARCHAR(1024) DEFAULT ''"
            )
        except Exception as exc:
            logger.debug("审计表 profile_snapshot 列已存在或迁移跳过: %s", exc)
        if self._db.kind == "sqlite":
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_audit_request ON audit(request_id)")
        else:
            try:
                self._db.execute("CREATE INDEX idx_audit_request ON audit(request_id)")
            except Exception:
                pass

    def log(self, record: dict[str, Any]) -> None:
        self.log_many([record])

    def log_many(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        rows = [self._row(record) for record in records]
        self._db.executemany(
            "REPLACE INTO audit(id, request_id, session_id, user_id, question, answer, intent, "
            "strategy, citations, compliance, profile_snapshot, confidence, latency_ms, "
            "model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    @staticmethod
    def _row(record: dict[str, Any]) -> tuple[Any, ...]:
        return (
            record.get("request_id"),
            record.get("request_id"),
            record.get("session_id", ""),
            record.get("user_id", ""),
            record.get("question", ""),
            record.get("answer", ""),
            record.get("intent", ""),
            record.get("strategy", ""),
            json.dumps(record.get("citations", []), ensure_ascii=False),
            json.dumps(record.get("compliance", {}), ensure_ascii=False),
            json.dumps(record.get("profile_snapshot", {}), ensure_ascii=False),
            float(record.get("confidence", 0)),
            float(record.get("latency_ms", 0)),
            record.get("model", ""),
            now_iso(),
        )

    def purge_older_than(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = (
            datetime.now().astimezone() - timedelta(days=days)
        ).isoformat(timespec="seconds")
        with self._db._lock:
            cursor = self._db._conn.cursor()
            cursor.execute(self._db._sql("DELETE FROM audit WHERE created_at < ?"), (cutoff,))
            count = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            if self._db.kind == "sqlite":
                self._db._conn.commit()
            cursor.close()
        return count

    def replay_spool(self, spool_dir: str | Path) -> int:
        """把数据库故障期间落盘的审计记录重新写入。"""
        count = 0
        for path in sorted(Path(spool_dir).glob("*.jsonl")):
            records = []
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            if records:
                self.log_many(records)
                count += len(records)
            path.rename(path.with_suffix(".done"))
        return count

    def get(self, request_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT * FROM audit WHERE request_id = ?", (request_id,)
        )
        if row:
            row["citations"] = json.loads(row.get("citations") or "[]")
            row["compliance"] = json.loads(row.get("compliance") or "{}")
            row["profile_snapshot"] = json.loads(row.get("profile_snapshot") or "{}")
        return row

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT * FROM audit ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def close(self) -> None:
        self._db.close()


class BufferedAuditStore:
    """非阻塞审计写入：请求只入队，后台批量落库；DB 故障时写 JSONL spool。"""

    def __init__(self, store: AuditStore, cfg) -> None:
        self.store = store
        self.batch_size = max(1, int(cfg.get("resilience.audit_batch_size", 50)))
        self.flush_interval = max(0.1, float(cfg.get("resilience.audit_flush_interval_s", 1.0)))
        self.retention_days = int(cfg.get("resilience.audit_retention_days", 180))
        self.spool_dir = Path(
            cfg.get("resilience.audit_spool_dir", "data/audit/spool")
        )
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=max(100, int(cfg.get("resilience.audit_queue_size", 10000)))
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="audit-writer")
        self._thread.start()
        self._last_purge = 0.0

    def log(self, record: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._spool([record])

    def _spool(self, records: list[dict[str, Any]]) -> None:
        path = self.spool_dir / f"audit-{datetime.now().strftime('%Y%m%d')}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("审计 spool 写入失败: %s", exc)

    def _worker(self) -> None:
        while not self._stop.is_set() or not self.queue.empty():
            batch: list[dict[str, Any]] = []
            try:
                batch.append(self.queue.get(timeout=self.flush_interval))
            except queue.Empty:
                continue
            while len(batch) < self.batch_size:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self.store.log_many(batch)
            except Exception as exc:
                logger.warning("审计批量写入失败，转 spool: %s", exc)
                self._spool(batch)
            finally:
                for _ in batch:
                    self.queue.task_done()
            now = time.time()
            if self.retention_days > 0 and now - self._last_purge > 86400:
                try:
                    removed = self.store.purge_older_than(self.retention_days)
                    if removed:
                        logger.info("审计清理旧记录: %s 条", removed)
                except Exception as exc:
                    logger.warning("审计清理失败: %s", exc)
                self._last_purge = now

    def flush(self, timeout_s: float = 5.0) -> None:
        # 数据库正常时会很快排空；故障时 worker 会转 spool，不会无限阻塞。
        self.queue.join()

    def get(self, request_id: str) -> dict[str, Any] | None:
        return self.store.get(request_id)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.recent(limit)

    def close(self) -> None:
        self._stop.set()
        self.queue.join()
        self._thread.join(timeout=5)
        self.store.close()
