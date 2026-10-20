from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict

from .schema import ASSET_CLASSES, SFTRecord


def split_records(
    records: list[SFTRecord],
    val_ratio: float = 0.05,
    test_ratio: float = 0.05,
) -> tuple[dict[str, list[SFTRecord]], dict]:
    """按 source_id 分组切分，防止同一份研报的多个问答同时出现在 train/test。"""
    groups: dict[str, list[SFTRecord]] = defaultdict(list)
    for record in records:
        groups[record.source_id or record.id].append(record)

    by_asset: dict[str, list[str]] = defaultdict(list)
    for source_id, items in groups.items():
        dominant = Counter(item.asset_class for item in items).most_common(1)[0][0]
        by_asset[dominant].append(source_id)

    splits: dict[str, list[SFTRecord]] = {"train": [], "val": [], "test": []}
    for asset in ASSET_CLASSES:
        source_ids = sorted(
            by_asset.get(asset, []),
            key=lambda value: hashlib.sha1(value.encode("utf-8")).hexdigest(),
        )
        if not source_ids:
            continue
        count = len(source_ids)
        val_count = max(1, math.ceil(count * val_ratio)) if count >= 3 else 0
        test_count = max(1, math.ceil(count * test_ratio)) if count >= 3 else 0
        if val_count + test_count >= count:
            val_count = 1 if count >= 3 else 0
            test_count = 1 if count >= 3 else 0
        for index, source_id in enumerate(source_ids):
            if index < val_count:
                target = "val"
            elif index < val_count + test_count:
                target = "test"
            else:
                target = "train"
            splits[target].extend(groups[source_id])

    stats = {
        "group_count": len(groups),
        "counts": {name: len(items) for name, items in splits.items()},
        "asset_distribution": {
            name: dict(Counter(item.asset_class for item in items))
            for name, items in splits.items()
        },
    }
    return splits, stats
