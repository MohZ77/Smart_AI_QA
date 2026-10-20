from __future__ import annotations

from typing import Any

from .types import UserProfile

QUESTIONNAIRE: list[dict[str, Any]] = [
    {
        "id": "experience",
        "question": "您的投资经验有多久？",
        "options": [
            {"label": "没有经验", "score": 1},
            {"label": "1年以内", "score": 2},
            {"label": "1-3年", "score": 3},
            {"label": "3-5年", "score": 4},
            {"label": "5年以上", "score": 5},
        ],
    },
    {
        "id": "horizon",
        "question": "这笔资金预计投资多久？",
        "options": [
            {"label": "3个月以内", "score": 1},
            {"label": "3-12个月", "score": 2},
            {"label": "1-3年", "score": 3},
            {"label": "3-5年", "score": 4},
            {"label": "5年以上", "score": 5},
        ],
    },
    {
        "id": "assets",
        "question": "您的可投资资产规模大致是？",
        "options": [
            {"label": "10万以下", "score": 1},
            {"label": "10-50万", "score": 2},
            {"label": "50-100万", "score": 3},
            {"label": "100-300万", "score": 4},
            {"label": "300万以上", "score": 5},
        ],
    },
    {
        "id": "income_stability",
        "question": "您的收入稳定性如何？",
        "options": [
            {"label": "不稳定", "score": 1},
            {"label": "较不稳定", "score": 2},
            {"label": "一般", "score": 3},
            {"label": "较稳定", "score": 4},
            {"label": "非常稳定", "score": 5},
        ],
    },
    {
        "id": "drawdown",
        "question": "您能接受的最大本金回撤是？",
        "options": [
            {"label": "不能接受亏损", "score": 1},
            {"label": "5%以内", "score": 2},
            {"label": "5%-10%", "score": 3},
            {"label": "10%-20%", "score": 4},
            {"label": "20%以上", "score": 5},
        ],
    },
    {
        "id": "goal",
        "question": "您的主要投资目标是？",
        "options": [
            {"label": "保本保息", "score": 1},
            {"label": "稳健增值", "score": 2},
            {"label": "平衡增长", "score": 3},
            {"label": "较高增长", "score": 4},
            {"label": "追求高收益", "score": 5},
        ],
    },
    {
        "id": "loss_reaction",
        "question": "如果投资一个月亏损10%，您会？",
        "options": [
            {"label": "立即全部赎回", "score": 1},
            {"label": "赎回大部分", "score": 2},
            {"label": "继续观察", "score": 3},
            {"label": "少量补仓", "score": 4},
            {"label": "继续加仓", "score": 5},
        ],
    },
    {
        "id": "knowledge",
        "question": "您对金融产品的熟悉程度？",
        "options": [
            {"label": "只了解存款", "score": 1},
            {"label": "了解货币基金/理财", "score": 2},
            {"label": "了解债券基金", "score": 3},
            {"label": "了解混合基金/股票", "score": 4},
            {"label": "熟悉衍生品/另类投资", "score": 5},
        ],
    },
    {
        "id": "liquidity",
        "question": "未来一年您对这笔资金的流动性需求？",
        "options": [
            {"label": "随时可能使用", "score": 1},
            {"label": "半年内可能使用", "score": 2},
            {"label": "一年内可能使用", "score": 3},
            {"label": "三年内不用", "score": 4},
            {"label": "五年内不用", "score": 5},
        ],
    },
    {
        "id": "self_rating",
        "question": "您的风险偏好自评？",
        "options": [
            {"label": "保守型", "score": 1},
            {"label": "稳健型", "score": 2},
            {"label": "平衡型", "score": 3},
            {"label": "成长型", "score": 4},
            {"label": "进取型", "score": 5},
        ],
    },
]

_ASSET_MIDPOINTS = {1: 5.0, 2: 30.0, 3: 75.0, 4: 200.0, 5: 500.0}


def assess_profile(answers: dict[str, int], user_id: str = "anonymous") -> tuple[UserProfile, float]:
    """把 10 题问卷答案映射为 C1-C5 用户画像。"""
    missing = [item["id"] for item in QUESTIONNAIRE if item["id"] not in answers]
    if missing:
        raise ValueError(f"缺少问卷答案: {', '.join(missing)}")
    values: dict[str, int] = {}
    for item in QUESTIONNAIRE:
        score = int(answers[item["id"]])
        if score < 1 or score > 5:
            raise ValueError(f"{item['id']} 的答案必须在 1-5 之间")
        values[item["id"]] = score

    average = sum(values.values()) / len(values)
    if average <= 1.8:
        risk_level = "C1"
    elif average <= 2.6:
        risk_level = "C2"
    elif average <= 3.4:
        risk_level = "C3"
    elif average <= 4.2:
        risk_level = "C4"
    else:
        risk_level = "C5"

    experience = _option_label("experience", values["experience"])
    horizon = _option_label("horizon", values["horizon"])
    return (
        UserProfile(
            user_id=user_id,
            risk_level=risk_level,
            investment_experience=experience,
            horizon=horizon,
            investable_assets=_ASSET_MIDPOINTS[values["assets"]],
        ),
        average,
    )


def _option_label(question_id: str, score: int) -> str:
    for item in QUESTIONNAIRE:
        if item["id"] != question_id:
            continue
        for option in item["options"]:
            if option["score"] == score:
                return str(option["label"])
    return ""
