from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import jieba

_STOPWORDS = {
    "的", "了", "和", "与", "及", "或", "是", "在", "有", "对", "为", "我", "你", "他",
    "她", "它", "这", "那", "吗", "呢", "什么", "怎么", "如何", "是否", "可以", "请问",
    "一下", "一个", "以及", "还有", "需要", "应该", "谢谢", "您好", "你好",
}
_PII_PATTERNS = [
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "<PHONE>"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "<ID_CARD>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "<BANK_CARD>"),
]

_JIEBA_LOCK = threading.Lock()
_JIEBA_READY = False


def stable_id(*parts: Any, length: int = 20) -> str:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:length]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_jieba(dict_path: Path | None = None) -> None:
    global _JIEBA_READY
    if _JIEBA_READY:
        return
    with _JIEBA_LOCK:
        if _JIEBA_READY:
            return
        jieba.setLogLevel(60)
        if dict_path and dict_path.exists():
            jieba.load_userdict(str(dict_path))
        _JIEBA_READY = True


def normalize_text(text: str) -> str:
    text = text.translate(str.maketrans("０１２３４５６７８９％", "0123456789%"))
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_question(text: str) -> str:
    text = normalize_text(text).lower()
    text = re.sub(r"[\s，。！？、,.!?；;：:\"'“”‘’()（）\[\]【】]+", "", text)
    return text


def tokenize(text: str, dict_path: Path | None = None) -> list[str]:
    ensure_jieba(dict_path)
    tokens: list[str] = []
    for token in jieba.lcut_for_search(text.lower()):
        token = token.strip()
        if not token or token in _STOPWORDS:
            continue
        if re.fullmatch(r"[\W_]+", token):
            continue
        tokens.append(token)
    return tokens


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句，保留标点，避免把条款编号切碎。"""
    normalized = normalize_text(text)
    if not normalized:
        return []
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", normalized)
    sentences = [p.strip() for p in parts if p and p.strip()]
    return sentences or [normalized]


def mask_pii(text: str) -> str:
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, max_chars: int, suffix: str = "…") -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - len(suffix))] + suffix


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class Timer:
    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        self.elapsed_ms = 0.0
        return self

    def __exit__(self, *args: Any) -> None:
        self.elapsed_ms = (time.perf_counter() - self.start) * 1000
