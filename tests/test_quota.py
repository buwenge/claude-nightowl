"""quota.py 的测试：/usage 解析、额度门槛、假 claude 的 fetch_usage。"""

import stat
from pathlib import Path

import pytest

from nightshift.quota import (
    UsageParseError,
    UsageUnavailable,
    check_guards,
    fetch_usage,
    parse_usage,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

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
