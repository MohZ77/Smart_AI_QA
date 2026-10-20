from __future__ import annotations

import asyncio

from huixiaoan.types import UserProfile


def test_insurance_question_returns_cited_answer(container) -> None:
    result = asyncio.run(
        container.answerer.answer(
            "医疗险的年度免赔额是多少？",
            UserProfile(user_id="test", risk_level="C3"),
            "test-insurance",
        )
    )
    assert "一万元" in result.answer
    assert result.citations
    assert result.compliance and result.compliance.passed
    assert result.strategy in {"direct", "faq"}


def test_compliance_question_is_not_blocked(container) -> None:
    result = asyncio.run(
        container.answerer.answer(
            "销售保险时可以承诺收益吗？",
            UserProfile(user_id="test", risk_level="C3"),
            "test-compliance",
        )
    )
    assert "禁止" in result.answer or "不得" in result.answer
    assert result.compliance and result.compliance.passed


def test_composite_question_uses_multiple_collections(container) -> None:
    result = asyncio.run(
        container.answerer.answer(
            "我想买点基金，消费板块的怎么样？",
            UserProfile(user_id="test", risk_level="C3"),
            "test-composite",
        )
    )
    assert result.strategy == "subquery"
    titles = " ".join(citation.title for citation in result.citations)
    assert "消费" in titles
    assert "基金" in titles or "研报" in titles


def test_suitability_filter_excludes_high_risk_fund(container) -> None:
    result = asyncio.run(
        container.answerer.answer(
            "我风险承受能力是C2，推荐几只基金",
            UserProfile(user_id="test", risk_level="C2"),
            "test-risk",
        )
    )
    titles = " ".join(citation.title for citation in result.citations)
    assert "消费主题" not in titles


def test_catalog_question_does_not_fabricate_unknown_product(container) -> None:
    result = asyncio.run(
        container.answerer.answer(
            "好医保有哪些险种",
            UserProfile(user_id="test", risk_level="C3"),
            "test-catalog",
        )
    )
    assert result.strategy == "catalog"
    assert "未收录" in result.answer
    assert "平安福" in result.answer
    assert len(result.citations) >= 3


def test_low_confidence_gate_returns_no_citations(container) -> None:
    result = asyncio.run(
        container.answerer.answer(
            "量子纠缠对保险理赔有什么影响",
            UserProfile(user_id="test", risk_level="C3"),
            "test-low-confidence",
        )
    )
    assert result.confidence < 0.80
    assert result.citations == []
    assert "80%" not in result.answer
    assert "我理解" in result.answer
    assert result.strategy == "clarification"


def test_missing_insurance_limit_asks_for_intent_clarification(container) -> None:
    result = asyncio.run(
        container.answerer.answer(
            "意外险的投保上限？",
            UserProfile(user_id="test", risk_level="C3"),
            "test-insurance-limit",
        )
    )
    assert "最高投保上限" in result.answer or "可投保范围" in result.answer
    assert "产品名称" in result.answer
    assert "80%" not in result.answer


def test_fund_name_uses_fund_specific_clarification(container) -> None:
    result = asyncio.run(
        container.answerer.answer(
            "易方达蓝筹精选混合",
            UserProfile(user_id="test", risk_level="C3"),
            "test-fund-name",
        )
    )
    assert "基金产品信息" in result.answer
    assert "基金经理" in result.answer
    assert "投保年龄" not in result.answer


def test_domain_filter_and_regulation_exemption(container) -> None:
    answerer = container.answerer
    assert answerer._apply_domain(
        ["fund", "insurance", "bond", "bank_wealth"],
        "product_consultation",
        "insurance",
    ) == ["insurance"]
    assert answerer._apply_domain(
        ["macro"], "compliance", "bank_wealth"
    ) == ["macro"]
    assert answerer._domain_applied(
        ["insurance"], "insurance", "product_consultation"
    ) is True
    assert answerer._domain_applied(
        ["macro"], "bank_wealth", "compliance"
    ) is False


def test_profile_rewrite_and_cache_key_are_profile_aware(container) -> None:
    low = UserProfile(
        user_id="low",
        risk_level="C2",
        investment_experience="1年以内",
        horizon="3个月以内",
        investable_assets=300_000,
        profile_version=1,
    )
    high = UserProfile(
        user_id="high",
        risk_level="C5",
        investment_experience="5年以上",
        horizon="5年以上",
        investable_assets=5_000_000,
        profile_version=1,
    )
    low_plan = container.planner.plan("推荐几只基金", [], low)
    high_plan = container.planner.plan("推荐几只基金", [], high)
    assert low_plan.queries[0] == "推荐几只基金"
    assert high_plan.queries[0] == "推荐几只基金"
    assert low_plan.profile_hints
    assert high_plan.profile_hints
    assert any("低风险" in hint for hint in low_plan.profile_hints)
    assert any("权益类" in hint for hint in high_plan.profile_hints)
    assert container.answerer._cache_key(
        "推荐几只基金", low
    ) != container.answerer._cache_key("推荐几只基金", high)
