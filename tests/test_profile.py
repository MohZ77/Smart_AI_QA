from __future__ import annotations

import pytest

from huixiaoan.profile import QUESTIONNAIRE, assess_profile


def _answers(score: int) -> dict[str, int]:
    return {item["id"]: score for item in QUESTIONNAIRE}


def test_low_score_maps_to_conservative() -> None:
    profile, average = assess_profile(_answers(1), "u1")
    assert profile.risk_level == "C1"
    assert average == 1.0


def test_high_score_maps_to_aggressive() -> None:
    profile, average = assess_profile(_answers(5), "u2")
    assert profile.risk_level == "C5"
    assert average == 5.0


def test_missing_answer_raises() -> None:
    with pytest.raises(ValueError):
        assess_profile({}, "u3")
