from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import UserProfile


@dataclass(frozen=True)
class ProfileBucket:
    """用于缓存和分群的画像桶，避免把精确资产等敏感值放进缓存键。"""

    risk: str
    experience: str
    horizon: str
    assets: str

    def key(self) -> str:
        return f"{self.risk}|{self.experience}|{self.horizon}|{self.assets}"


def experience_band(profile: UserProfile) -> str:
    value = (profile.investment_experience or "").strip().lower()
    if value in {"", "没有经验", "1年以内", "1年以下", "新手", "0-1年"}:
        return "novice"
    if "5年以上" in value or "10年" in value or "资深" in value:
        return "expert"
    return "intermediate"


def horizon_band(profile: UserProfile) -> str:
    value = (profile.horizon or "").strip().lower()
    if any(token in value for token in ("3个月", "半年", "1年以内", "短期", "随时")):
        return "short"
    if any(token in value for token in ("5年以上", "长期", "养老", "传承")):
        return "long"
    return "medium"


def asset_band(profile: UserProfile) -> str:
    assets = profile.investable_assets
    if assets is None or assets <= 0:
        return "unknown"
    if assets < 500_000:
        return "under_50w"
    if assets < 3_000_000:
        return "50_300w"
    if assets < 6_000_000:
        return "300_600w"
    return "over_600w"


def profile_bucket(profile: UserProfile) -> ProfileBucket:
    return ProfileBucket(
        risk=(profile.risk_level or "C3").upper(),
        experience=experience_band(profile),
        horizon=horizon_band(profile),
        assets=asset_band(profile),
    )


def profile_fingerprint(profile: UserProfile) -> str:
    """缓存键中的画像指纹：分桶 + 版本 + 租户，不包含精确资产金额。"""
    tenant = (profile.tenant_id or "default").strip().lower()
    version = max(0, int(profile.profile_version or 0))
    return f"{tenant}|{profile_bucket(profile).key()}|v{version}"


def profile_snapshot(profile: UserProfile) -> dict[str, Any]:
    """供审计回放使用，只保存分档，不保存精确资产和自由文本 PII。"""
    bucket = profile_bucket(profile)
    return {
        "tenant_id": profile.tenant_id or "default",
        "risk_level": bucket.risk,
        "experience_band": bucket.experience,
        "horizon_band": bucket.horizon,
        "asset_band": bucket.assets,
        "profile_version": int(profile.profile_version or 0),
    }


def profile_query_hints(
    question: str, profile: UserProfile, intent: str
) -> list[str]:
    """把画像转成受控检索扩展词，保留原始问题，不交给 LLM 自由改写。"""
    if intent == "compliance":
        return []

    bucket = profile_bucket(profile)
    hints: list[str] = []
    fund_like = any(
        token in question
        for token in ("基金", "理财", "股票", "债券", "ETF", "指数", "混合")
    )
    insurance_like = any(
        token in question
        for token in (
            "保险",
            "医疗险",
            "重疾险",
            "寿险",
            "意外险",
            "年金险",
            "条款",
            "保障",
            "理赔",
            "等待期",
            "免责",
            "免赔额",
        )
    )
    if fund_like:
        if bucket.risk in {"C1", "C2"}:
            hints.append("低风险 货币基金 债券基金 R1 R2 适当性")
        elif bucket.risk == "C3":
            hints.append("中低风险 混合基金 债券基金 R1 R2 R3 适当性")
        else:
            hints.append("权益类 混合基金 R4 R5 风险收益特征")
    elif insurance_like:
        hints.append("保险产品 保障责任 等待期 免责 适当性")
    else:
        hints.append(f"风险等级 {bucket.risk} 适当性")

    if fund_like and bucket.horizon == "short":
        hints.append("短期 流动性 货币基金 短债")
    elif fund_like and bucket.horizon == "long":
        hints.append("长期 3-5年 资产配置")

    if bucket.experience == "novice":
        hints.append("通俗解释 费用 风险提示")
    elif bucket.experience == "expert":
        hints.append(
            "最大回撤 夏普比率 基金经理 持仓"
            if fund_like
            else "条款细节 理赔条件 责任免除"
        )

    if fund_like and bucket.assets == "under_50w":
        hints.append("起投金额 低门槛")
    elif fund_like and bucket.assets == "over_600w":
        hints.append("资产配置 组合分散")

    # 控制 query 长度，避免把检索扩展成噪声。
    return hints[:4]


def profile_query_suffix(question: str, profile: UserProfile, intent: str) -> str:
    hints = profile_query_hints(question, profile, intent)
    return " ".join(hints)
