from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from ..utils import percentile


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, int] = defaultdict(int)
        self.latencies_ms: list[float] = []

    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self.counters[name] += value

    def observe(self, latency_ms: float) -> None:
        with self._lock:
            self.latencies_ms.append(latency_ms)
            if len(self.latencies_ms) > 10000:
                self.latencies_ms = self.latencies_ms[-5000:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latencies = list(self.latencies_ms)
            counters = dict(self.counters)
        return {
            "counters": counters,
            "latency_ms": {
                "count": len(latencies),
                "p50": round(percentile(latencies, 0.50), 2),
                "p95": round(percentile(latencies, 0.95), 2),
                "p99": round(percentile(latencies, 0.99), 2),
            },
        }

    def render_prometheus(self, cache_stats: dict[str, int] | None = None) -> str:
        lines = [
            "# HELP huixiaoan_requests_total Total API requests",
            "# TYPE huixiaoan_requests_total counter",
        ]
        snapshot = self.snapshot()
        for name, value in snapshot["counters"].items():
            lines.append(f'huixiaoan_{name}{{}} {value}')
        lines.extend(
            [
                "# HELP huixiaoan_request_duration_ms Request latency in milliseconds",
                "# TYPE huixiaoan_request_duration_ms summary",
                f"huixiaoan_request_duration_ms_count {snapshot['latency_ms']['count']}",
                f"huixiaoan_request_duration_ms{{quantile=\"0.5\"}} {snapshot['latency_ms']['p50']}",
                f"huixiaoan_request_duration_ms{{quantile=\"0.95\"}} {snapshot['latency_ms']['p95']}",
                f"huixiaoan_request_duration_ms{{quantile=\"0.99\"}} {snapshot['latency_ms']['p99']}",
            ]
        )
        if cache_stats:
            for key, value in cache_stats.items():
                lines.append(f"huixiaoan_cache_{key} {value}")
        return "\n".join(lines) + "\n"
