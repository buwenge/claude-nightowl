"""quota.py 的测试：/usage 解析、额度门槛、假 claude 的 fetch_usage_claude。"""

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


def test_check_guards_usage_label_looked_up_by_runner_not_top_level_models():
    """S6.1 B3：usage_label 必须按 runner 对应的模型表查——传 runner="codex"
    时，就算顶层 config.models 里恰好有一个同名 usage_label 命中，也不该
    被那张 Claude 兼容表污染；Codex 自己的 models 表里没配就不做单模型线。"""
    usage = usage_fixture()
    usage["per_model"]["Fable"] = 97  # 顶层 CONFIG.models 里 claude-fable-5 → Fable
    config = {
        **CONFIG,
        "runners": {
            "codex": {"models": {"gpt-5.6-luna": {}}, "efforts": []},  # 没配 usage_label
        },
    }
    ok, _ = check_guards(usage, "gpt-5.6-luna", config, GUARDS, runner="codex")
    assert ok  # 没被顶层 Claude 表的 Fable 误伤


def test_fetch_usage_with_fake_claude(tmp_path, monkeypatch):
    # 先坐实环境里确实有 CLAUDECODE，fetch_usage_claude 必须把它摘掉
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
    usage = fetch_usage_claude(config)

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


def test_fetch_usage_claude_prefers_runners_table_over_stale_top_level(tmp_path):
    """S6.1 B3：config.runners.claude 存在时是唯一权威源——顶层 claude_bin/
    probe_model 就算还留着旧值也不能被用，否则"校验按新表、实际查额度按
    旧表"两处配置分裂，没人能保证它们一直同步。"""
    fake = tmp_path / "fake_probe.sh"
    args_file = tmp_path / "args.txt"
    fake.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$@\" > '{args_file}'\n"
        f"cat '{FIXTURES / 'usage_output.txt'}'\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    config = {
        **CONFIG,
        "claude_bin": "/should/not/be/used",  # 顶层留着一个假的、过时的值
        "probe_model": "should-not-be-used",
        "runners": {
            "claude": {
                "bin": str(fake), "probe_model": "the-real-probe-model",
                "models": CONFIG["models"], "efforts": [],
            },
        },
    }
    usage = fetch_usage_claude(config)
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert args == ["-p", "/usage", "--model", "the-real-probe-model", "--tools", ""]
    assert usage["session_pct"] == 13


def test_fetch_usage_nonzero_exit(tmp_path):
    fake = tmp_path / "fake_fail.sh"
    fake.write_text("#!/bin/bash\necho 坏了 >&2\nexit 3\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    config = dict(CONFIG, claude_bin=str(fake))
    with pytest.raises(UsageUnavailable) as exc_info:
        fetch_usage_claude(config)
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

    usage = fetch_usage_claude(dict(CONFIG, claude_bin=str(fake)))
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
    usage = fetch_usage_claude(dict(CONFIG, claude_bin="/nonexistent/claude"))
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


def test_fetch_usage_alias_is_gone():
    """审查 D：`fetch_usage` 别名已删——test_warmup 曾 monkeypatch 它而 scheduler
    调的是 fetch_usage_claude，补丁落空导致测试真起 claude。不许再长回来。"""
    import nightshift.quota as q
    assert not hasattr(q, "fetch_usage")


def test_parse_usage_week_all_line_renamed_is_parse_error():
    """审查 D3：认出了单模型周线却认不出 all models 那一行（措辞变了被当成一个
    "模型"收走）→ 必须显式失败，不能让 week_all_pct 静默 None、七日线失效。"""
    text = ("Current session: 10% used · resets Sep 2, 4:09am (UTC)\n"
            "Current week (All models): 99% used · resets Sep 2, 11:59am (UTC)\n"
            "Current week (Fable): 20% used · resets Sep 2, 11:59am (UTC)\n")
    with pytest.raises(UsageParseError):
        parse_usage(text)
    # 只有 session + all models、没有单模型行：照常
    ok = parse_usage("Current session: 10% used\nCurrent week (all models): 19% used\n")
    assert ok["week_all_pct"] == 19 and ok["per_model"] == {}


def test_resets_in_minutes_year_boundary():
    """审查 D4：跨年那几分钟，缓存里的 resets 还是 12 月 31 日 → 应算"已过"取 0，
    不是按当前年解析成明年 12 月 31 日的 52 万分钟。"""
    from datetime import datetime, timezone
    from nightshift.quota import resets_in_minutes
    assert resets_in_minutes("Dec 31, 11:55pm (UTC)", datetime(2027, 1, 1, 0, 10, tzinfo=timezone.utc)) == 0
    assert resets_in_minutes("Jan 2, 12pm (UTC)", datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)) == 3 * 24 * 60


def test_check_guards_missing_or_null_lines_fall_back_to_config_guards():
    """审查 D2：网页编辑清空某条线 → task.guards 缺 key（server 不回填）；
    以前 guards["session_pct_max"] KeyError 把整轮 tick 掀翻。现在回退
    config.guards；两处都没配才跳过这条线；非数字 fail-closed。"""
    usage = usage_fixture()
    usage["session_pct"] = 85
    config = {**CONFIG, "guards": {"session_pct_max": 80, "weekly_pct_max": 95}}
    for guards in ({}, {"weekly_pct_max": 95}, {"session_pct_max": None, "weekly_pct_max": 95}):
        ok, reason = check_guards(usage, "claude-fable-5", config, guards)
        assert not ok and "五小时" in reason, guards
    # 两处都没配这条线：跳过，不炸
    ok, _ = check_guards(usage, "claude-fable-5", {**CONFIG, "guards": {}}, {})
    assert ok
    # 线不是数字：fail-closed，给人话
    ok, reason = check_guards(usage, "claude-fable-5", config, {"session_pct_max": "80", "weekly_pct_max": 95})
    assert not ok and "不是数字" in reason
    # model_weekly_pct_max 为 null → 跟 weekly 线
    usage2 = usage_fixture()
    usage2["per_model"]["Fable"] = 96
    ok, reason = check_guards(usage2, "claude-fable-5", config, {"model_weekly_pct_max": None})
    assert not ok and "Fable" in reason


