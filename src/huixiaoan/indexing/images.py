from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from ..types import ImageAsset

logger = logging.getLogger(__name__)


class ImageStore:
    """图片落盘与索引；图片 URL 统一通过 /v1/images/{id} 暴露。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._lock = threading.RLock()
        self._index: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.index_path.exists():
            return {}
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            logger.warning("图片索引损坏，重新建立")
            return {}

    def _save_index(self) -> None:
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    def save(self, asset: ImageAsset) -> dict[str, Any]:
        with self._lock:
            record: dict[str, Any] = {
                "id": asset.image_id,
                "doc_id": asset.doc_id,
                "page": asset.page,
                "alt": asset.alt,
                "caption": asset.caption,
                "mime": asset.mime,
            }
            if asset.data:
                suffix = asset.ext if asset.ext.startswith(".") else f".{asset.ext}"
                target_dir = self.root / asset.doc_id
                target_dir.mkdir(parents=True, exist_ok=True)
                path = target_dir / f"{asset.image_id}{suffix}"
                path.write_bytes(asset.data)
                record["path"] = str(path)
                record["url"] = f"/v1/images/{asset.image_id}"
            elif asset.remote_url:
                record["url"] = asset.remote_url
                record["remote"] = True
            self._index[asset.image_id] = record
            self._save_index()
            return record

    def get(self, image_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._index.get(image_id)

    def path(self, image_id: str) -> Path | None:
        record = self.get(image_id)
        if not record or not record.get("path"):
            return None
        path = Path(record["path"]).resolve()
        root = self.root.resolve()
        if root not in path.parents:
            return None
        return path if path.exists() else None

    def count(self) -> int:
        with self._lock:
            return len(self._index)
