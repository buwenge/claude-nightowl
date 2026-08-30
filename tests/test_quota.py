"""quota.py 的测试：/usage 解析、额度门槛、假 claude 的 fetch_usage。"""

import json
import stat
from pathlib import Path

import pytest

from nightshift import store
from nightshift.quota import (
    AppServerTimeout,
    UsageParseError,
    UsageUnavailable,
    check_guards,
    fetch_usage,
    fetch_usage_claude,
    fetch_usage_codex,
    load_quota_file,
    normalize_codex_ratelimits,
    parse_usage,
    write_quota_runner,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_APP_SERVER = FIXTURES.parent / "fake_codex_app_server.py"

CONFIG = {
    "claude_bin": "claude",
    "probe_model": "claude-haiku-4-5-20251001",
    "models": {
        "claude-fable-5": {"context_limit": 500000, "usage_label": "Fable"},
        "claude-haiku-4-5-20251001": {"context_limit": 200000},
    },
}

GUARDS = {"session_pct_max": 80, "weekly_pct_max": 95}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))


def fixture_text() -> str:
    return (FIXTURES / "usage_output.txt").read_text(encoding="utf-8")


def usage_fixture() -> dict:
    return parse_usage(fixture_text())


def test_parse_usage_fixture():
    usage = usage_fixture()
    assert usage["session_pct"] == 13
    assert usage["week_all_pct"] == 19
    assert usage["per_model"] == {"Fable": 35}
    assert "Aug 27" in usage["session_resets"]
    assert "Sep 2" in usage["week_all_resets"]
    assert usage["per_model_resets"]["Fable"] == usage["week_all_resets"]
    assert usage["raw"] == fixture_text()


def test_parse_usage_garbage_raises():
    with pytest.raises(UsageParseError) as exc_info:
        parse_usage("这里什么额度都没有，只有一句闲聊。")
    assert "闲聊" in exc_info.value.raw  # 异常里带着原文


def test_parse_usage_session_only_ok():
    usage = parse_usage("Current session: 42% used")
    assert usage["session_pct"] == 42
    assert usage["week_all_pct"] is None
    assert usage["per_model"] == {}


def test_check_guards_all_pass():
    ok, reason = check_guards(usage_fixture(), "claude-fable-5", CONFIG, GUARDS)
    assert ok
    assert reason == ""


def test_check_guards_session_over():
    usage = usage_fixture()
    usage["session_pct"] = 85
    ok, reason = check_guards(usage, "claude-fable-5", CONFIG, GUARDS)
    assert not ok
    assert "五小时" in reason and "85%" in reason and "80%" in reason


def test_check_guards_week_all_over():
    usage = usage_fixture()
    usage["week_all_pct"] = 96
    ok, reason = check_guards(usage, "claude-fable-5", CONFIG, GUARDS)
    assert not ok
    assert "七日" in reason and "96%" in reason


def test_check_guards_model_line_over_but_all_not():
    # all models 才 19%，但任务模型自己的周线 Fable 97% —— 一样拦
    usage = usage_fixture()
    usage["per_model"]["Fable"] = 97
    ok, reason = check_guards(usage, "claude-fable-5", CONFIG, GUARDS)
    assert not ok
    assert "Fable" in reason
    # 没配 usage_label 的模型不做单模型线判定
    ok, _ = check_guards(usage, "claude-haiku-4-5-20251001", CONFIG, GUARDS)
    assert ok


def test_fetch_usage_with_fake_claude(tmp_path, monkeypatch):
    # 先坐实环境里确实有 CLAUDECODE，fetch_usage 必须把它摘掉
    monkeypatch.setenv("CLAUDECODE", "1")
    args_file = tmp_path / "args.txt"
    env_file = tmp_path / "env.txt"
    fake = tmp_path / "fake_probe.sh"
    fake.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$@\" > '{args_file}'\n"
        f"env > '{env_file}'\n"
        f"cat '{FIXTURES / 'usage_output.txt'}'\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    config = dict(CONFIG, claude_bin=str(fake))
    usage = fetch_usage(config)

    args = args_file.read_text(encoding="utf-8").splitlines()
    assert args == [
        "-p",
        "/usage",
        "--model",
        "claude-haiku-4-5-20251001",
        "--tools",
        "",
    ]
    env_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert all(not line.startswith("CLAUDECODE=") for line in env_lines)
    assert usage["session_pct"] == 13
    assert usage["per_model"] == {"Fable": 35}