def test_fetch_usage_claude_missing_probe_model_is_unavailable(monkeypatch):
    """审查 D5：probe_model 没配 → UsageUnavailable（调用方接得住），
    不是 None 进 argv 的 TypeError。"""
    monkeypatch.delenv("NIGHTSHIFT_FAKE_USAGE_FILE", raising=False)
    with pytest.raises(UsageUnavailable):
        fetch_usage_claude({"runners": {"claude": {"bin": "/bin/false"}}})


def test_load_quota_file_non_dict_slice_is_empty(tmp_path):
    """审查 D6：分片被写坏成非对象时按空壳，消费方 slice_.get 不炸。"""
    (tmp_path / "quota.json").write_text(
        json.dumps({"claude": "oops", "codex": [1, 2]}), encoding="utf-8"
    )
    assert load_quota_file() == {"claude": {}, "codex": {}}


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


def test_fetch_usage_codex_two_lines_in_one_chunk(tmp_path, monkeypatch):
    """审查 D11：通知 + 响应同一次 write 到达时不能卡到超时——以前 select 盯裸 fd、
    readline 却把第二行留在 TextIOWrapper 缓冲里，fd 不再可读，一直等到 deadline。"""
    fake = tmp_path / "two_lines_app_server.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "R = {'rateLimits': {'limitId': 'codex',"
        " 'primary': {'usedPercent': 12, 'windowDurationMins': 300, 'resetsAt': 1788099565},"
        " 'secondary': {'usedPercent': 2, 'windowDurationMins': 10080, 'resetsAt': 1788653052}}}\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line) if line.strip() else {}\n"
        "    m = msg.get('method')\n"
        "    if m == 'initialize':\n"
        "        sys.stdout.write(json.dumps({'method': 'server/hello', 'params': {}}) + '\\n'"
        " + json.dumps({'id': msg['id'], 'result': {'userAgent': 'fake'}}) + '\\n'); sys.stdout.flush()\n"
        "    elif m == 'account/rateLimits/read':\n"
        "        sys.stdout.write(json.dumps({'method': 'account/updated', 'params': {}}) + '\\n'"
        " + json.dumps({'id': msg['id'], 'result': R}) + '\\n'); sys.stdout.flush(); break\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.delenv("NIGHTSHIFT_CODEX_BIN", raising=False)
    import time
    t0 = time.time()
    usage = fetch_usage_codex({"runners": {"codex": {"bin": str(fake)}}}, timeout=5.0)
    assert usage["session_pct"] == 12 and usage["week_all_pct"] == 2
    assert time.time() - t0 < 3  # 没有等到 deadline


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


def test_normalize_codex_ratelimits_accepts_float_percent():
    """审查 D1（阻断）：codex 核心协议 used_percent 是 f64（rollout 里是 47.0），
    app-server 的 usedPercent 若是 12.0 以前会被丢成 None → 守卫全放行。
    浮点向上取整；bool/字符串仍是 None。"""
    result = normalize_codex_ratelimits({
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 99.0, "windowDurationMins": 300, "resetsAt": 1788099565},
            "secondary": {"usedPercent": 80.4, "windowDurationMins": 10080, "resetsAt": 1788653052},
        },
    })
    assert result["session_pct"] == 99
    assert result["week_all_pct"] == 81  # 80.4 向上取整，宁可早拦
    bad = normalize_codex_ratelimits({
        "rateLimits": {"primary": {"usedPercent": True, "windowDurationMins": 300},
                       "secondary": {"usedPercent": "12", "windowDurationMins": 10080}},
    })
    assert bad["session_pct"] is None and bad["week_all_pct"] is None
    # 端到端：假 app-server 回浮点，守卫要拦
    ok, reason = check_guards(result, "gpt-5.6-luna", {"runners": {"codex": {"models": {"gpt-5.6-luna": {}}}}}, GUARDS, runner="codex")
    assert not ok and "五小时" in reason


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


def test_write_quota_runner_concurrent_threads_do_not_lose_updates(tmp_path, monkeypatch):
    """S6.1 A6：scheduler 主线程与网页手动刷新线程会并发调用；两个线程各自
    反复写各自那家分片，不许有任何一次写丢失（lost update），也不许因为
    临时文件名撞车而炸异常。"""
    import threading

    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    errors: list[Exception] = []
    rounds = 30

    def hammer(runner: str):
        try:
            for i in range(rounds):
                write_quota_runner(
                    runner, {"usage": {"session_pct": i}, "fetched_at": f"t{i}", "error": None}
                )
        except Exception as exc:  # pragma: no cover - 断言在主线程做，这里只留痕
            errors.append(exc)

    t1 = threading.Thread(target=hammer, args=("claude",))
    t2 = threading.Thread(target=hammer, args=("codex",))
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)

    assert not errors, errors
    data = load_quota_file()
    # 最终两家都在、都是各自最后一轮写的值——没有一家被另一家的写入顶掉/丢失
    assert data["claude"]["usage"]["session_pct"] == rounds - 1
    assert data["codex"]["usage"]["session_pct"] == rounds - 1
