"""健康热量计算专项测试。"""

from datetime import date

import pytest

from nightshift.wellness_calc import (
    ProfileValidationError,
    calculate_bmr,
    calculate_goal,
    daily_summary,
    generate_plan,
    validate_profile,
)


def profile(**changes):
    value = {
        "sex": "female", "birth_year": 1990, "height_cm": 165,
        "weight_kg": 70, "target_weight_kg": 60, "activity_level": "moderate",
    }
    value.update(changes)
    return value


def test_mifflin_profile_and_tdee_are_transparent():
    normalized = validate_profile(profile(), today=date(2026, 1, 1))
    assert normalized["age"] == 36
    assert calculate_bmr(profile(), today=date(2026, 1, 1)) == pytest.approx(1390.2)
    goal = calculate_goal(profile(), today=date(2026, 1, 1))
    assert goal["tdee"] == 2155
    assert goal["calorie_target"] < goal["tdee"]
    assert goal["warnings"] == []


def test_invalid_profile_is_rejected():
    with pytest.raises(ProfileValidationError):
        validate_profile(profile(height_cm=0))
    with pytest.raises(ProfileValidationError):
        validate_profile(profile(activity_level="unknown"))
    with pytest.raises(ProfileValidationError):
        validate_profile(profile(birth_year=1890))


def test_dangerous_goal_returns_maintenance_and_warning():
    result = calculate_goal(profile(target_weight_kg=40), today=date(2026, 1, 1))
    assert result["safe_to_cut"] is False
    assert result["calorie_target"] == result["tdee"]
    assert result["warnings"]


def test_daily_summary_does_not_automatically_eat_back_exercise():
    result = daily_summary(
        [
            {"type": "meal", "calories": 500, "protein_g": 30},
            {"type": "exercise", "calories_burned": 250},
        ],
        target=1800,
        date_value="2026-09-07",
    )
    assert result["calories_in"] == 500
    assert result["exercise_kcal"] == 250
    assert result["remaining_calories"] == 1300
    assert result["net_calories"] == 250
    assert result["macros"]["protein_g"] == 30


def test_daily_summary_uses_latest_weight_record():
    result = daily_summary([
        {"type": "weight", "weight_kg": 71, "created_at": "2026-09-07T08:00:00Z"},
        {"type": "weight", "weight_kg": 70.5, "created_at": "2026-09-07T20:00:00Z"},
    ])
    assert result["weight_kg"] == 70.5


def test_goal_does_not_create_deficit_when_target_is_not_lower():
    result = calculate_goal(profile(target_weight_kg=70), today=date(2026, 1, 1))
    assert result["deficit_calories"] == 0
    assert result["calorie_target"] == result["tdee"]


def test_custom_deficit_is_capped_and_floor_is_preserved():
    capped = calculate_goal(profile(), today=date(2026, 1, 1), deficit_kcal=2000)
    assert capped["deficit_calories"] <= min(500, capped["tdee"] * 0.20)
    assert capped["warnings"]
    low_tdee = profile(sex="male", height_cm=160, weight_kg=55, target_weight_kg=50, activity_level="sedentary")
    floored = calculate_goal(low_tdee, today=date(2026, 1, 1))
    assert floored["calorie_target"] == 1500
    assert floored["calorie_target"] != floored["tdee"]


def test_local_plan_has_three_meals_and_respects_preferences():
    plan = generate_plan(
        "2026-09-07",
        {"calorie_target": 1800},
        preferences={"items": {"general": {"鸡蛋": "avoid", "豆腐": "love"}}, "allergies": ["虾"]},
    )
    assert [meal["meal"] for meal in plan["meals"]] == ["早餐", "午餐", "晚餐"]
    assert plan["total_calories"] == 1800
    text = repr(plan)
    assert "鸡蛋" not in text
    assert "虾仁" not in text
    assert any("豆腐" in repr(meal) for meal in plan["meals"])


def test_plan_avoid_matches_ingredient_category_and_default_budget_is_visible():
    seafood_blocked = generate_plan(
        "2026-09-07", {"calorie_target": 1800},
        preferences={"items": {"general": {"海鲜": "avoid"}}},
    )
    assert all(
        ingredient["category"] != "海鲜"
        for meal in seafood_blocked["meals"]
        for ingredient in meal["ingredients"]
    )
    fallback = generate_plan("2026-09-07", {}, {})
    assert fallback["calorie_target"] == 1800
    assert fallback["warnings"]
    assert "默认 1800" in fallback["notice"]
