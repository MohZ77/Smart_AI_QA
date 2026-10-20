from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass
from typing import Callable

from ..types import Chunk, RawDocument
from ..utils import normalize_text, split_sentences, stable_id, truncate

logger = logging.getLogger(__name__)

_ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百千万零〇0-9]+条")
_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇0-9]+[章节编部分]")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_CN_HEADING_RE = re.compile(r"^([一二三四五六七八九十]+、)\s*(.+)$")
_PAREN_HEADING_RE = re.compile(r"^（[一二三四五六七八九十]+）\s*(.+)$")
_NUM_HEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,2})[、.\s]\s*(.+)$")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_PAGE_NO_RE = re.compile(r"^\s*[-—]?\s*\d{1,4}\s*[-—]?\s*$")


@dataclass
class _Section:
    start: int
    end: int
    heading: str
    level: int
    path: str


@dataclass
class _Span:
    start: int
    end: int


class StructureChunker:
    """结构感知分块：标题/条款边界优先，句边界滑动窗口兜底，表格保持完整。"""

    def __init__(
        self,
        max_chars: int = 520,
        min_chars: int = 120,
        overlap_chars: int = 100,
        parent_max_chars: int = 1600,
        table_max_chars: int = 1800,
        pii_masking: bool = True,
    ) -> None:
        self.max_chars = max(120, max_chars)
        self.min_chars = max(0, min(min_chars, self.max_chars // 2))
        self.overlap_chars = max(0, min(overlap_chars, self.max_chars // 2))
        self.parent_max_chars = max(self.max_chars, parent_max_chars)
        self.table_max_chars = max(self.max_chars, table_max_chars)
        self.pii_masking = pii_masking

    def chunk_document(self, doc: RawDocument) -> list[Chunk]:
        full_text, spans = self._join_pages(doc)
        if not full_text.strip():
            return []
        full_text = self._strip_repeated_margins(full_text, spans)
        sections = self._split_sections(full_text)
        chunks: list[Chunk] = []
        for section in sections:
            section_text = full_text[section.start : section.end]
            if not section_text.strip():
                continue
            pieces = self._split_section(section_text)
            for piece in pieces:
                text = full_text[section.start + piece.start : section.start + piece.end].strip()
                marker_ids = re.findall(r"\[\[IMAGE:([A-Za-z0-9_-]+)\]\]", text)
                if marker_ids:
                    text = re.sub(r"\[\[IMAGE:[A-Za-z0-9_-]+\]\]", "", text).strip()
                    if len(text) < 20:
                        text = " ".join(
                            image.alt or "条款图片"
                            for image in doc.images
                            if image.image_id in marker_ids
                        )
                if len(text) < 20:
                    continue
                article_match = _ARTICLE_RE.search(section.heading) or _ARTICLE_RE.search(text[:120])
                article = article_match.group(0) if article_match else ""
                page_start = self._page_at(spans, section.start + piece.start)
                page_end = self._page_at(spans, section.start + piece.end)
                image_ids = set(marker_ids)
                if doc.metadata.get("image_mode") == "page":
                    for image in doc.images:
                        if image.page and page_start <= image.page <= page_end:
                            image_ids.add(image.image_id)
                chunk_id = stable_id(
                    doc.doc_id, section.path, article, section.start + piece.start, length=24
                )
                chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        doc_id=doc.doc_id,
                        text=normalize_text(text),
                        title=doc.title,
                        section=section.path,
                        article=article,
                        page=page_start,
                        page_end=page_end,
                        parent_text=truncate(
                            re.sub(
                                r"\[\[IMAGE:[A-Za-z0-9_-]+\]\]", "", section_text
                            ).strip(),
                            self.parent_max_chars,
                        ),
                        doc_type=doc.doc_type,
                        asset_class=doc.asset_class,
                        industry=doc.industry,
                        publish_date=doc.publish_date,
                        risk_level=doc.risk_level,
                        product_code=doc.product_code,
                        effective_date=doc.effective_date,
                        metadata={
                            **doc.metadata,
                            "source_path": doc.source_path,
                            "image_ids": sorted(image_ids),
                            "heading_level": section.level,
                            "char_start": section.start + piece.start,
                            "char_end": section.start + piece.end,
                            "chunk_index": len(chunks),
                        },
                    )
                )
        return chunks

    def _join_pages(self, doc: RawDocument) -> tuple[str, list[tuple[int, int, int]]]:
        parts: list[str] = []
        spans: list[tuple[int, int, int]] = []
        cursor = 0
        for page in doc.pages:
            text = page.text
            if parts:
                parts.append("\n")
                cursor += 1
            start = cursor
            parts.append(text)
            cursor += len(text)
            spans.append((start, cursor, page.number))
        return "".join(parts), spans

    def _page_at(self, spans: list[tuple[int, int, int]], offset: int) -> int:
        if not spans:
            return 0
        starts = [s[0] for s in spans]
        index = max(0, bisect.bisect_right(starts, offset) - 1)
        index = min(index, len(spans) - 1)
        return spans[index][2]

    def _strip_repeated_margins(
        self, text: str, spans: list[tuple[int, int, int]]
    ) -> str:
        """删除跨页重复的页眉页脚与纯页码行；不改变页码映射，仅替换为空格占位。"""
        if len(spans) <= 2:
            return text
        lines = text.splitlines(keepends=True)
        offsets: list[int] = []
        cursor = 0
        for line in lines:
            offsets.append(cursor)
            cursor += len(line)

        line_counts: dict[str, set[int]] = {}
        for line, offset in zip(lines, offsets):
            key = re.sub(r"\d+", "#", line.strip())
            if not key or len(key) > 60:
                continue
            page_no = self._page_at(spans, offset)
            line_counts.setdefault(key, set()).add(page_no)

        threshold = max(2, int(len(spans) * 0.5))
        repeated = {key for key, pages in line_counts.items() if len(pages) >= threshold}
        result: list[str] = []
        for line, offset in zip(lines, offsets):
            key = re.sub(r"\d+", "#", line.strip())
            is_repeated = (
                key in repeated
                and len(line.strip()) <= 60
                and self._parse_heading(line) is None
            )
            if _PAGE_NO_RE.match(line) or is_repeated:
                # 用等长空格占位，保证后续字符偏移与页码映射不变。
                if line.endswith("\n"):
                    result.append(" " * (len(line) - 1) + "\n")
                else:
                    result.append(" " * len(line))
            else:
                result.append(line)
        return "".join(result)

    def _split_sections(self, text: str) -> list[_Section]:
        lines = text.splitlines(keepends=True)
        offsets: list[int] = []
        cursor = 0
        for line in lines:
            offsets.append(cursor)
            cursor += len(line)

        boundaries: list[tuple[int, str, int, str]] = []
        stack: list[tuple[int, str]] = []
        for line, offset in zip(lines, offsets):
            heading = self._parse_heading(line)
            if not heading:
                continue
            level, title = heading
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = " > ".join(item[1] for item in stack[-3:])
            boundaries.append((offset, title, level, path))

        if not boundaries:
            return [_Section(0, len(text), "", 0, "")]

        sections: list[_Section] = []
        if boundaries[0][0] > 0:
            sections.append(_Section(0, boundaries[0][0], "", 0, ""))
        for idx, (start, title, level, path) in enumerate(boundaries):
            end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(text)
            sections.append(_Section(start, end, title, level, path))
        return sections

    def _parse_heading(self, line: str) -> tuple[int, str] | None:
        stripped = line.strip()
        if not stripped or len(stripped) > 80:
            return None
        if _PAGE_NO_RE.match(stripped) or _TABLE_LINE_RE.match(stripped):
            return None

        match = _MD_HEADING_RE.match(stripped)
        if match:
            return len(match.group(1)), match.group(2).strip()

        match = _CHAPTER_RE.match(stripped)
        if match and len(stripped) <= 50:
            return 1, stripped

        match = _ARTICLE_RE.match(stripped)
        if match and len(stripped) <= 80:
            return 3, stripped

        match = _CN_HEADING_RE.match(stripped)
        if match and len(match.group(2)) <= 40 and not re.search(r"[。；;]$", stripped):
            return 2, stripped

        match = _PAREN_HEADING_RE.match(stripped)
        if match and len(match.group(1)) <= 30 and not re.search(r"[。；;]$", stripped):
            return 4, stripped

        match = _NUM_HEADING_RE.match(stripped)
        if (
            match
            and len(match.group(2)) <= 40
            and not re.search(r"[。；;，,]$", stripped)
            and len(stripped) <= 60
        ):
            return 2 + min(match.group(1).count("."), 2), stripped
        return None

    def _split_section(self, text: str) -> list[_Span]:
        if len(text) <= self.max_chars:
            return [_Span(0, len(text))]

        table_lines = [line for line in text.splitlines() if _TABLE_LINE_RE.match(line)]
        if len(table_lines) >= max(3, len(text.splitlines()) // 2):
            return self._split_table(text)
        return self._split_by_sentence(text)

    def _split_by_sentence(self, text: str) -> list[_Span]:
        sentences = split_sentences(text)
        if not sentences:
            return [_Span(0, len(text))]

        positions: list[tuple[int, int]] = []
        cursor = 0
        for sentence in sentences:
            index = text.find(sentence, cursor)
            if index < 0:
                index = cursor
            end = index + len(sentence)
            positions.append((index, end))
            cursor = end

        spans: list[_Span] = []
        current_start, current_end = positions[0]
        for position_index in range(1, len(positions)):
            sent_start, sent_end = positions[position_index]
            if current_end - current_start + (sent_end - sent_start) <= self.max_chars:
                current_end = sent_end
                continue
            spans.append(_Span(current_start, current_end))

            # 用尾部句子作为重叠上下文，避免跨块语义断裂。
            overlap_start = current_end
            back = position_index - 1
            while back >= 0 and current_end - positions[back][0] <= self.overlap_chars:
                overlap_start = positions[back][0]
                back -= 1
            current_start, current_end = min(overlap_start, sent_start), sent_end
        spans.append(_Span(current_start, current_end))

        # 合并过短的尾块，减少无意义碎片。
        if len(spans) > 1 and spans[-1].end - spans[-1].start < self.min_chars:
            spans[-2] = _Span(spans[-2].start, spans[-1].end)
            spans.pop()
        return spans

    def _split_table(self, text: str) -> list[_Span]:
        lines = text.split("\n")
        spans: list[_Span] = []
        cursor = 0
        block_start = 0
        block_len = 0

        for line in lines:
            line_len = len(line) + 1
            if block_len and block_len + line_len > self.table_max_chars:
                spans.append(_Span(block_start, cursor))
                block_start = cursor
                block_len = 0
            block_len += line_len
            cursor += line_len
        if block_start < len(text):
            spans.append(_Span(block_start, len(text)))
        return spans
