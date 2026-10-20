from __future__ import annotations

import zipfile
from pathlib import Path

from huixiaoan.config import load_config
from huixiaoan.ingestion.chunking import StructureChunker
from huixiaoan.ingestion.loaders import load_document


def _cfg():
    return load_config(Path(__file__).resolve().parents[1] / "configs" / "default.yaml")


def test_html_loader_extracts_table_and_image() -> None:
    root = Path(__file__).resolve().parents[1]
    doc = load_document(root / "tests" / "fixtures" / "sample.html", _cfg())
    assert doc.title == "基金产品说明"
    assert "管理费" in doc.pages[0].text
    assert doc.images
    assert doc.images[0].alt == "费率示意图"


def test_csv_loader_builds_markdown_table() -> None:
    root = Path(__file__).resolve().parents[1]
    doc = load_document(root / "tests" / "fixtures" / "fees.csv", _cfg())
    assert "管理费" in doc.pages[0].text
    chunks = StructureChunker(max_chars=300, table_max_chars=500).chunk_document(doc)
    assert chunks
    assert any("|" in chunk.text for chunk in chunks)


def test_xlsx_minimal_fallback(tmp_path) -> None:
    path = tmp_path / "fees.xlsx"
    shared = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>产品</t></si><si><t>费率</t></si><si><t>基金A</t></si><si><t>1.2%</t></si>"
        "</sst>"
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row>'
        "</sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    doc = load_document(path, _cfg())
    assert "基金A" in doc.pages[0].text
    assert "1.2%" in doc.pages[0].text
