from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from huixiaoan.dataprep import prepare_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="金融 SFT 数据清洗、合规过滤、去重与切分")
    parser.add_argument("--input", default="data/sft/seed.jsonl", help="原始 JSONL")
    parser.add_argument("--out", default="data/sft/processed", help="输出目录")
    parser.add_argument("--target", type=int, default=60000, help="目标样本量，仅用于缺口审计")
    parser.add_argument("--min-quality", type=float, default=60.0)
    parser.add_argument("--val-ratio", type=float, default=0.05)
    parser.add_argument("--test-ratio", type=float, default=0.05)
    parser.add_argument("--strict", action="store_true", help="有 warning 也拒绝")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-chatml", action="store_true")
    parser.add_argument("--report", default="outputs/sft_prep_report.json")
    args = parser.parse_args()

    try:
        report = prepare_dataset(
            args.input,
            args.out,
            target=args.target,
            min_quality=args.min_quality,
            val_ratio=args.val_ratio,
            test_ratio=args.test_ratio,
            strict=args.strict,
            limit=args.limit,
            include_chatml=not args.no_chatml,
        )
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc

    payload = report.to_dict()
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        key: value
        for key, value in payload.items()
        if key not in {"manifest"}
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"manifest: {Path(args.out) / 'manifest.json'}")
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()
