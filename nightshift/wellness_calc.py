"""健康档案的热量估算纯函数。

这里的数字都是生活记录用的估算，不是诊断或医疗建议。模块不读写文件，
也不调用模型，因此在没有网络时仍可用于手动记录。
"""

from __future__ import annotations

import math
from datetime import date
from numbers import Real
from typing import Any, Iterable, Mapping

__all__ = [
    "ACTIVITY_FACTORS",
    "ProfileValidationError",
    "activity_factor",
    "calculate_bmr",
    "calculate_goal",
    "calculate_targets",
    "calculate_tdee",
    "generate_plan",
    "bmr",
    "tdee",
    "goal",
    "validate",
    "calculate_bmi",
    "daily_summary",
    "calculate_summary",
    "summary_for_day",
    "mifflin_st_jeor",
    "profile_summary",
    "safety_warnings",
    "validate_profile",
]


class ProfileValidationError(ValueError):
    """档案缺少必要字段或包含不合理的数值。"""


ACTIVITY_FACTORS = {
    "sedentary": 1.20,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.90,
}

_ACTIVITY_ALIASES = {
    "静坐": "sedentary", "久坐": "sedentary", "sedentary": "sedentary",
    "轻度": "light", "轻度活动": "light", "light": "light",
    "中度": "moderate", "中度活动": "moderate", "moderate": "moderate",
    "高度": "active", "高度活动": "active", "active": "active",
    "非常高": "very_active", "运动员": "very_active", "very_active": "very_active",
}
_SEX_ALIASES = {
    "m": "male", "man": "male", "male": "male", "男": "male", "男性": "male",
    "f": "female", "woman": "female", "female": "female", "女": "female", "女性": "female",
}


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    """把有限的实数转换为 float，并拒绝 bool 和非正数。"""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ProfileValidationError(f"{name} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or result <= minimum:
        raise ProfileValidationError(f"{name} 必须大于 {minimum}")
    return result


def _today(value: date | None) -> date:
    if value is None:
        return date.today()
    if not isinstance(value, date):
        raise TypeError("today 必须是 datetime.date")
    return value


def validate_profile(profile: Mapping[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """校验并规范化档案，返回副本，不修改调用方对象。

    允许前端使用 ``gender``/``activity`` 等常见别名，但返回值始终包含
    ``sex``、``activity_level`` 和以公制表示的身高体重。
    """
    if not isinstance(profile, Mapping):
        raise ProfileValidationError("档案必须是对象")
    now = _today(today)
    source = dict(profile)
    sex_value = source.get("sex", source.get("biological_sex", source.get("gender")))
    if not isinstance(sex_value, str) or sex_value.strip().lower() not in _SEX_ALIASES:
        raise ProfileValidationError("sex 必须是 male 或 female")
    sex = _SEX_ALIASES[sex_value.strip().lower()]
    activity_value = source.get("activity_level", source.get("activity"))
    if not isinstance(activity_value, str) or activity_value.strip().lower() not in _ACTIVITY_ALIASES:
        raise ProfileValidationError("activity_level 不是支持的活动等级")
    activity_level = _ACTIVITY_ALIASES[activity_value.strip().lower()]

    year = source.get("birth_year")
    if isinstance(year, bool) or not isinstance(year, int):
        raise ProfileValidationError("birth_year 必须是整数")
    age = now.year - year
    if year < 1900 or year > now.year or age < 13 or age > 120:
        raise ProfileValidationError("birth_year 超出支持范围")
    height = _number(source.get("height_cm"), "height_cm")
    weight = _number(source.get("weight_kg"), "weight_kg")
    target = _number(source.get("target_weight_kg", weight), "target_weight_kg")
    if not 80 <= height <= 260:
        raise ProfileValidationError("height_cm 必须在 80 到 260 之间")
    if not 20 <= weight <= 500 or not 20 <= target <= 500:
        raise ProfileValidationError("体重必须在 20 到 500 公斤之间")
    result = source
    result.update({
        "sex": sex,
        "activity_level": activity_level,
        "birth_year": year,
        "age": age,
        "height_cm": height,
        "weight_kg": weight,
        "target_weight_kg": target,
    })
    return result


def activity_factor(level: str) -> float:
    """返回活动等级对应的 TDEE 系数。"""
    if not isinstance(level, str) or level.strip().lower() not in _ACTIVITY_ALIASES:
        raise ValueError("未知活动等级")
    return ACTIVITY_FACTORS[_ACTIVITY_ALIASES[level.strip().lower()]]


def mifflin_st_jeor(*, sex: str, weight_kg: float, height_cm: float, age: int) -> float:
    """按 Mifflin–St Jeor 公式计算 BMR（千卡/日）。"""
    normalized = _SEX_ALIASES.get(str(sex).strip().lower())
    if normalized is None:
        raise ValueError("sex 必须是 male 或 female")
    weight = _number(weight_kg, "weight_kg")
    height = _number(height_cm, "height_cm")
    if isinstance(age, bool) or not isinstance(age, int) or age <= 0:
        raise ValueError("age 必须是正整数")
    offset = 5 if normalized == "male" else -161
    return round(10 * weight + 6.25 * height - 5 * age + offset, 1)


def calculate_bmr(profile: Mapping[str, Any], *, today: date | None = None) -> float:
    """从档案计算 BMR。"""
    normalized = validate_profile(profile, today=today)
    return mifflin_st_jeor(
        sex=normalized["sex"], weight_kg=normalized["weight_kg"],
        height_cm=normalized["height_cm"], age=normalized["age"],
    )


def calculate_tdee(profile: Mapping[str, Any], bmr: float | None = None, *, today: date | None = None) -> float:
    """按档案活动等级计算 TDEE。"""
    normalized = validate_profile(profile, today=today)
    base = calculate_bmr(normalized, today=today) if bmr is None else _number(bmr, "bmr")
    return round(base * ACTIVITY_FACTORS[normalized["activity_level"]], 1)


def _bmi(weight: float, height: float) -> float:
    return round(weight / ((height / 100) ** 2), 1)


def calculate_bmi(weight_kg: float, height_cm: float) -> float:
    """计算 BMI，供展示使用。"""
    weight = _number(weight_kg, "weight_kg")
    height = _number(height_cm, "height_cm")
    return _bmi(weight, height)


def safety_warnings(profile: Mapping[str, Any], goal: Mapping[str, Any] | None = None, *, today: date | None = None) -> list[str]:
    """生成软提醒；任何提醒都不会替用户做医疗判断。"""
    normalized = validate_profile(profile, today=today)
    warnings: list[str] = []
    current_bmi = _bmi(normalized["weight_kg"], normalized["height_cm"])
    target_bmi = _bmi(normalized["target_weight_kg"], normalized["height_cm"])
    if current_bmi < 18.5:
        warnings.append("当前 BMI 偏低，减重目标应先咨询专业人士")
    if target_bmi < 18.5:
        warnings.append("目标 BMI 偏低，不自动生成减重热量缺口")
    if goal:
        target_calories = goal.get("calorie_target", goal.get("target_calories"))
        tdee = goal.get("tdee")
        if isinstance(target_calories, Real) and isinstance(tdee, Real) and tdee > 0:
            if target_calories < (1500 if normalized["sex"] == "male" else 1200):
                warnings.append("建议摄入低于保守下限，请咨询专业人士")
            if (tdee - target_calories) / tdee > 0.20:
                warnings.append("热量缺口超过 TDEE 的 20%，不建议自行执行")
    if normalized.get("pregnant_or_breastfeeding") or normalized.get("eating_disorder_risk") or normalized.get("medical_condition"):
        warnings.append("孕哺、疾病或进食风险标记存在，不自动计算减重预算")
    return warnings


def calculate_goal(profile: Mapping[str, Any], *, today: date | None = None,
                   deficit_kcal: float | None = None) -> dict[str, Any]:
    """计算维护热量和保守摄入目标，并附带安全提醒。"""
    normalized = validate_profile(profile, today=today)
    bmr = calculate_bmr(normalized, today=today)
    tdee = calculate_tdee(normalized, bmr=bmr, today=today)
    maintenance = round(tdee)
    # 默认约 10%（至少 250，最多 500）的缓慢缺口。
    # 任何自定义缺口也要同时受 500 千卡和 TDEE 20% 双重护栏约束。
    reducing = normalized["target_weight_kg"] < normalized["weight_kg"]
    maximum_deficit = min(500.0, tdee * 0.20)
    requested = normalized.get("deficit_kcal") if deficit_kcal is None else deficit_kcal
    warnings: list[str] = []
    if requested is not None:
        if isinstance(requested, bool) or not isinstance(requested, Real) or not math.isfinite(float(requested)) or float(requested) < 0:
            raise ProfileValidationError("deficit_kcal 必须是非负有限数字")
        deficit = float(requested)
        if deficit > maximum_deficit:
            deficit = maximum_deficit
            warnings.append("热量缺口已限制在 500 千卡及 TDEE 的 20% 以内")
    else:
        deficit = min(maximum_deficit, max(250.0, tdee * 0.10))
    if not reducing:
        deficit = 0.0
    proposed = round(tdee - deficit)
    floor = 1500 if normalized["sex"] == "male" else 1200
    target_bmi = _bmi(normalized["target_weight_kg"], normalized["height_cm"])
    if _bmi(normalized["weight_kg"], normalized["height_cm"]) < 18.5:
        warnings.append("当前 BMI 偏低，减重目标应先咨询专业人士")
    if target_bmi < 18.5:
        warnings.append("目标 BMI 偏低，不自动生成减重热量缺口")
    # 低于保守地板时优先钳到地板，不因这一项提醒直接跳回维护热量。
    if reducing and proposed < floor:
        proposed = min(maintenance, floor)
        warnings.append("建议摄入低于保守下限，请咨询专业人士")
    if normalized.get("pregnant_or_breastfeeding") or normalized.get("eating_disorder_risk") or normalized.get("medical_condition"):
        warnings.append("孕哺、疾病或进食风险标记存在，不自动计算减重预算")
    # BMI/医疗风险属于硬安全信号，只有这些情况回到维护热量；普通地板或
    # 自定义缺口过大则保留经过钳制的目标，方便用户看见实际护栏。
    critical_warning = (
        _bmi(normalized["weight_kg"], normalized["height_cm"]) < 18.5
        or target_bmi < 18.5
        or bool(normalized.get("pregnant_or_breastfeeding") or normalized.get("eating_disorder_risk") or normalized.get("medical_condition"))
    )
    if critical_warning:
        proposed = maintenance
    result = {
        "bmr": round(bmr), "tdee": maintenance, "maintenance_calories": maintenance,
        "calorie_target": proposed, "target_calories": proposed,
        "deficit_calories": max(0, maintenance - proposed),
        "deficit_percent": round(max(0, maintenance - proposed) / maintenance, 3),
        "target_bmi": target_bmi, "safe_to_cut": not bool(warnings),
        "warnings": warnings,
    }
    return result


def calculate_targets(profile: Mapping[str, Any], *, today: date | None = None,
                      deficit_kcal: float | None = None) -> dict[str, Any]:
    """calculate_goal 的语义别名，便于 API 使用。"""
    return calculate_goal(profile, today=today, deficit_kcal=deficit_kcal)


# 短名称是 API 适配层和命令行调用的稳定别名。
bmr = calculate_bmr
tdee = calculate_tdee
goal = calculate_goal
validate = validate_profile


def profile_summary(profile: Mapping[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """返回档案、BMI 和热量目标的组合视图。"""
    normalized = validate_profile(profile, today=today)
    result = dict(normalized)
    result["bmi"] = _bmi(normalized["weight_kg"], normalized["height_cm"])
    result["goal"] = calculate_goal(normalized, today=today)
    return result


# 首版离线餐单模板。热量是常见份量的估算，生成后仍应允许用户修改；模板不
# 依赖外部营养数据库，也不把模型猜测写入正式记录。
_MEAL_TEMPLATES: dict[str, tuple[dict[str, Any], ...]] = {
    "早餐": (
        {"title": "燕麦牛奶香蕉", "ingredients": (("燕麦", "主食", 220), ("牛奶", "蛋奶", 100), ("香蕉", "水果", 90))},
        {"title": "鸡蛋全麦吐司番茄", "ingredients": (("鸡蛋", "蛋奶", 140), ("全麦吐司", "主食", 180), ("番茄", "蔬菜", 35))},
        {"title": "豆腐小白菜粥", "ingredients": (("大米", "主食", 230), ("豆腐", "豆类", 120), ("小白菜", "蔬菜", 30))},
    ),
    "午餐": (
        {"title": "鸡肉糙米西兰花", "ingredients": (("糙米", "主食", 260), ("鸡胸肉", "肉禽", 260), ("西兰花", "蔬菜", 50))},
        {"title": "豆腐杂粮饭青菜", "ingredients": (("杂粮饭", "主食", 280), ("豆腐", "豆类", 180), ("上海青", "蔬菜", 45))},
        {"title": "三文鱼土豆菠菜", "ingredients": (("土豆", "主食", 230), ("三文鱼", "海鲜", 280), ("菠菜", "蔬菜", 35))},
    ),
    "晚餐": (
        {"title": "虾仁白菜米饭", "ingredients": (("米饭", "主食", 240), ("虾仁", "海鲜", 180), ("大白菜", "蔬菜", 45))},
        {"title": "鸡肉南瓜蘑菇", "ingredients": (("鸡腿肉", "肉禽", 280), ("南瓜", "蔬菜", 120), ("蘑菇", "蔬菜", 40))},
        {"title": "豆腐荞麦面胡萝卜", "ingredients": (("荞麦面", "主食", 270), ("豆腐", "豆类", 160), ("胡萝卜", "蔬菜", 45))},
    ),
}


def _preference_sets(preferences: Mapping[str, Any] | None) -> tuple[set[str], set[str], set[str]]:
    """提取禁用、喜爱和硬约束词，返回小写字符串集合。"""
    blocked: set[str] = set()
    loved: set[str] = set()
    rules: set[str] = set()
    if not isinstance(preferences, Mapping):
        return blocked, loved, rules

    def add_values(target: set[str], value: Any) -> None:
        if isinstance(value, Mapping):
            values = value.keys()
        elif isinstance(value, (list, tuple, set)):
            values = value
        elif isinstance(value, str):
            values = (value,)
        else:
            values = ()
        for item in values:
            if isinstance(item, str) and item.strip():
                target.add(item.strip().lower())

    items = preferences.get("items", preferences.get("categories", {}))
    if isinstance(items, Mapping):
        # 同时兼容 {分类: {食材: 档位}} 和 {食材: 档位} 两种存法。
        for category, values in items.items():
            if isinstance(values, Mapping):
                for food, level in values.items():
                    if not isinstance(food, str) or not isinstance(level, str):
                        continue
                    if level == "avoid":
                        blocked.add(food.strip().lower())
                    elif level == "love":
                        loved.add(food.strip().lower())
            elif isinstance(values, str) and category:
                if values == "avoid":
                    blocked.add(str(category).strip().lower())
                elif values == "love":
                    loved.add(str(category).strip().lower())
    for key in ("allergies", "avoid"):
        add_values(blocked, preferences.get(key))
    raw_rules = preferences.get("dietary_rules")
    if isinstance(raw_rules, Mapping):
        add_values(rules, raw_rules.keys())
        for key in ("avoid", "allergies", "excluded"):
            add_values(blocked, raw_rules.get(key))
    else:
        add_values(rules, raw_rules)
    # 常见硬规则只用于扩大禁用类别，不会尝试解释任意自然语言。
    if any("素" in item or "vegetarian" in item.lower() for item in rules):
        blocked.update({"鸡胸肉", "鸡腿肉", "三文鱼", "虾仁"})
    if any("不吃海鲜" in item or "no seafood" in item.lower() for item in rules):
        blocked.update({"三文鱼", "虾仁"})
    return blocked, loved, rules


def _food_matches(food: str, terms: set[str]) -> bool:
    name = food.lower()
    return any(term in name or name in term for term in terms)


def generate_plan(day: str, payload: Mapping[str, Any] | None = None,
                  profile: Mapping[str, Any] | None = None,
                  preferences: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """按固定模板生成早餐、午餐和晚餐，不依赖网络或模型。

    选择顺序是硬禁用过滤、喜欢食材加分、模板原始顺序兜底。各餐热量按目标
    比例缩放，三餐总和与目标相等（四舍五入误差不超过 1 千卡）。
    """
    if not isinstance(day, str) or not day.strip():
        raise ValueError("day 必须是非空日期字符串")
    payload = payload if isinstance(payload, Mapping) else {}
    profile = profile if isinstance(profile, Mapping) else {}
    preferences = preferences if isinstance(preferences, Mapping) else {}
    used_default_budget = False
    target_value = payload.get("calorie_target", payload.get("target_calories", payload.get("target_kcal", payload.get("budget_kcal", payload.get("daily_target_kcal")))))
    if not isinstance(target_value, Real) or isinstance(target_value, bool) or not math.isfinite(float(target_value)) or float(target_value) <= 0:
        target_value = profile.get("daily_target_kcal", profile.get("calorie_budget"))
    if not isinstance(target_value, Real) or isinstance(target_value, bool) or not math.isfinite(float(target_value)) or float(target_value) <= 0:
        try:
            target_value = calculate_goal(profile)["calorie_target"]
        except (TypeError, ValueError, KeyError):
            target_value = 1800.0
            used_default_budget = True
    target = max(1.0, float(target_value))
    blocked, loved, _rules = _preference_sets(preferences)
    meals: list[dict[str, Any]] = []
    ratios = (0.25, 0.40, 0.35)
    for (meal_name, templates), ratio in zip(_MEAL_TEMPLATES.items(), ratios):
        usable: list[tuple[int, int, dict[str, Any]]] = []
        for index, template in enumerate(templates):
            ingredients = template["ingredients"]
            # 硬避开既检查具体食材名，也检查分类名；例如 avoid=海鲜时，
            # 虾仁和三文鱼模板都不能通过，即使用户没有逐个列出它们。
            if any(_food_matches(food, blocked) or _food_matches(category, blocked)
                   for food, category, _kcal in ingredients):
                continue
            love_score = sum(1 for food, _category, _kcal in ingredients if _food_matches(food, loved))
            usable.append((love_score, -index, template))
        selected = max(usable, key=lambda item: (item[0], item[1]))[2] if usable else None
        meal_target = round(target * ratio)
        if selected is None:
            meals.append({
                "meal": meal_name, "title": meal_name, "name": "按硬约束自选的一餐",
                "calories_kcal": meal_target, "ingredients": [],
                "note": "内置模板均含硬禁用食材，请补充可接受食材后重生成。",
            })
            continue
        base = sum(item[2] for item in selected["ingredients"])
        scale = meal_target / base if base else 1.0
        ingredients = [{
            "name": food, "category": category, "portion": "一份",
            "calories_kcal": round(kcal * scale),
        } for food, category, kcal in selected["ingredients"]]
        meals.append({
            "meal": meal_name, "title": selected["title"], "name": selected["title"],
            "calories_kcal": meal_target, "ingredients": ingredients,
            "note": "按内置常见份量估算，可在保存前修改。",
        })
    # 修正四舍五入带来的总和误差，保证计划校验简单且透明。
    difference = round(target) - sum(int(item["calories_kcal"]) for item in meals)
    if meals:
        meals[-1]["calories_kcal"] += difference
    result = {
        "version": 1, "date": day, "target_calories": round(target),
        "total_calories": sum(int(item["calories_kcal"]) for item in meals),
        "meals": meals, "warnings": [], "source": "local_template",
    }
    if used_default_budget:
        message = "档案不完整，使用默认 1800 千卡预算估算；请完善档案后重新生成。"
        result["warnings"] = [message]
        result["notice"] = message
    return result


def _value(record: Mapping[str, Any], *names: str) -> float:
    for name in names:
        value = record.get(name)
        if isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value)):
            return float(value)
    nutrition = record.get("nutrition")
    if isinstance(nutrition, Mapping):
        for name in names:
            value = nutrition.get(name)
            if isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value)):
                return float(value)
    return 0.0


def daily_summary(entries: Mapping[str, Any] | Iterable[Mapping[str, Any]] | str, target: float | Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None, *compat: Mapping[str, Any], date_value: str | None = None, date: str | None = None) -> dict[str, Any]:
    """汇总当天摄入、运动和宏量营养素；缺失记录保持为 0 并标出记录数。

    ``entries`` 可是记录列表，也可是包含 ``records``/``meals``/``exercises``
    的日期文档。这里不会把运动消耗默认加回摄入预算。
    """
    # 兼容早期 HTTP 层调用 ``daily_summary(day, entries, profile)``。
    if isinstance(entries, str) and isinstance(target, Iterable) and not isinstance(target, (str, bytes, Mapping)):
        date_value = entries
        entries = target
        target = None
        if compat and isinstance(compat[0], Mapping):
            profile = compat[0]
            target = profile.get("daily_target_kcal", profile.get("calorie_budget"))
    records: list[Mapping[str, Any]] = []
    if isinstance(entries, Mapping):
        if isinstance(entries.get("records"), list):
            records.extend(x for x in entries["records"] if isinstance(x, Mapping))
        else:
            for key in ("meals", "exercises", "weights", "water"):
                value = entries.get(key, [])
                if isinstance(value, list):
                    records.extend(x for x in value if isinstance(x, Mapping))
    else:
        records.extend(x for x in entries if isinstance(x, Mapping))
    intake = burned = protein = carbs = fat = fiber = 0.0
    latest_weight: float | None = None
    latest_weight_key: str | None = None
    meals = exercises = 0
    for record in records:
        kind = str(record.get("type", record.get("kind", "meal"))).lower()
        if kind in {"weight", "体重"}:
            value = _value(record, "weight_kg", "weight")
            if value > 0:
                stamp = record.get("recorded_at", record.get("created_at", record.get("timestamp", "")))
                stamp = str(stamp)
                if latest_weight is None or stamp >= (latest_weight_key or ""):
                    latest_weight, latest_weight_key = value, stamp
            continue
        is_exercise = kind in {"exercise", "workout", "运动"}
        if is_exercise:
            burned += _value(record, "calories_burned", "burned_kcal", "calories_kcal", "calories")
            exercises += 1
        elif kind in {"meal", "food", "饮食", "餐"} or any(k in record for k in ("calories", "calories_kcal", "kcal")):
            intake += _value(record, "calories_kcal", "calories", "kcal", "energy_kcal")
            protein += _value(record, "protein_g", "protein")
            carbs += _value(record, "carbs_g", "carbohydrates_g", "carbs")
            fat += _value(record, "fat_g", "fat")
            fiber += _value(record, "fiber_g", "fiber")
            meals += 1
    if isinstance(target, Mapping):
        target_value = target.get("calorie_target", target.get("target_calories", target.get("calories")))
    else:
        target_value = target
    budget = float(target_value) if isinstance(target_value, Real) and not isinstance(target_value, bool) else None
    result: dict[str, Any] = {
        "date": date_value if date_value is not None else date,
        "calories_in": round(intake, 1), "intake_kcal": round(intake, 1),
        "calories_out": round(burned, 1), "exercise_kcal": round(burned, 1),
        "net_calories": round(intake - burned, 1),
        "meals_count": meals, "exercise_count": exercises,
        "macros": {"protein_g": round(protein, 1), "carbs_g": round(carbs, 1), "fat_g": round(fat, 1), "fiber_g": round(fiber, 1)},
        "weight_kg": latest_weight,
        "record_count": len(records), "target_calories": round(budget, 1) if budget is not None else None,
        "remaining_calories": round(budget - intake, 1) if budget is not None else None,
    }
    if budget and budget > 0:
        result["progress_percent"] = round(intake / budget * 100, 1)
    else:
        result["progress_percent"] = None
    # API 的旧字段名保留为别名，便于网页和导出数据平滑升级。
    result.update({
        "consumed_kcal": result["calories_in"],
        "net_kcal": result["net_calories"],
        "budget_kcal": result["target_calories"],
        "entry_count": result["record_count"],
    })
    return result


calculate_summary = daily_summary
summary_for_day = daily_summary
