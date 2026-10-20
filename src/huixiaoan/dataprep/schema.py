from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

ASSET_CLASSES = {"equity", "fund", "insurance", "bond", "bank_wealth", "macro"}
ASSET_CLASS_LABELS = {
    "equity": "股票",
    "fund": "基金",
    "insurance": "保险",
    "bond": "债券",
    "bank_wealth": "银行理财",
    "macro": "宏观经济",
}
INTENTS = {
    "product_consultation",
    "market_analysis",
    "investment_strategy",
    "risk_assessment",
    "claim",
    "compliance",
    "other",
}

SYSTEM_PROMPT = (
    "你是合规优先的金融理财顾问助手。回答必须："
    "1) 区分事实、分析和建议；2) 不承诺收益、不保本、不使用绝对化表述；"
    "3) 推荐产品时先判断用户风险等级与产品风险等级是否匹配；"
    "4) 涉及产品时提示“过往业绩不代表未来表现”，并附风险提示；"
    "5) 资料不足时明确说明，不编造数据、产品条款或收益。"
)


@dataclass
class SFTRecord:
    id: str
    instruction: str
    output: str
    input: str = ""
    asset_class: str = "macro"
    intent: str = "other"
    risk_level: str = "C3"
    source: str = ""
    source_id: str = ""
    publish_date: str = ""
    license: str = ""
    language: str = "zh"
    quality_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SFTRecord":
        instruction = str(
            payload.get("instruction")
            or payload.get("question")
            or payload.get("prompt")
            or ""
        ).strip()
        output = str(
            payload.get("output")
            or payload.get("answer")
            or payload.get("response")
            or ""
        ).strip()
        if not instruction or not output:
            raise ValueError("记录必须包含 instruction 和 output")
        raw_id = str(payload.get("id") or "").strip()
        record_id = raw_id or hashlib.sha1(
            f"{instruction}|{output}".encode("utf-8")
        ).hexdigest()[:20]
        known = {
            "id", "instruction", "output", "input", "asset_class", "intent",
            "risk_level", "source", "source_id", "publish_date", "license",
            "language", "quality_score", "metadata", "question", "answer",
            "prompt", "response",
        }
        metadata = dict(payload.get("metadata") or {})
        for key, value in payload.items():
            if key not in known:
                metadata.setdefault(key, value)
        return cls(
            id=record_id,
            instruction=instruction,
            output=output,
            input=str(payload.get("input") or "").strip(),
            asset_class=str(payload.get("asset_class") or "macro").strip().lower(),
            intent=str(payload.get("intent") or "other").strip(),
            risk_level=str(payload.get("risk_level") or "C3").strip().upper(),
            source=str(payload.get("source") or "").strip(),
            source_id=str(payload.get("source_id") or record_id).strip(),
            publish_date=str(payload.get("publish_date") or "").strip(),
            license=str(payload.get("license") or "").strip(),
            language=str(payload.get("language") or "zh").strip(),
            quality_score=float(payload.get("quality_score") or 0.0),
            metadata=metadata,
        )

    def validate(self) -> None:
        if self.asset_class not in ASSET_CLASSES:
            raise ValueError(f"非法 asset_class: {self.asset_class}")
        if self.intent not in INTENTS:
            raise ValueError(f"非法 intent: {self.intent}")
        if len(self.instruction) < 4:
            raise ValueError("instruction 过短")
        if len(self.output) < 20:
            raise ValueError("output 过短")
        if len(self.instruction) > 2000 or len(self.output) > 12000:
            raise ValueError("instruction/output 超长")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instruction": self.instruction,
            "input": self.input,
            "output": self.output,
            "asset_class": self.asset_class,
            "intent": self.intent,
            "risk_level": self.risk_level,
            "source": self.source,
            "source_id": self.source_id,
            "publish_date": self.publish_date,
            "license": self.license,
            "language": self.language,
            "quality_score": round(self.quality_score, 2),
            "metadata": self.metadata,
        }

    def to_chat_messages(self) -> dict[str, Any]:
        user_content = self.instruction
        if self.input:
            user_content = f"{user_content}\n\n【补充信息】\n{self.input}"
        return {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": self.output},
            ],
            "metadata": {
                "id": self.id,
                "asset_class": self.asset_class,
                "intent": self.intent,
                "risk_level": self.risk_level,
                "source_id": self.source_id,
                "quality_score": round(self.quality_score, 2),
            },
        }
