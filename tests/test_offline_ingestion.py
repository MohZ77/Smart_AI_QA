from __future__ import annotations


def test_offline_markdown_reaches_vector_store(container, tmp_path) -> None:
    path = tmp_path / "专属测试基金.md"
    path.write_text(
        """---
asset_class: fund
doc_type: fund
risk_level: R3
publish_date: 2026-10-19
---

# 专属测试基金说明书

## 一、风险等级

专属测试基金的风险等级为 R3，适合风险承受能力为 C3 及以上的投资者。

## 二、风险提示

基金投资有风险，过往业绩不代表未来表现，不构成投资建议。
""",
        encoding="utf-8",
    )
    before = container.vector_store.count()
    report = container.ingest([str(path)])
    assert report.errors == []
    assert container.vector_store.count() > before
    hits = container.vector_store.search(
        container.embedder.encode_query("专属测试基金 风险等级"), top_k=5
    )
    assert hits
    assert any("专属测试基金" in hit.chunk.title for hit in hits)

