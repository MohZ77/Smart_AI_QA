from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import mimetypes
import re
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

from ..config import Config
from ..types import ImageAsset, Page, RawDocument
from ..utils import normalize_text, sha256_text, stable_id

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {
    ".pdf", ".docx", ".md", ".markdown", ".txt",
    ".html", ".htm", ".csv", ".xlsx",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")

_DOC_TYPE_HINTS = {
    "insurance": ("保险", "条款", "policy", "insurance"),
    "fund": ("基金", "理财", "fund", "prospectus", "说明书"),
    "research": ("研报", "研究报告", "行业", "research", "report"),
    "regulation": ("法规", "监管", "办法", "规定", "regulation", "compliance"),
    "faq": ("faq", "问答", "常见问题"),
    "equity": ("股票", "权益", "equity", "stock"),
    "bond": ("债券", "固收", "bond"),
    "bank_wealth": ("银行理财", "理财子公司", "bank_wealth"),
}


def infer_doc_type(path: Path) -> str:
    folder_types = {
        "insurance": "insurance",
        "fund": "fund",
        "research": "research",
        "regulation": "regulation",
        "regulations": "regulation",
        "faq": "faq",
        "equity": "equity",
        "bond": "bond",
        "bank_wealth": "bank_wealth",
    }
    if path.parent.name.lower() in folder_types:
        return folder_types[path.parent.name.lower()]
    haystack = f"{path.parent.name}/{path.stem}".lower()
    for doc_type, hints in _DOC_TYPE_HINTS.items():
        if any(hint.lower() in haystack for hint in hints):
            return doc_type
    return "other"


_ASSET_CLASS_BY_TYPE = {
    "insurance": "insurance",
    "fund": "fund",
    "research": "macro",
    "regulation": "macro",
    "equity": "equity",
    "bond": "bond",
    "bank_wealth": "bank_wealth",
    "faq": "other",
}


def infer_asset_class(path: Path, doc_type: str) -> str:
    folder_classes = {
        "insurance": "insurance",
        "fund": "fund",
        "research": "macro",
        "regulation": "macro",
        "regulations": "macro",
        "macro": "macro",
        "equity": "equity",
        "bond": "bond",
        "bank_wealth": "bank_wealth",
    }
    if path.parent.name.lower() in folder_classes:
        return folder_classes[path.parent.name.lower()]
    haystack = f"{path.parent.name}/{path.stem}".lower()
    if any(token in haystack for token in ("insurance", "保险")):
        return "insurance"
    if any(token in haystack for token in ("fund", "基金")):
        return "fund"
    if any(token in haystack for token in ("bond", "债券", "固收")):
        return "bond"
    if any(token in haystack for token in ("bank_wealth", "银行理财")):
        return "bank_wealth"
    if any(token in haystack for token in ("equity", "股票", "权益")):
        return "equity"
    if any(token in haystack for token in ("macro", "宏观", "research", "研报", "regulation", "法规")):
        return "macro"
    return _ASSET_CLASS_BY_TYPE.get(doc_type, "other")


def infer_industry(path: Path, text: str = "") -> str:
    haystack = f"{path.parent.name}/{path.stem}/{text[:500]}".lower()
    for industry, keywords in {
        "消费": ("消费", "食品饮料", "家电", "consumer"),
        "医药": ("医药", "医疗", "生物", "healthcare"),
        "科技": ("科技", "半导体", "人工智能", "computer", "technology"),
        "金融": ("金融", "银行", "券商", "保险", "financial"),
        "制造": ("制造", "机械", "汽车", "新能源", "manufacturing"),
    }.items():
        if any(keyword.lower() in haystack for keyword in keywords):
            return industry
    return ""


def infer_publish_date(path: Path, text: str = "") -> str:
    date_pattern = re.compile(r"(20\d{2})[-_年./]?(\d{1,2})[-_月./]?(\d{1,2})?")
    for haystack in (path.stem, path.parent.name, text[:800]):
        match = date_pattern.search(haystack)
        if match:
            year, month, day = match.group(1), match.group(2), match.group(3) or "01"
            return f"{year}-{int(month):02d}-{int(day):02d}"
    return ""


def infer_risk_level(text: str = "") -> str:
    match = re.search(r"(?:风险等级|风险评级|risk\s*level)[^RrC]{0,12}(R[1-5]|C[1-5])", text, re.I)
    if match:
        return match.group(1).upper()
    return ""


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}, text
    metadata: dict[str, str] = {}
    end_index = -1
    for index in range(1, len(lines)):
        line = lines[index].strip()
        if line == "---":
            end_index = index
            break
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("'\"")
    if end_index < 0:
        return {}, text
    return metadata, "\n".join(lines[end_index + 1 :])


