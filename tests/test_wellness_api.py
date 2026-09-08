"""健康 API 的离线校验与存储回退测试，不打开真实端口。"""

import json
from io import BytesIO

import pytest

from nightshift import wellness


def test_profile_validation_accepts_and_rejects_dangerous_values():
    profile = wellness._validate_profile({"height_cm": 170, "weight_kg": 75, "sex": "unspecified"})
    assert profile["height_cm"] == 170
    with pytest.raises(wellness.WellnessError) as exc:
        wellness._validate_profile({"height_cm": 170, "token": "secret"})
    assert exc.value.fields["token"] == "unknown"


def test_date_and_entry_validation():
    assert wellness._validate_date("2026-09-07") == "2026-09-07"
    with pytest.raises(wellness.WellnessError):
        wellness._validate_date("2026-02-30")
    entry = wellness._validate_entry({"type": "exercise", "name": "步行", "duration_min": 30})
    assert entry["type"] == "exercise"
    with pytest.raises(wellness.WellnessError):
        wellness._validate_entry({"type": "medicine", "name": "x"})


def test_fallback_entry_storage_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    day = "2026-09-07"
    entries = [
        {"id": "one", "type": "meal", "name": "午餐", "calories": 600},
        {"id": "two", "type": "exercise", "name": "走路", "calories_burned": 120},
    ]
    wellness._save_entries(day, entries)
    assert wellness._entries(day) == entries
    summary = wellness._summary(day, entries)
    assert summary["calories_in"] == 600
    assert summary["exercise_kcal"] == 120
    assert summary["net_calories"] == 480
    raw = json.loads((tmp_path / "wellness" / "entries" / f"{day}.json").read_text())
    assert raw.get("entries", raw.get("records")) == entries


def test_preference_levels_are_explicit():
    result = wellness._validate_preferences({"items": {"vegetables": {"西兰花": "love"}}})
    assert result["items"]["vegetables"]["西兰花"] == "love"
    with pytest.raises(wellness.WellnessError):
        wellness._validate_preferences({"items": {"vegetables": {"芹菜": "never"}}})


def test_static_path_is_a_fixed_name():
    assert wellness._STATIC_FILES["app.js"] == "app.js"
    assert "../" not in wellness._STATIC_FILES


def test_api_routes_return_stable_envelope_without_socket(tmp_path, monkeypatch):
    """用裸 handler 调路由，避免测试申请真实端口。"""
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))

    def call(method, path, payload=None):
        handler = wellness._Handler.__new__(wellness._Handler)
        handler.path = path
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = BytesIO(raw)
        captured = {}
        handler._send = lambda status, body, content_type="application/json; charset=utf-8": captured.update(
            status=status, body=body, content_type=content_type
        )
        handler._route(method)
        return captured

    response = call("POST", "/health/api/days/2026-09-07/entries", {"type": "meal", "text": "午饭", "calories": 600})
    assert response["status"] == 201
    assert response["body"]["ok"] is True
    entry_id = response["body"]["data"]["id"]
    response = call("GET", "/health/api/days/2026-09-07/entries")
    assert response["body"]["data"]["entries"][0]["id"] == entry_id
    response = call("GET", "/health/api/days/2026-09-07/summary")
    assert response["body"]["data"]["calories_in"] == 600
    response = call("DELETE", f"/health/api/days/2026-09-07/entries/{entry_id}")
    assert response["body"] == {"ok": True, "data": {"deleted": entry_id}}


def test_make_server_only_reads_wellness_http(monkeypatch):
    seen = {}

    class FakeServer:
        def __init__(self, address, handler):
            seen["address"] = address
            seen["handler"] = handler

    monkeypatch.setattr(wellness, "ThreadingHTTPServer", FakeServer)
    wellness.make_server({"http": {"port": 8190}, "wellness": {"http": {"port": 8321}}})
    assert seen["address"] == ("127.0.0.1", 8321)
    wellness.make_server({"http": {"port": 8190}})
    assert seen["address"] == ("127.0.0.1", 8191)
    wellness.make_server({"wellness": {"port": 9999}, "http": {"port": 8190}})
    assert seen["address"] == ("127.0.0.1", 8191)


def test_plan_is_saved_and_does_not_contain_avoid(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    wellness._save_preferences({"categories": {"avoid": ["西兰花"], "love": ["虾仁"]}})
    plan = wellness._plan("2026-09-07", {})
    assert plan["meals"]
    rendered = json.dumps(plan, ensure_ascii=False)
    assert "西兰花" not in rendered
    from nightshift import wellness_store
    assert wellness_store.load_plan("2026-09-07") == plan


def test_unsafe_manual_budget_does_not_become_summary_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    wellness._save_profile({
        "birth_year": 1990, "height_cm": 170, "weight_kg": 80,
        "target_weight_kg": 70, "sex": "female", "activity_level": "light",
        "calorie_target": 500, "deficit_kcal": 5000,
    })
    summary = wellness._summary("2026-09-07", [])
    assert summary["calorie_target"] >= 1200


def test_deficit_only_profile_uses_calc_goal_for_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    profile = {
        "birth_year": 1990, "height_cm": 170, "weight_kg": 80,
        "target_weight_kg": 70, "sex": "female", "activity_level": "light",
        "deficit_kcal": 300,
    }
    wellness._save_profile(profile)
    from nightshift import wellness_calc
    expected = wellness_calc.calculate_goal(profile)["calorie_target"]
    summary = wellness._summary("2026-09-07", [{"type": "meal", "calories": 500}])
    assert summary["calorie_target"] == expected
    assert summary["remaining_calories"] == expected - 500
