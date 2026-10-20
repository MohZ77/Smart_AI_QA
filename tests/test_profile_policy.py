from __future__ import annotations

from huixiaoan.profile_policy import (
    asset_band,
    experience_band,
    horizon_band,
    profile_bucket,
    profile_fingerprint,
    profile_query_hints,
    profile_snapshot,
)
from huixiaoan.types import UserProfile


def test_profile_bands_and_bucket() -> None:
    profile = UserProfile(
        risk_level="C2",
        investment_experience="1年以内",
        horizon="3个月以内",
        investable_assets=300_000,
        profile_version=3,
        tenant_id="tenant-a",
    )
    bucket = profile_bucket(profile)
    assert bucket.risk == "C2"
    assert bucket.experience == "novice"
    assert bucket.horizon == "short"
    assert bucket.assets == "under_50w"
    assert experience_band(profile) == "novice"
    assert horizon_band(profile) == "short"
    assert asset_band(profile) == "under_50w"


def test_profile_fingerprint_is_bucketed_and_versioned() -> None:
    low = UserProfile(
        risk_level="C2",
        investment_experience="1年以内",
        horizon="3个月以内",
        investable_assets=300_000,
        profile_version=1,
        tenant_id="tenant-a",
    )
    same_bucket_other_amount = UserProfile(
        risk_level="C2",
        investment_experience="1年以内",
        horizon="3个月以内",
        investable_assets=450_000,
        profile_version=1,
        tenant_id="tenant-a",
    )
    changed_version = UserProfile(
        risk_level="C2",
        investment_experience="1年以内",
        horizon="3个月以内",
        investable_assets=300_000,
        profile_version=2,
        tenant_id="tenant-a",
    )
    assert profile_fingerprint(low) == profile_fingerprint(same_bucket_other_amount)
    assert profile_fingerprint(low) != profile_fingerprint(changed_version)


def test_profile_query_hints_are_domain_specific() -> None:
    low = UserProfile(
        risk_level="C2",
        investment_experience="1年以内",
        horizon="3个月以内",
        investable_assets=300_000,
    )
    high = UserProfile(
        risk_level="C5",
        investment_experience="5年以上",
        horizon="5年以上",
        investable_assets=5_000_000,
    )
    low_hints = " ".join(profile_query_hints("推荐几只基金", low, "product_consultation"))
    high_hints = " ".join(profile_query_hints("推荐几只基金", high, "product_consultation"))
    insurance_hints = " ".join(
        profile_query_hints("医疗险的免赔额是多少", low, "claim")
    )
    risk_hints = " ".join(
        profile_query_hints("R4 产品适合什么投资者", low, "risk_assessment")
    )
    assert "低风险" in low_hints
    assert "短期" in low_hints
    assert "起投金额" in low_hints
    assert "权益类" in high_hints
    assert "长期" in high_hints
    assert "最大回撤" in high_hints
    assert "混合基金" not in insurance_hints
    assert "保险产品" not in risk_hints


def test_profile_snapshot_does_not_store_exact_assets() -> None:
    profile = UserProfile(
        risk_level="C4",
        investment_experience="3-5年",
        horizon="1-3年",
        investable_assets=2_000_000,
        profile_version=7,
        tenant_id="tenant-b",
    )
    snapshot = profile_snapshot(profile)
    assert snapshot["risk_level"] == "C4"
    assert snapshot["asset_band"] == "50_300w"
    assert snapshot["profile_version"] == 7
    assert snapshot["tenant_id"] == "tenant-b"
    assert "investable_assets" not in snapshot
