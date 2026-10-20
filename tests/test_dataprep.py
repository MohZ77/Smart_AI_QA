from __future__ import annotations

import json
from pathlib import Path

from huixiaoan.dataprep.compliance import DataComplianceFilter
from huixiaoan.dataprep.dedup import dedupe_records
from huixiaoan.dataprep.normalize import normalize_record
from huixiaoan.dataprep.pipeline import prepare_dataset
from huixiaoan.dataprep.schema import SFTRecord
from huixiaoan.dataprep.split import split_records


def _record(record_id: str, source_id: str, asset_class: str = "fund") -> SFTRecord:
    return normalize_record(
        SFTRecord.from_dict(
            {
                "id": record_id,
                "instruction": "消费主题基金适合C3用户吗？",
                "output": (
                    "需要结合风险等级和持有期限判断。基金投资有风险，"
                    "过往业绩不代表未来表现，不构成投资建议。"
                ),
                "asset_class": asset_class,
                "intent": "product_consultation",
                "risk_level": "C3",
                "source_id": source_id,
            }
        )
    )


def test_normalize_and_append_disclaimer() -> None:
    record = normalize_record(
        SFTRecord.from_dict(
            {
                "instruction": "基金怎么选？",
                "output": "先看风险等级和回撤，再看费率和基金经理。",
                "asset_class": "基金",
                "intent": "product_consultation",
            }
        )
    )
    allowed, report = DataComplianceFilter().apply(record)
    assert allowed is not None
    assert allowed.asset_class == "fund"
    assert "不构成投资建议" in allowed.output
    assert report["modified"]


def test_compliance_rejects_promise_but_allows_negation() -> None:
    bad = normalize_record(
        SFTRecord.from_dict(
            {
                "instruction": "这个基金怎么样？",
                "output": "这个产品保证收益，稳赚不赔。",
                "asset_class": "fund",
            }
        )
    )
    allowed, report = DataComplianceFilter().apply(bad)
    assert allowed is None
    assert report["violations"]

    good = normalize_record(
        SFTRecord.from_dict(
            {
                "instruction": "销售可以承诺收益吗？",
                "output": "不可以，销售不得承诺收益，也不得保本保收益。",
                "asset_class": "insurance",
            }
        )
    )
    allowed, _ = DataComplianceFilter().apply(good)
    assert allowed is not None


def test_exact_dedup() -> None:
    records = [_record("a", "s1"), _record("b", "s2")]
    kept, stats = dedupe_records(records)
    assert len(kept) == 1
    assert stats.exact_duplicates == 1


def test_group_split_has_no_source_leakage() -> None:
    records = [_record(f"r{i}", f"s{i}") for i in range(6)]
    splits, stats = split_records(records)
    assert stats["counts"]["train"] > 0
    assert stats["counts"]["val"] > 0
    assert stats["counts"]["test"] > 0
    seen: dict[str, str] = {}
    for split_name, items in splits.items():
        for item in items:
            assert item.source_id not in seen or seen[item.source_id] == split_name
            seen[item.source_id] = split_name


def test_prepare_dataset_exports_and_reports_target_gap(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    report = prepare_dataset(
        root / "data" / "sft" / "seed.jsonl",
        tmp_path,
        target=20,
        min_quality=60,
    )
    assert report.kept == 8
    assert report.manifest["target_gap"] == 12
    assert (tmp_path / "train.jsonl").exists()
    assert (tmp_path / "train.chatml.jsonl").exists()
    first = json.loads((tmp_path / "train.chatml.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["messages"][0]["role"] == "system"
    assert first["messages"][-1]["role"] == "assistant"
