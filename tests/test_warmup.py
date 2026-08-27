import json
from datetime import datetime, timezone

import pytest

from nightshift import scheduler, store, warmup

CONFIG = json.load(open(__file__.rsplit("/tests/", 1)[0] + "/config.example.json", encoding="utf-8"))


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    store.atomic_write_json(tmp_path / "config.json", CONFIG)
    return tmp_path


def cfg(enabled=True, time_local="07:00", times=None):
    c = json.loads(json.dumps(CONFIG))
    c["warmup"] = {"enabled": enabled, "times": times if times is not None else [time_local]}
    c["display_tz_offset_hours"] = 8
    return c


def test_due_respects_switch_time_and_once_per_day():
    # 北京 06:59 → 未到；07:00 → 到；同一天做过 → 不再
    assert warmup.due(cfg(), datetime(2026, 8, 27, 22, 59, tzinfo=timezone.utc)) == []
    assert warmup.due(cfg(), datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)) == ["07:00"]  # 北京 8/28 07:00
    assert warmup.due(cfg(enabled=False), datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)) == []
    assert warmup.due(cfg(time_local="乱写"), datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)) == []
    store.atomic_write_json(warmup.state_path(), {"done": {"2026-08-28": ["07:00"]}})
    assert warmup.due(cfg(), datetime(2026, 8, 27, 23, 30, tzinfo=timezone.utc)) == []
    # 多时刻：07:00 做过，18:00 到点再来一次；中午加的 12:30 也算
    two = cfg(times=["07:00", "18:00", "12:30"])
    assert warmup.due(two, datetime(2026, 8, 28, 10, 5, tzinfo=timezone.utc)) == ["12:30", "18:00"]  # 北京 18:05


def test_run_warmup_with_fake_claude(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/bash\necho 好\n", encoding="utf-8")
    fake.chmod(0o755)
    c = cfg()
    c["claude_bin"] = str(fake)
    now = datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)
    result = warmup.run_warmup(c, now, slot="07:00")
    assert result["ok"] and result["reply"] == "好" and result["last_date"] == "2026-08-28"
    assert result["done"] == {"2026-08-28": ["07:00"]}
    assert warmup.read_state()["last_run_at"] == "2026-08-27T23:00:00Z"


def test_tick_runs_warmup_once_and_refreshes_quota(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler.warmup, "run_warmup", lambda c, now, slot=None: (calls.append(slot), store.atomic_write_json(warmup.state_path(), {"done": {"2026-08-28": ["07:00"]}, "ok": True}))[0] or {"ok": True})
    monkeypatch.setattr(scheduler.quota, "fetch_usage", lambda c: {"session_pct": 1, "week_all_pct": 1, "per_model": {}, "raw": ""})
    c = cfg()
    store.atomic_write_json(store.home() / "config.json", c)
    now = datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)
    actions = scheduler.tick(c, now)
    assert any("预热窗口（07:00）：成功" in a for a in actions)
    assert (store.home() / "quota.json").is_file()
    scheduler.tick(c, datetime(2026, 8, 27, 23, 5, tzinfo=timezone.utc))
    assert len(calls) == 1
