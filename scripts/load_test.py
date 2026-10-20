from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from huixiaoan.utils import percentile

QUESTIONS = [
    "医疗险的年度免赔额是多少？",
    "平安福的等待期是多少天？",
    "基金的风险等级如何匹配用户？",
    "销售保险时可以承诺收益吗？",
    "消费板块最近的市场趋势怎么样？",
]


async def one_request(client: httpx.AsyncClient, args, semaphore: asyncio.Semaphore):
    question = random.choice(QUESTIONS)
    async with semaphore:
        started = time.perf_counter()
        try:
            response = await client.post(
                args.url,
                json={
                    "question": question,
                    "session_id": f"load-{random.randint(1, 100000)}",
                    "profile": {"user_id": "load", "risk_level": "C3"},
                },
                headers={"X-API-Key": args.api_key} if args.api_key else None,
            )
            latency = (time.perf_counter() - started) * 1000
            return latency, response.status_code, response.text[:200]
        except Exception as exc:
            return (time.perf_counter() - started) * 1000, 0, str(exc)


async def main_async(args) -> dict:
    semaphore = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        started = time.perf_counter()
        tasks = [one_request(client, args, semaphore) for _ in range(args.requests)]
        results = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started

    latencies = [item[0] for item in results]
    errors = [item for item in results if item[1] != 200]
    return {
        "url": args.url,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "elapsed_s": round(elapsed, 3),
        "rps": round(args.requests / elapsed, 2) if elapsed else 0,
        "success": args.requests - len(errors),
        "error": len(errors),
        "error_rate": round(len(errors) / max(1, args.requests), 4),
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50), 2),
            "p95": round(percentile(latencies, 0.95), 2),
            "p99": round(percentile(latencies, 0.99), 2),
            "max": round(max(latencies), 2),
        },
        "error_samples": errors[:5],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP 压测：验证并发与延迟")
    parser.add_argument("--url", default="http://127.0.0.1:8000/v1/chat")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--report", default="outputs/load_test.json")
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