def discover_files(root: str | Path) -> list[Path]:
    path = Path(root)
    if path.is_file():
        return [path] if path.suffix.lower() in SUPPORTED_SUFFIXES else []
    if not path.exists():
        return []
    return sorted(
        p
        for p in path.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_SUFFIXES
        and not p.name.lower().startswith(("readme", "license", "manifest"))
        and not any(part.startswith(".") for part in p.parts)
    )


def _pdf_title(reader: object, fallback: str) -> str:
    try:
        metadata = getattr(reader, "metadata", None) or {}
        title = str(metadata.get("/Title", "") or "").strip()
        if title and len(title) > 1:
            return title
    except Exception:  # pragma: no cover - 第三方 PDF 元数据差异较大
        pass
    return fallback


def _image_meta(name: str, data: bytes) -> tuple[str, str]:
    suffix = Path(name).suffix.lower()
    if suffix not in _IMAGE_SUFFIXES:
        if data.startswith(b"\x89PNG"):
            suffix = ".png"
        elif data.startswith(b"\xff\xd8"):
            suffix = ".jpg"
        elif data.startswith(b"GIF8"):
            suffix = ".gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            suffix = ".webp"
        elif data.lstrip().startswith((b"<svg", b"<?xml")):
            suffix = ".svg"
        else:
            suffix = ".png"
    mime = mimetypes.guess_type(f"x{suffix}")[0] or "image/png"
    return suffix, mime


def _extract_markdown_images(
    path: Path, text: str
) -> tuple[str, list[ImageAsset]]:
    images: list[ImageAsset] = []
    doc_id = stable_id(path.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest(), length=24)

    def replace(match: re.Match[str]) -> str:
        alt = match.group(1).strip()
        target = match.group(2).strip().strip("<>")
        caption = (match.group(3) or alt).strip()
        data = b""
        mime = "image/png"
        ext = ".png"
        remote_url = ""
        if target.startswith("data:image/") and ";base64," in target:
            header, payload = target.split(",", 1)
            mime = header[5:].split(";", 1)[0] or "image/png"
            ext = mimetypes.guess_extension(mime) or ".png"
            try:
                data = base64.b64decode(payload)
            except Exception:
                return alt
        elif target.startswith(("http://", "https://")):
            remote_url = target
            ext = Path(urlparse(target).path).suffix.lower() or ".png"
            mime = mimetypes.guess_type(f"x{ext}")[0] or "image/png"
        else:
            local = (path.parent / unquote(target)).resolve()
            if local.exists() and local.is_file():
                data = local.read_bytes()
                ext, mime = _image_meta(local.name, data)
            else:
                logger.warning("Markdown 图片不存在: %s", local)
                return alt or target
        image_id = stable_id(
            doc_id, target, alt, len(data), data[:32].hex() if data else remote_url, length=20
        )
        images.append(
            ImageAsset(
                image_id=image_id,
                doc_id=doc_id,
                page=1,
                alt=alt or caption or "条款图片",
                caption=caption,
                mime=mime,
                ext=ext,
                data=data,
                remote_url=remote_url,
            )
        )
        return f"[[IMAGE:{image_id}]]"

    return _IMAGE_RE.sub(replace, text), images


def _extract_pdf_images(path: Path, doc_id: str) -> list[ImageAsset]:
    from pypdf import PdfReader

    images: list[ImageAsset] = []
    try:
        reader = PdfReader(str(path))
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_images = page.images
            except Exception as exc:  # pragma: no cover - 依赖 PDF 结构
                logger.warning("PDF 图片读取失败 %s 第%s页: %s", path.name, page_number, exc)
                continue
            for index, image in enumerate(page_images, start=1):
                try:
                    data = image.data
                except Exception:
                    continue
                ext, mime = _image_meta(image.name or f"image-{index}", data)
                images.append(
                    ImageAsset(
                        image_id=stable_id(doc_id, page_number, image.name, index, data[:32].hex(), length=20),
                        doc_id=doc_id,
                        page=page_number,
                        alt=f"{path.stem} 第 {page_number} 页图片 {index}",
                        caption=f"{path.stem} 第 {page_number} 页",
                        mime=mime,
                        ext=ext,
                        data=data,
                    )
                )
    except Exception as exc:  # pragma: no cover
        logger.warning("PDF 图片提取失败 %s: %s", path.name, exc)
    return images


