from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from huixiaoan.config import load_config
from huixiaoan.evaluation.runner import load_dataset, run_answer_eval, run_retrieval_eval
from huixiaoan.service.container import Container


async def main_async(args: argparse.Namespace) -> dict:
    container = Container(load_config(args.config))
    try:
        dataset = load_dataset(args.dataset)
        report = {}
        if args.mode in {"retrieval", "both"}:
            report["retrieval"] = run_retrieval_eval(container.answerer, dataset, args.k)
        if args.mode in {"answer", "both"}:
            report["answer"] = await run_answer_eval(container.answerer, dataset)
        return report
    finally:
        container.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="评测检索与回答质量")
    parser.add_argument("--dataset", default="data/eval/qa.jsonl")
    parser.add_argument("--mode", choices=["retrieval", "answer", "both"], default="both")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--report", default="outputs/eval_report.json")
    args = parser.parse_args()

    report = asyncio.run(main_async(args))
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        key: {k: v for k, v in value.items() if k != "rows"}
        for key, value in report.items()
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"完整报告: {output}")


if __name__ == "__main__":
    main()
