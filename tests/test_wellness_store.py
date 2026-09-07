"""健康数据 JSON 仓库专项测试。"""

import json

import pytest

from nightshift.wellness_store import CorruptDataError, WellnessStore


def test_profile_preferences_entries_and_plan_are_persistent(tmp_path):
    store = WellnessStore(tmp_path)
    profile = {"sex": "female", "height_cm": 165, "weight_kg": 70}
    assert store.save_profile(profile) == profile
    assert store.load_profile() == profile
    assert store.update_profile({"target_weight_kg": 60})["target_weight_kg"] == 60
    assert store.save_preferences({"vegetables": {"avoid": ["celery"]}})
    assert store.load_preferences()["vegetables"]["avoid"] == ["celery"]

    first = store.create_entry("2026-09-07", {"type": "meal", "calories": 500})
    assert first["id"]
    assert store.get_entry("2026-09-07", first["id"])["calories"] == 500
    changed = store.update_entry("2026-09-07", first["id"], {"calories": 550})
    assert changed["calories"] == 550
    assert store.delete_entry("2026-09-07", first["id"]) is True
    assert store.get_entry("2026-09-07", first["id"]) is None

    plan = {"meals": [{"name": "breakfast"}], "total_kcal": 1800}
    assert store.save_plan("2026-09-07", plan) == plan
    assert store.load_plan("2026-09-07") == plan


def test_writes_are_atomic_json_and_missing_data_is_empty(tmp_path):
    store = WellnessStore(tmp_path)
    assert store.list_entries("2026-09-07") == []
    store.save_entries("2026-09-07", [])
    path = tmp_path / "wellness" / "entries" / "2026-09-07.json"
    assert json.loads(path.read_text(encoding="utf-8"))["records"] == []
    assert not list(path.parent.glob("*.tmp"))


def test_corrupt_json_is_not_silently_replaced(tmp_path):
    store = WellnessStore(tmp_path)
    path = tmp_path / "wellness" / "profile.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(CorruptDataError, match="JSON 损坏"):
        store.load_profile()
    assert path.read_text(encoding="utf-8") == "{bad"


def test_dates_are_strict(tmp_path):
    store = WellnessStore(tmp_path)
    with pytest.raises(ValueError):
        store.list_entries("2026-9-7")
    with pytest.raises(ValueError):
        store.list_entries("2026-02-30")