def _extract_docx_images(document: object, doc_id: str) -> tuple[str, list[ImageAsset]]:
    images: list[ImageAsset] = []
    markers: list[str] = []
    for index, shape in enumerate(getattr(document, "inline_shapes", []), start=1):
        try:
            blip = shape._inline.graphic.graphicData.pic.blipFill.blip
            part = document.part.related_parts[blip.embed]
            data = part.blob
            ext, mime = _image_meta(str(part.partname), data)
            image_id = stable_id(doc_id, index, str(part.partname), data[:32].hex(), length=20)
            images.append(
                ImageAsset(
                    image_id=image_id,
                    doc_id=doc_id,
                    page=1,
                    alt=f"文档图片 {index}",
                    caption=f"文档图片 {index}",
                    mime=mime,
                    ext=ext,
                    data=data,
                )
            )
            markers.append(f"[[IMAGE:{image_id}]]")
        except Exception as exc:  # pragma: no cover - docx 图片关系差异
            logger.warning("DOCX 图片提取失败 %s: %s", doc_id, exc)
    return "\n".join(markers), images


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if self._skip:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "article", "tr"}:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            if not self._skip:
                self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)

    @property
    def text(self) -> str:
        return normalize_text("".join(self.parts))

    @property
    def title(self) -> str:
        return normalize_text("".join(self.title_parts))


def _load_html(path: Path) -> tuple[str, list[Page], dict, list[ImageAsset]]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    text = parser.text
    images: list[ImageAsset] = []
    image_markdown = []
    for match in re.finditer(r"<img\b[^>]*>", raw, flags=re.I):
        tag = match.group(0)
        src_match = re.search(r"\bsrc\s*=\s*[\"']([^\"']+)[\"']", tag, flags=re.I)
        alt_match = re.search(r"\balt\s*=\s*[\"']([^\"']*)[\"']", tag, flags=re.I)
        if src_match:
            alt = alt_match.group(1) if alt_match else ""
            image_markdown.append(f"![{alt}]({src_match.group(1)})")
    if image_markdown:
        markers, images = _extract_markdown_images(
            path, "\n".join(image_markdown)
        )
        text = f"{text}\n\n{markers}"
    title = parser.title or path.stem
    return title, [Page(number=1, text=text)], {"parser": "html"}, images


def _table_to_markdown(rows: list[list[str]], fallback_title: str) -> str:
    if not rows:
        return f"# {fallback_title}\n"
    width = max(len(row) for row in rows)
    normalized_rows = [row + [""] * (width - len(row)) for row in rows]
    header = normalized_rows[0]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in normalized_rows[1:])
    return f"# {fallback_title}\n\n" + "\n".join(lines)


def _load_csv(path: Path) -> tuple[str, list[Page], dict, list[ImageAsset]]:
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        for row in csv.reader(handle):
            if any(cell.strip() for cell in row):
                rows.append([cell.strip() for cell in row])
    return (
        path.stem,
        [Page(number=1, text=_table_to_markdown(rows, path.stem))],
        {"parser": "csv"},
        [],
    )


def _read_xlsx_minimal(path: Path) -> list[list[str]]:
    """无 openpyxl 时的最小 XLSX 读取：sharedStrings + 第一个 worksheet。"""
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append("".join(node.text or "" for node in item.iter()))
        sheet_name = next(
            (name for name in archive.namelist() if name.startswith("xl/worksheets/sheet")),
            "",
        )
        if not sheet_name:
            return []
        root = ET.fromstring(archive.read(sheet_name))
        rows: list[list[str]] = []
        for row in root.iter():
            if not row.tag.endswith("}row"):
                continue
            values: dict[int, str] = {}
            for cell in row:
                if not cell.tag.endswith("}c"):
                    continue
                ref = cell.attrib.get("r", "A1")
                column = 0
                for char in re.match(r"[A-Z]+", ref).group(0):
                    column = column * 26 + ord(char) - 64
                cell_type = cell.attrib.get("t")
                value = ""
                for node in cell.iter():
                    if node.tag.endswith("}v"):
                        value = node.text or ""
                        break
                    if node.tag.endswith("}t") and node.text:
                        value = node.text
                if cell_type == "s" and value.isdigit():
                    value = shared[int(value)] if int(value) < len(shared) else ""
                values[column - 1] = value
            if values:
                rows.append([values.get(i, "") for i in range(max(values) + 1)])
        return rows


