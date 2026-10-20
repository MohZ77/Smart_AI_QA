from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..generation.answerer import Answerer
from ..types import UserProfile
from ..utils import Timer, percentile
from .metrics import (
    answer_keyword_coverage,
    citation_coverage,
    faithfulness_lite,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)

logger = logging.getLogger(__name__)


def load_dataset(path: str | Path) -> list[dict[str, Any]]:
    items = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def run_retrieval_eval(
    answerer: Answerer, dataset: list[dict[str, Any]], k: int = 5
) -> dict[str, Any]:
    rows = []
    for item in dataset:
        question = item["question"]
        profile = UserProfile(risk_level=item.get("risk_level", "C3"))
        plan = answerer.planner.plan(question, [], profile)
        hits, confidence, _ = answerer._retrieve(plan, profile)
        rows.append(
            {
                "question": question,
                "intent": plan.intent.name,
                "strategy": plan.strategy,
                "collections": answerer._collections_for_query(question, plan.intent.name),
                "recall_at_k": round(recall_at_k(hits, item, k), 4),
                "mrr_at_k": round(mrr_at_k(hits, item, k), 4),
                "ndcg_at_k": round(ndcg_at_k(hits, item, k), 4),
                "confidence": confidence,
                "top_titles": [hit.chunk.title for hit in hits[:k]],
            }
        )
    return {
        "mode": "retrieval",
        "count": len(rows),
        "k": k,
        "recall_at_k": _mean([row["recall_at_k"] for row in rows]),
        "mrr_at_k": _mean([row["mrr_at_k"] for row in rows]),
        "ndcg_at_k": _mean([row["ndcg_at_k"] for row in rows]),
        "rows": rows,
    }


async def run_answer_eval(
    answerer: Answerer, dataset: list[dict[str, Any]]
) -> dict[str, Any]:
    rows = []
    for index, item in enumerate(dataset):
        question = item["question"]
        profile = UserProfile(
            user_id=f"eval-{index}",
            risk_level=item.get("risk_level", "C3"),
        )
        with Timer() as timer:
            result = await answerer.answer(
                question, profile, session_id=f"eval-{index}"
            )
        contexts = [
            citation.quote or citation.title for citation in result.citations
        ]
        rows.append(
            {
                "question": question,
                "answer": result.answer,
                "intent": result.intent.name,
                "strategy": result.strategy,
                "confidence": result.confidence,
                "latency_ms": round(timer.elapsed_ms, 2),
                "keyword_coverage": round(answer_keyword_coverage(result.answer, item), 4),
                "faithfulness_lite": round(faithfulness_lite(result.answer, contexts), 4),
                "citation_coverage": citation_coverage(result.answer),
                "compliance_passed": bool(result.compliance and result.compliance.passed),
                "citations": len(result.citations),
            }
        )
    latencies = [row["latency_ms"] for row in rows]
    return {
        "mode": "answer",
        "count": len(rows),
        "keyword_coverage": _mean([row["keyword_coverage"] for row in rows]),
        "faithfulness_lite": _mean([row["faithfulness_lite"] for row in rows]),
        "citation_coverage": _mean([row["citation_coverage"] for row in rows]),
        "compliance_pass_rate": _mean([1.0 if row["compliance_passed"] else 0.0 for row in rows]),
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50), 2),
            "p95": round(percentile(latencies, 0.95), 2),
            "p99": round(percentile(latencies, 0.99), 2),
        },
        "rows": rows,
    }


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0
