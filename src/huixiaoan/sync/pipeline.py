from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..service.container import Container

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    source: str
    scanned: int = 0
    changed: int = 0
    skipped: int = 0
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "scanned": self.scanned,
            "changed": self.changed,
            "skipped": self.skipped,
            "files": self.files,
            "errors": self.errors,
        }


class SyncPipeline:
    """增量数据同步适配层。

    生产环境可把 LocalJsonlSource 替换为 Wind / 基金净值 / 新闻 API 的拉取适配器；
    本类负责去重、落盘为带 frontmatter 的文档，再复用统一索引流水线。
    """

    def __init__(self, cfg: Config, container: Container) -> None:
        self.cfg = cfg
        self.container = container
        self.state_path = cfg.path("sync.state_path", "data/sync_state.json")
        self.output_dir = cfg.path("sync.output_dir", "data/raw/sync")
        self.state = self._load_state()

    def _load_state(self) -> dict[str, str]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def run(self, source: str, input_path: str | Path | None = None) -> SyncReport:
        path = Path(input_path or self._default_input(source))
        report = SyncReport(source=source)
        if not path.exists():
            report.errors.append(f"同步输入不存在: {path}")
            return report

        target_dir = self.output_dir / source
        target_dir.mkdir(parents=True, exist_ok=True)
        changed_files: list[str] = []
        new_state: dict[str, str] = {}
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                report.scanned += 1
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    report.errors.append(f"第 {line_no} 行 JSON 解析失败: {exc}")
                    continue
                item_id = str(item.get("id") or _hash(json.dumps(item, sort_keys=True)))[:24]
                content_hash = _hash(json.dumps(item, ensure_ascii=False, sort_keys=True))
                key = f"{source}:{item_id}"
                if self.state.get(key) == content_hash:
                    report.skipped += 1
                    continue
                target = target_dir / f"{item_id}.md"
                target.write_text(_to_markdown(source, item), encoding="utf-8")
                new_state[key] = content_hash
                changed_files.append(str(target))
                report.changed += 1

        ingestion_ok = True
        if changed_files:
            report.files = changed_files
            try:
                ingestion = self.container.ingest([str(target_dir)], rebuild=False)
                if ingestion.errors:
                    report.errors.extend(ingestion.errors)
            except Exception as exc:
                logger.exception("增量索引失败")
                report.errors.append(str(exc))
                ingestion_ok = False
        if ingestion_ok:
            self.state.update(new_state)
            self._save_state()
        return report

    def _default_input(self, source: str) -> str:
        return str(self.cfg.get(f"sync.{source}_jsonl", f"data/sync/{source}.jsonl"))


def _to_markdown(source: str, item: dict[str, Any]) -> str:
    title = str(item.get("title") or item.get("name") or "增量数据")
    body = str(item.get("text") or item.get("content") or item.get("summary") or "")
    frontmatter = {
        "asset_class": item.get("asset_class") or ("fund" if source == "fund" else "macro"),
        "industry": item.get("industry", ""),
        "publish_date": item.get("publish_date") or item.get("date", ""),
        "risk_level": item.get("risk_level", ""),
        "source": source,
        "doc_type": item.get("doc_type") or ("research" if source == "wind" else "other"),
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.extend(["---", "", f"# {title}", "", body, ""])
    return "\n".join(lines)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