def test_fetch_usage_nonzero_exit(tmp_path):
    fake = tmp_path / "fake_fail.sh"
    fake.write_text("#!/bin/bash\necho 坏了 >&2\nexit 3\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    config = dict(CONFIG, claude_bin=str(fake))
    with pytest.raises(UsageUnavailable) as exc_info:
        fetch_usage(config)
    assert "坏了" in str(exc_info.value)


def test_fetch_usage_creates_missing_home(tmp_path, monkeypatch):
    """NIGHTSHIFT_HOME 指向不存在的目录：先建目录再跑子进程，照常解析（R3）。"""
    missing = tmp_path / "not" / "created"
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(missing))
    assert not missing.exists()

    fake = tmp_path / "fake_probe.sh"
    fake.write_text(
        "#!/bin/bash\n"
        f"cat '{FIXTURES / 'usage_output.txt'}'\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    usage = fetch_usage(dict(CONFIG, claude_bin=str(fake)))
    assert usage["session_pct"] == 13
    assert usage["per_model"] == {"Fable": 35}
    assert missing.exists()  # ensure_dirs 已把数据目录骨架建出来


def test_fetch_usage_fake_file_switch(tmp_path, monkeypatch):
    """NIGHTSHIFT_FAKE_USAGE_FILE：读该文件当 /usage 输出，完全不起子进程
    （serve --once 的集成测试用，monkeypatch 管不到子进程）。"""
    usage_file = tmp_path / "usage.txt"
    usage_file.write_text(fixture_text(), encoding="utf-8")
    monkeypatch.setenv("NIGHTSHIFT_FAKE_USAGE_FILE", str(usage_file))
    # 可执行文件路径是故意给的不存在值：真起了子进程必然炸，测试就露馅
    usage = fetch_usage(dict(CONFIG, claude_bin="/nonexistent/claude"))
    assert usage["session_pct"] == 13
    assert usage["week_all_pct"] == 19
    assert usage["per_model"] == {"Fable": 35}


def test_resets_in_minutes():
    from datetime import datetime, timezone
    from nightshift.quota import resets_in_minutes
    now = datetime(2026, 8, 27, 16, 50, tzinfo=timezone.utc)
    assert resets_in_minutes("Aug 27, 6:40pm (UTC)", now) == 110
    assert resets_in_minutes("Aug 27, 12pm (UTC)", now) == 0        # 已过，取 0
    assert resets_in_minutes("Sep 2, 12am (UTC)", now) == (5 * 24 + 7) * 60 + 10
    assert resets_in_minutes("垃圾", now) is None
    assert resets_in_minutes(None, now) is None
    # 跨年：一月的时间在八月看来是"一天前以上" → 算下一年
    assert resets_in_minutes("Jan 1, 1am (UTC)", now) > 100 * 24 * 60


# ---------- S6：fetch_usage_claude 别名与 Codex 额度 ----------


def test_fetch_usage_claude_alias_is_same_function():
    assert fetch_usage is fetch_usage_claude


CODEX_CONFIG = {"runners": {"codex": {"bin": str(FAKE_APP_SERVER)}}}


def test_fetch_usage_codex_happy_path(monkeypatch):
    monkeypatch.delenv("NIGHTSHIFT_CODEX_BIN", raising=False)
    usage = fetch_usage_codex({"runners": {"codex": {"bin": str(FAKE_APP_SERVER)}}})
    assert usage["session_pct"] == 12
    assert usage["week_all_pct"] == 2
    assert usage["session_resets"] is not None and usage["session_resets"].endswith("Z")
    assert usage["week_all_resets"] is not None
    assert usage["per_model"] == {}
    assert usage["rate_limit_reached_type"] is None
    assert usage["reset_credits_available"] == 1
    assert usage["windows"]["primary"]["window_minutes"] == 300
    assert usage["windows"]["secondary"]["window_minutes"] == 10080


def test_fetch_usage_codex_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NIGHTSHIFT_CODEX_BIN", str(FAKE_APP_SERVER))
    usage = fetch_usage_codex({"runners": {"codex": {"bin": "/nonexistent/codex"}}})
    assert usage["session_pct"] == 12


def test_fetch_usage_codex_missing_binary():
    with pytest.raises(UsageUnavailable, match="找不到"):
        fetch_usage_codex({"runners": {"codex": {"bin": "/nonexistent/codex-binary"}}})


def test_fetch_usage_codex_hang_times_out(monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_FAKE_CODEX_HANG", "1")
    with pytest.raises(AppServerTimeout):
        fetch_usage_codex(CODEX_CONFIG, timeout=1.0)


def test_fetch_usage_codex_early_exit_is_unavailable(monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_FAKE_CODEX_EXIT_EARLY", "1")
    with pytest.raises(AppServerTimeout):
        fetch_usage_codex(CODEX_CONFIG, timeout=3.0)


def test_normalize_codex_ratelimits_prefers_rate_limits_by_id():
    result = {
        "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 99, "windowDurationMins": 300, "resetsAt": 1}},
        "rateLimitsByLimitId": {
            "codex": {"limitId": "codex", "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1788099565},
                      "secondary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1788653052},
                      "rateLimitReachedType": "primary"},
        },
        "rateLimitResetCredits": {"availableCount": 0, "credits": []},
    }
    usage = normalize_codex_ratelimits(result)
    assert usage["session_pct"] == 12  # 来自 rateLimitsByLimitId，不是顶层那份 99
    assert usage["rate_limit_reached_type"] == "primary"
    assert usage["reset_credits_available"] == 0


def test_normalize_codex_ratelimits_missing_fields_are_null_not_zero():
    usage = normalize_codex_ratelimits({"rateLimits": {}})
    assert usage["session_pct"] is None
    assert usage["week_all_pct"] is None
    assert usage["session_resets"] is None
    assert usage["rate_limit_reached_type"] is None
    assert usage["reset_credits_available"] is None
    assert usage["windows"] == {}


def test_normalize_codex_ratelimits_unknown_window_minutes_ignored():
    """windowDurationMins 既不是 300 也不是 10080：不瞎猜是哪条线，两条都是 None。"""
    result = {"rateLimits": {"primary": {"usedPercent": 50, "windowDurationMins": 999, "resetsAt": 1}}}
    usage = normalize_codex_ratelimits(result)
    assert usage["session_pct"] is None
    assert usage["week_all_pct"] is None
    assert usage["windows"]["primary"]["used_pct"] == 50  # 原始数据仍留痕，只是不进 session/week


# ---------- S6：quota.json 双 runner 归一读写 ----------


def test_load_quota_file_missing_returns_empty_shells():
    assert load_quota_file() == {"claude": {}, "codex": {}}


def test_load_quota_file_old_shape_reads_as_claude():
    old = {"usage": {"session_pct": 5}, "fetched_at": "2026-08-30T00:00:00Z"}
    store.atomic_write_json(store.home() / "quota.json", old)
    data = load_quota_file()
    assert data["claude"] == old
    assert data["codex"] == {}
    # 读取不改盘：文件仍是旧形状，下次成功刷新才会换新形状
    on_disk = json.loads((store.home() / "quota.json").read_text(encoding="utf-8"))
    assert on_disk == old


def test_load_quota_file_new_shape_roundtrip():
    new = {"claude": {"usage": {"session_pct": 1}, "fetched_at": "t1", "error": None},
           "codex": {"usage": {"session_pct": 2}, "fetched_at": "t2", "error": None}}
    store.atomic_write_json(store.home() / "quota.json", new)
    assert load_quota_file() == new


def test_write_quota_runner_does_not_clobber_the_other():
    write_quota_runner("claude", {"usage": {"session_pct": 1}, "fetched_at": "t1", "error": None})
    write_quota_runner("codex", {"usage": {"session_pct": 2}, "fetched_at": "t2", "error": None})
    data = load_quota_file()
    assert data["claude"]["usage"]["session_pct"] == 1
    assert data["codex"]["usage"]["session_pct"] == 2
    # 再刷新一次 codex，claude 那份原样不动（一家刷新失败/成功都不该动到另一家）
    write_quota_runner("codex", {"usage": None, "fetched_at": "t3", "error": "查不到"})
    data = load_quota_file()
    assert data["claude"]["usage"]["session_pct"] == 1
    assert data["codex"]["error"] == "查不到"
