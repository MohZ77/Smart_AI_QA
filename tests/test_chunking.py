from __future__ import annotations

from huixiaoan.ingestion.chunking import StructureChunker
from huixiaoan.types import Page, RawDocument


def test_structure_chunking_keeps_article_boundaries() -> None:
    text = """# 示例条款

## 第一条 等待期
自合同生效之日起九十日内为等待期。等待期内因疾病身故不承担保险责任。

## 第二条 免责
因故意犯罪、酒后驾驶导致的事故，本公司不承担保险责任。本条不适用于意外伤害。

## 第三条 理赔
申请人应当在十日内通知本公司，并提交完整材料。
"""
    doc = RawDocument(
        doc_id="d1",
        title="示例条款",
        source_path="demo.md",
        doc_type="insurance",
        asset_class="insurance",
        pages=[Page(number=1, text=text)],
    )
    chunks = StructureChunker(max_chars=80, min_chars=20, overlap_chars=10).chunk_document(doc)
    assert chunks
    assert all(chunk.article for chunk in chunks if "第" in chunk.section)
    first_article_chunks = [chunk for chunk in chunks if chunk.article == "第一条"]
    assert first_article_chunks
    assert all("第二条" not in chunk.text for chunk in first_article_chunks)
    assert any("九十日" in chunk.text for chunk in chunks)


def test_markdown_table_is_not_broken_into_characters() -> None:
    table = "| 责任 | 保额 |\n| --- | --- |\n" + "\n".join(
        f"| 责任{i} | {i * 10000} |" for i in range(1, 40)
    )
    doc = RawDocument(
        doc_id="d2",
        title="表格条款",
        source_path="table.md",
        pages=[Page(number=1, text=f"# 保障表\n\n{table}")],
    )
    chunks = StructureChunker(max_chars=200, table_max_chars=300).chunk_document(doc)
    assert chunks
    assert all("|" in chunk.text for chunk in chunks)
    assert max(len(chunk.text) for chunk in chunks) < 800
