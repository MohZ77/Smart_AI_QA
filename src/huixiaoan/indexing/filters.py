from __future__ import annotations

from typing import Any

from ..types import Chunk


def match_filters(chunk: Chunk, filters: dict[str, Any] | None) -> bool:
    if not filters:
        return True
    for key, expected in filters.items():
        actual = getattr(chunk, key, None)
        if actual is None or actual == "":
            actual = chunk.metadata.get(key)
        if isinstance(expected, dict):
            for operator, value in expected.items():
                if operator == "gte":
                    if actual is None or str(actual) < str(value):
                        return False
                elif operator == "lte":
                    if actual is None or str(actual) > str(value):
                        return False
                elif operator == "ne":
                    if str(actual) == str(value):
                        return False
                elif operator == "in":
                    if actual not in value:
                        return False
        elif isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def to_milvus_expr(filters: dict[str, Any] | None) -> str:
    if not filters:
        return ""
    clauses: list[str] = []
    for key, value in filters.items():
        if not key.isidentifier():
            continue
        if isinstance(value, dict):
            for operator, operand in value.items():
                symbol = {"gte": ">=", "lte": "<=", "ne": "!="}.get(operator)
                if symbol:
                    clauses.append(f"{key} {symbol} {_literal(operand)}")
                elif operator == "in":
                    clauses.append(f"{key} in {_literal_list(operand)}")
        elif isinstance(value, (list, tuple, set)):
            if value:
                clauses.append(f"{key} in {_literal_list(value)}")
        else:
            clauses.append(f"{key} == {_literal(value)}")
    return " and ".join(clauses)


def _literal(value: Any) -> str:
    if isinstance(value, str):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return str(value)


def _literal_list(values: Any) -> str:
    return "[" + ", ".join(_literal(item) for item in values) + "]"
