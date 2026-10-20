from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .compliance import DataComplianceFilter
from .dedup import dedupe_records
from .normalize import normalize_record
from .quality import score_record
from .schema import SFTRecord
from .split import split_records


@dataclass
class DataPrepReport:
    input_path: str
    output_dir: str
    target: int
    total: int = 0
    invalid: int = 0
    compliance_rejected: int = 0
    low_quality: int = 0
    exact_duplicates: int = 0
    near_duplicates: int = 0
    kept: int = 0
    splits: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_path": self.input_path,
            "output_dir": self.output_dir,
            "target": self.target,
            "total": self.total,
            "invalid": self.invalid,
            "compliance_rejected": self.compliance_rejected,
            "low_quality": self.low_quality,
            "exact_duplicates": self.exact_duplicates,
            "near_duplicates": self.near_duplicates,
            "kept": self.kept,
            "splits": self.splits,
            "warnings": self.warnings,
            "manifest": self.manifest,
        }


def prepare_dataset(
    input_path: str | Path,
    output_dir: str | Path,
    target: int = 60000,
    min_quality: float = 60.0,
    val_ratio: float = 0.05,
    test_ratio: float = 0.05,
    strict: bool = False,
    limit: int | None = None,
    include_chatml: bool = True,
) -> DataPrepReport:
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    if not input_path.exists():
        raise FileNotFoundError(
            f"输入文件不存在: {input_path}。请准备 instruction/input/output 格式的 JSONL。"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    report = DataPrepReport(str(input_path), str(output_dir), target)
    filter_ = DataComplianceFilter(strict=strict)
    candidates: list[SFTRecord] = []

    with input_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            if limit is not None and report.total >= limit:
                break
            report.total += 1
            try:
                payload = json.loads(line)
                record = normalize_record(SFTRecord.from_dict(payload))
                record.validate()
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                report.invalid += 1
                if len(report.warnings) < 20:
                    report.warnings.append(f"第 {line_no} 行无效: {exc}")
                continue
            allowed, compliance_report = filter_.apply(record)
            if allowed is None:
                report.compliance_rejected += 1
                if len(report.warnings) < 20:
                    reason = (
                        compliance_report["violations"]
                        or compliance_report["warnings"]
                    )
                    report.warnings.append(f"第 {line_no} 行被合规过滤: {reason}")
                continue
            record = allowed
            record.metadata["compliance_warnings"] = compliance_report["warnings"]
            record.quality_score = score_record(record)
            if record.quality_score < min_quality:
                report.low_quality += 1
                continue
            candidates.append(record)

    deduped, dedup_stats = dedupe_records(candidates)
    report.exact_duplicates = dedup_stats.exact_duplicates
    report.near_duplicates = dedup_stats.near_duplicates
    report.kept = len(deduped)
    splits, split_stats = split_records(deduped, val_ratio, test_ratio)
    report.splits = split_stats["counts"]

    write_splits(output_dir, splits, include_chatml=include_chatml)
    distribution = dict(Counter(record.asset_class for record in deduped))
    intents = dict(Counter(record.intent for record in deduped))
    target_gap = max(0, target - len(deduped))
    manifest = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "input_path": str(input_path),
        "target": target,
        "target_gap": target_gap,
        "target_met": target_gap == 0,
        "kept": len(deduped),
        "asset_distribution": distribution,
        "intent_distribution": intents,
        "split_stats": split_stats,
        "dedup": {
            "exact": report.exact_duplicates,
            "near": report.near_duplicates,
        },
        "compliance": {
            "rejected": report.compliance_rejected,
            "strict": strict,
        },
        "quality": {
            "min_quality": min_quality,
            "rejected_low_quality": report.low_quality,
        },
        "warnings": report.warnings,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report.manifest = manifest
    if target_gap > 0:
        report.warnings.append(
            f"当前有效样本 {len(deduped)} 条，距目标 {target} 条还差 {target_gap} 条；"
            "请补充真实、授权、经分析师复核的数据，不要用未审核生成文本补足。"
        )
    return report


def write_splits(
    output_dir: Path,
    splits: dict[str, list[SFTRecord]],
    include_chatml: bool = True,
) -> None:
    for name, records in splits.items():
        _write_jsonl(output_dir / f"{name}.jsonl", (r.to_dict() for r in records))
        if include_chatml:
            _write_jsonl(
                output_dir / f"{name}.chatml.jsonl",
                (r.to_chat_messages() for r in records),
            )


def _write_jsonl(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
