"""健康周月报告专项测试。"""

import json

import pytest

from nightshift.__main__ import main
from nightshift.wellness_store import WellnessStore


def test_range_summary_handles_month_boundary_and_moving_weight(tmp_path):
    store = WellnessStore(tmp_path)
    store.create_entry("2026-01-30", {"type": "weight", "weight_kg": 71})
    store.create_entry("2026-01-31", {"type": "meal", "name": "米饭", "calories": 500,
                                      "protein_g": 10})
    store.create_entry("2026-01-31", {"type": "weight", "weight_kg": 70})
    store.create_entry("2026-02-01", {"type": "meal", "name": "面条", "calories": 600})
    store.create_entry("2026-02-01", {"type": "exercise", "name": "步行",
                                      "calories_burned": 200})

    result = store.range_summary("2026-01-31", "2026-02-01", target=1800)

    assert [item["date"] for item in result["days"]] == ["2026-01-31", "2026-02-01"]
    assert result["totals"]["calories_in"] == 1100
    assert result["totals"]["calories_out"] == 200
    assert result["totals"]["macros"]["protein_g"] == 10
    assert result["days"][0]["foods"][0]["name"] == "米饭"
    assert result["days"][0]["weight_7d_average"] == 70.5


def test_report_markdown_and_json_have_same_numbers(tmp_path):
    store = WellnessStore(tmp_path)
    store.create_entry("2026-09-07", {"type": "meal", "name": "早餐", "calories": 400})
    data = store.report("week", "2026-09-07", "json", target=1800)
    markdown = store.report("week", "2026-09-07", "md", target=1800)

    assert data["totals"]["calories_in"] == 400
    assert "2026-09-07" in markdown
    assert "400.0" in markdown
    assert "每日食物" in markdown
    assert json.dumps(data, ensure_ascii=False)


def test_report_rejects_invalid_period_and_date(tmp_path):
    store = WellnessStore(tmp_path)
    with pytest.raises(ValueError, match="period"):
        store.report("year", "2026-09-07")
    with pytest.raises(ValueError, match="from"):
        store.range_summary("2026-09-08", "2026-09-07")


def test_moving_weight_uses_only_latest_measurement_per_day(tmp_path):
    store = WellnessStore(tmp_path)
    store.create_entry("2026-09-01", {"type": "weight", "weight_kg": 70,
                                      "recorded_at": "2026-09-01T08:00:00Z"})
    store.create_entry("2026-09-01", {"type": "weight", "weight_kg": 72,
                                      "recorded_at": "2026-09-01T20:00:00Z"})
    store.create_entry("2026-09-02", {"type": "weight", "weight_kg": 71,
                                      "recorded_at": "2026-09-02T08:00:00Z"})

    result = store.range_summary("2026-09-02", "2026-09-02")

    assert result["days"][0]["weight_7d_average"] == 71.5


def test_cli_json_matches_store_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    store = WellnessStore(tmp_path)
    store.save_profile({"calorie_target": 1800})
    store.create_entry("2026-09-07", {"type": "meal", "name": "午饭", "calories": 650})
    expected = store.report("week", "2026-09-07", "json", profile={"calorie_target": 1800})

    assert main(["wellness-report", "--days", "7", "--end", "2026-09-07", "--format", "json"]) == 0
    actual = json.loads(capsys.readouterr().out)
    assert actual["from"] == expected["from"]
    assert actual["to"] == expected["to"]
    assert actual["totals"] == expected["totals"]
    assert actual["averages"] == expected["averages"]
    assert actual["days"] == expected["days"]
