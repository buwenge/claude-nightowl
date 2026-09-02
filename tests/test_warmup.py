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
    # 多时刻：07:00 做过；12:30、18:00 都过点——总review二 G14 之后只返回
    # 最晚那个（18:00），12:30 直接标 done，不用真发一句
    two = cfg(times=["07:00", "18:00", "12:30"])
    assert warmup.due(two, datetime(2026, 8, 28, 10, 5, tzinfo=timezone.utc)) == ["18:00"]  # 北京 18:05
    assert "12:30" in warmup.read_state()["done"]["2026-08-28"]
    # 再查一次：12:30 已经标 done，18:00 还没做过——不会重复冒出来
    assert warmup.due(two, datetime(2026, 8, 28, 10, 5, tzinfo=timezone.utc)) == ["18:00"]


def test_run_warmup_with_fake_claude(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/bash\necho 好\n", encoding="utf-8")
    fake.chmod(0o755)
    c = cfg()
    # S6.1 二次返修 B3：warmup 现在从 runners.claude 权威视图取 bin，不再看
    # 顶层 claude_bin（config.example.json 已声明 runners.claude，顶层键
    # 只是过期快照，改它不会生效）。
    c["runners"]["claude"]["bin"] = str(fake)
    now = datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)
    result = warmup.run_warmup(c, now, slot="07:00")
    assert result["ok"] and result["reply"] == "好" and result["last_date"] == "2026-08-28"
    assert result["done"] == {"2026-08-28": ["07:00"]}
    assert warmup.read_state()["last_run_at"] == "2026-08-27T23:00:00Z"


def test_run_warmup_ignores_stale_toplevel_when_runner_view_diverges(tmp_path):
    """二次返修阻断二反例③：顶层 `claude_bin`/`probe_model` 与
    `runners.claude.bin`/`probe_model` 故意分裂，预热命令只该认 runner
    view——旧代码直接读 `config["claude_bin"]`/`config["probe_model"]`，
    会拿一份过期的顶层快照去起进程。"""
    real_bin = tmp_path / "real-claude"
    real_bin.write_text("#!/bin/bash\necho 好\n", encoding="utf-8")
    real_bin.chmod(0o755)
    fake_bin = tmp_path / "wrong-claude"  # 顶层这份就不该被用到
    fake_bin.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    c = cfg()
    c["claude_bin"] = str(fake_bin)
    c["probe_model"] = "TOP_PROBE"
    c["runners"]["claude"]["bin"] = str(real_bin)
    c["runners"]["claude"]["probe_model"] = "RUNNER_PROBE"
    now = datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)
    result = warmup.run_warmup(c, now, slot="07:00")
    assert result["ok"]  # 走的是 runner view 那个真实脚本，不是顶层那个必炸的假脚本
    assert result["model"] == "RUNNER_PROBE"


def test_run_warmup_missing_probe_model_records_error(tmp_path):
    """审查 D5：probe_model 没配 → 记一次失败、当天该时刻标 done，不抛 TypeError。"""
    c = cfg()
    c["runners"]["claude"].pop("probe_model", None)
    c["runners"]["claude"]["bin"] = "/bin/false"
    now = datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)
    result = warmup.run_warmup(c, now, slot="07:00")
    assert result["ok"] is False and "probe_model" in result["error"]
    assert warmup.due(c, now) == []  # 标了 done，不会每 tick 重来


def test_tick_runs_warmup_once_and_refreshes_quota(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler.warmup, "run_warmup", lambda c, now, slot=None: (calls.append(slot), store.atomic_write_json(warmup.state_path(), {"done": {"2026-08-28": ["07:00"]}, "ok": True}))[0] or {"ok": True})
    # 审查 D10：必须 patch scheduler 真正调的 fetch_usage_claude——以前 patch 的是
    # 别名 fetch_usage，落空后这条测试每跑一次就真起一次 `claude -p /usage`。
    monkeypatch.setattr(scheduler.quota, "fetch_usage_claude", lambda c: {"session_pct": 1, "week_all_pct": 1, "per_model": {}, "raw": ""})
    c = cfg()
    store.atomic_write_json(store.home() / "config.json", c)
    now = datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)
    actions = scheduler.tick(c, now)
    assert any("预热窗口（07:00）：成功" in a for a in actions)
    assert (store.home() / "quota.json").is_file()
    scheduler.tick(c, datetime(2026, 8, 27, 23, 5, tzinfo=timezone.utc))
    assert len(calls) == 1