def _load_xlsx(path: Path) -> tuple[str, list[Page], dict, list[ImageAsset]]:
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(str(path), read_only=True, data_only=True)
        sheet = workbook.active
        rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        title = sheet.title or path.stem
        workbook.close()
    except ImportError:
        rows = _read_xlsx_minimal(path)
        title = path.stem
    return (
        title,
        [Page(number=1, text=_table_to_markdown(rows, title))],
        {"parser": "xlsx"},
        [],
    )


def _load_pdf_with_mineru(path: Path, cfg: Config) -> list[Page] | None:
    """MinerU 适配层：输出目录中的 Markdown 作为解析结果，失败时返回 None。"""
    command = str(cfg.get("ingestion.mineru_cmd", "") or "").strip()
    if not command:
        return None

    timeout = int(cfg.get("ingestion.mineru_timeout_s", 300))
    with tempfile.TemporaryDirectory(prefix="mineru_") as out_dir:
        try:
            proc = subprocess.run(
                [command, "-p", str(path), "-o", out_dir],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning("MinerU 解析失败(%s): %s", path.name, proc.stderr[-500:])
                return None
            markdown_files = sorted(Path(out_dir).rglob("*.md"))
            if not markdown_files:
                logger.warning("MinerU 未产出 Markdown: %s", path.name)
                return None
            text = markdown_files[0].read_text(encoding="utf-8", errors="ignore")
            # MinerU 的 Markdown 不含页码；按分页符/标题粗略切页，保留可追溯的章节。
            pages = re.split(r"\n\s*<!--\s*page[:\s]*(\d+)\s*-->\s*\n", text, flags=re.I)
            if len(pages) > 1:
                result: list[Page] = []
                for idx in range(1, len(pages), 2):
                    result.append(Page(number=int(pages[idx]), text=pages[idx + 1]))
                return result
            return [Page(number=1, text=text)]
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("MinerU 调用异常(%s): %s", path.name, exc)
            return None


def _load_pdf(
    path: Path, cfg: Config
) -> tuple[str, list[Page], dict, list[ImageAsset]]:
    mineru_pages = _load_pdf_with_mineru(path, cfg)
    if mineru_pages:
        return path.stem, mineru_pages, {"parser": "mineru"}, []

    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pragma: no cover
            logger.warning("PDF 第 %s 页解析失败 %s: %s", index, path.name, exc)
            text = ""
        pages.append(Page(number=index, text=text))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    doc_id = stable_id(path.as_posix(), digest, length=24)
    images = _extract_pdf_images(path, doc_id)
    return (
        _pdf_title(reader, path.stem),
        pages,
        {"parser": "pypdf", "page_count": len(pages)},
        images,
    )


def _load_docx(path: Path) -> tuple[str, list[Page], dict, list[ImageAsset]]:
    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = DocxDocument(str(path))
    blocks: list[str] = []
    title = path.stem

    def iter_blocks() -> Iterable[object]:
        body = document.element.body
        for child in body.iterchildren():
            if child.tag.endswith("}p"):
                yield Paragraph(child, document)
            elif child.tag.endswith("}tbl"):
                yield Table(child, document)

    for block in iter_blocks():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            style = (block.style.name if block.style else "").lower()
            if "heading" in style or "标题" in style:
                level = "".join(ch for ch in style if ch.isdigit()) or "1"
                blocks.append(f"{'#' * min(int(level), 6)} {text}")
                if title == path.stem and int(level) <= 1:
                    title = text
            else:
                blocks.append(text)
        else:
            rows = []
            for row in block.rows:
                cells = [cell.text.replace("\n", " ").strip() for cell in row.cells]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                header = rows[0]
                sep = "| " + " | ".join(["---"] * len(block.rows[0].cells)) + " |"
                blocks.append("\n".join([header, sep, *rows[1:]]))

    text = "\n\n".join(blocks)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    doc_id = stable_id(path.as_posix(), digest, length=24)
    markers, images = _extract_docx_images(document, doc_id)
    if markers:
        text = f"{text}\n\n{markers}"
    return title, [Page(number=1, text=text)], {"parser": "python-docx"}, images


def _load_text(path: Path) -> tuple[str, list[Page], dict, list[ImageAsset]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    frontmatter, text = _parse_frontmatter(text)
    title = path.stem
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            title = stripped[:80]
            break
    return title, [Page(number=1, text=text)], {"parser": "text", **frontmatter}, []


def load_document(path: str | Path, cfg: Config) -> RawDocument:
    file_path = Path(path).resolve()
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        title, pages, meta, images = _load_pdf(file_path, cfg)
        image_mode = "page"
    elif suffix == ".docx":
        title, pages, meta, images = _load_docx(file_path)
        image_mode = "marker"
    elif suffix in {".html", ".htm"}:
        title, pages, meta, images = _load_html(file_path)
        image_mode = "marker" if images else "none"
    elif suffix == ".csv":
        title, pages, meta, images = _load_csv(file_path)
        image_mode = "none"
    elif suffix == ".xlsx":
        title, pages, meta, images = _load_xlsx(file_path)
        image_mode = "none"
    elif suffix in {".md", ".markdown", ".txt"}:
        title, pages, meta, images = _load_text(file_path)
        image_mode = "none"
        if suffix in {".md", ".markdown"} and pages:
            pages[0].text, images = _extract_markdown_images(
                file_path, pages[0].text
            )
            image_mode = "marker"
    else:
        raise ValueError(f"不支持的文件类型: {suffix}")

    sidecar_path = file_path.with_name(file_path.name + ".meta.json")
    sidecar: dict[str, object] = {}
    if sidecar_path.exists():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if sidecar.get("title"):
                title = str(sidecar["title"])
        except (OSError, json.JSONDecodeError):
            logger.warning("元数据 sidecar 解析失败: %s", sidecar_path)

    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    doc_id = stable_id(file_path.as_posix(), digest, length=24)
    cleaned_pages = [Page(number=p.number, text=normalize_text(p.text)) for p in pages if p.text.strip()]

    metadata = {
        **meta,
        "file_name": file_path.name,
        "suffix": suffix,
        "size_bytes": file_path.stat().st_size,
        "content_hash": digest,
        "image_mode": image_mode,
        "source_url": str(sidecar.get("source_url", "") or ""),
        "source_doc_id": str(sidecar.get("doc_id", "") or ""),
    }
    full_text = "\n".join(page.text for page in cleaned_pages)
    doc_type = str(
        sidecar.get("doc_type") or metadata.get("doc_type") or infer_doc_type(file_path)
    )
    asset_class = str(
        sidecar.get("asset_class")
        or metadata.get("asset_class")
        or infer_asset_class(file_path, doc_type)
    )
    industry = str(
        sidecar.get("industry") or metadata.get("industry") or infer_industry(file_path, full_text)
    )
    publish_date = str(
        sidecar.get("publish_date")
        or metadata.get("publish_date")
        or infer_publish_date(file_path, full_text)
    )
    risk_level = str(
        sidecar.get("risk_level") or metadata.get("risk_level") or infer_risk_level(full_text)
    )
    metadata.update(
        {
            "asset_class": asset_class,
            "industry": industry,
            "publish_date": publish_date,
            "risk_level": risk_level,
        }
    )
    return RawDocument(
        doc_id=doc_id,
        title=normalize_text(title)[:120] or file_path.stem,
        source_path=file_path.as_posix(),
        doc_type=doc_type,
        asset_class=asset_class,
        industry=industry,
        publish_date=publish_date,
        risk_level=risk_level,
        product_code=str(
            sidecar.get("product_code", "") or metadata.get("product_code", "") or ""
        ),
        effective_date=str(
            sidecar.get("effective_date", "") or metadata.get("effective_date", "") or ""
        ),
        pages=cleaned_pages,
        images=images,
        metadata=metadata,
    )


def load_documents(paths: Iterable[str | Path], cfg: Config) -> tuple[list[RawDocument], list[str]]:
    documents: list[RawDocument] = []
    errors: list[str] = []
    for path in paths:
        try:
            documents.append(load_document(path, cfg))
        except Exception as exc:
            logger.exception("加载文档失败: %s", path)
            errors.append(f"{path}: {exc}")
    return documents, errors


def load_faq_jsonl(path: str | Path) -> list[dict]:
    items: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning("FAQ JSONL 第 %s 行解析失败: %s", line_no, exc)
    return items
