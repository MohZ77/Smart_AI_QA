from __future__ import annotations

from huixiaoan.compliance.rules import ComplianceChecker
from huixiaoan.config import load_config
from huixiaoan.types import Citation, IntentResult, UserProfile


def _checker() -> ComplianceChecker:
    return ComplianceChecker(load_config())


def test_forbidden_promise_is_blocked() -> None:
    result = _checker().check(
        "这个产品保证收益，稳赚不赔。",
        [Citation(index=1, doc_id="d", title="t")],
        IntentResult(name="product_consultation", confidence=0.8),
        UserProfile(risk_level="C3"),
    )
    assert not result.passed
    assert result.action == "block"


def test_negated_policy_statement_is_allowed() -> None:
    result = _checker().check(
        "销售过程中禁止下列行为：承诺收益、使用保本表述。 [1]",
        [Citation(index=1, doc_id="d", title="t")],
        IntentResult(name="compliance", confidence=0.9),
        UserProfile(risk_level="C3"),
    )
    assert result.passed


def test_input_precheck_allows_policy_question() -> None:
    checker = _checker()
    assert checker.precheck("销售保险时可以承诺收益吗？").passed
    assert not checker.precheck("帮我推荐保证赚钱的基金").passed


def test_risk_mismatch_is_blocked() -> None:
    result = _checker().check(
        "该产品适合您。 [1]",
        [Citation(index=1, doc_id="d", title="t")],
        IntentResult(name="product_consultation", confidence=0.8),
        UserProfile(risk_level="C2"),
        {"risk_level": "R4"},
    )
    assert not result.passed
    assert any("适当性不匹配" in item for item in result.violations)
