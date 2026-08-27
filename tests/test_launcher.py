"""launcher 的测试：纯函数部分 + tmux 集成（假 claude，不花钱）。"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from nightshift import launcher, store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = FIXTURES.parent / "fake_claude.sh"
SELFTEST_SESSION = "ns-selftest"  # 守则：测试专用会话名，用完必杀

CONFIG = {
    "tmux_session": SELFTEST_SESSION,
    "window_prefix": "ns:",
    "claude_bin": "claude",
    "probe_model": "claude-haiku-4-5-20251001",
    "display_tz_offset_hours": 8,
    "memory_max_bytes": 2147483648,
    "projects": {"demo": "/home/user/projects/demo"},
    "models": {
        "claude-fable-5": {"context_limit": 500000, "usage_label": "Fable"},
    },
    "default_context_limit": 200000,
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
    "chain": {"max_windows": 3, "on_no_handover": "continue"},
}


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    monkeypatch.delenv("NIGHTSHIFT_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("NIGHTSHIFT_CLAUDE_JSON", raising=False)
    store.atomic_write_json(tmp_path / "config.json", CONFIG)
    return tmp_path


def make_task(project_path: str | None = None, **over):
    """建一个任务；project_path 不为 None 时改写数据目录里的 config.json，
    返回 (task_id, 实际用的 config)——launch 必须用这份 config。"""
    config = dict(CONFIG)
    if project_path:
        config["projects"] = {"demo": project_path}
        store.atomic_write_json(store.home() / "config.json", config)
    task = {
        "title": "集成测试任务",
        "project": "demo",
        "model": "claude-fable-5",
        "effort": "high",
        "run_at": "2026-08-27T18:00:00Z",
        "task_text": "正文",
        "prompt_final": "完整提示词",
    }
    task.update(over)
    return store.create_task(task, config), config


# ---------- 纯函数部分 ----------


def test_hook_settings_seven_events():
    settings = launcher.hook_settings("abc-123")
    hooks = settings["hooks"]
    assert set(hooks) == {
        "UserPromptSubmit",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "PostToolUse",
        "PreCompact",
        "SessionEnd",
    }
    for event, entries in hooks.items():
        assert len(entries) == 1
        inner = entries[0]["hooks"][0]
        assert inner["type"] == "command"
        assert inner["timeout"] == 10
        assert f"nightshift.hook abc-123 {event}" in inner["command"]


def test_write_task_files(tmp_path):
    task_id, config = make_task(project_path="/home/user/projects/demo")
    task = store.load_task(task_id)
    launcher.write_task_files(task, config, "01234567-89ab-cdef-0123-456789abcdef")
    d = store.task_dir(task_id)

    run_sh = (d / "run.sh").read_text(encoding="utf-8")
    assert "--model 'claude-fable-5'" in run_sh
    assert "--effort 'high'" in run_sh
    assert "--permission-mode auto" in run_sh
    assert "--session-id '01234567-89ab-cdef-0123-456789abcdef'" in run_sh
    assert f"--settings '{d / 'settings.json'}'" in run_sh
    assert "cd '/home/user/projects/demo'" in run_sh
    assert "unset CLAUDECODE" in run_sh
    assert f"NIGHTSHIFT_HOME='{store.home()}'" in run_sh
    assert f"PYTHONPATH='{REPO_ROOT}'" in run_sh
    assert "claude 已退出" in run_sh
    mode = (d / "run.sh").stat().st_mode
    assert mode & 0o700 == 0o700  # 可执行

    settings = json.loads((d / "settings.json").read_text(encoding="utf-8"))
    assert len(settings["hooks"]) == 7
    assert (d / "prompt.txt").read_text(encoding="utf-8") == task["prompt_final"]


def test_claude_bin_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_BIN", str(tmp_path / "fake.sh"))
    assert launcher.claude_bin(CONFIG) == str(tmp_path / "fake.sh")
    monkeypatch.delenv("NIGHTSHIFT_CLAUDE_BIN")
    assert launcher.claude_bin(CONFIG) == CONFIG["claude_bin"]


def test_is_trusted_three_cases(tmp_path, monkeypatch):
    claude_json = tmp_path / "claude.json"
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_JSON", str(claude_json))

    # ① 文件不存在 → False
    assert launcher.is_trusted("/some/dir") is False

    # ② 文件在但没有该目录的信任记录 → False
    claude_json.write_text(
        json.dumps({"projects": {"/other/dir": {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    assert launcher.is_trusted("/some/dir") is False
    assert launcher.is_trusted("/other/dir") is True  # 对照：信任过的是 True

    # ③ 显式 false / 坏 JSON → False
    claude_json.write_text(
        json.dumps({"projects": {"/some/dir": {"hasTrustDialogAccepted": False}}}),
        encoding="utf-8",
    )
    assert launcher.is_trusted("/some/dir") is False
    claude_json.write_text("{坏的", encoding="utf-8")
    assert launcher.is_trusted("/some/dir") is False


def test_pid_alive():
    assert launcher.pid_alive(os.getpid()) is True
    # 超出 pid_max（默认约 4 百万）的 pid 必然不存在
    assert launcher.pid_alive(2**24) is False


# ---------- tmux 集成部分（假 claude）----------


@pytest.fixture
def tmux_session():
    if shutil.which("tmux") is None:
        pytest.skip("tmux 不在 PATH，跳过集成测试")
    subprocess.run(
        ["tmux", "kill-session", "-t", SELFTEST_SESSION], capture_output=True
    )
    proc = subprocess.run(
        ["tmux", "new-session", "-d", "-s", SELFTEST_SESSION],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    yield SELFTEST_SESSION
    # 守则：无论断言成败都要杀掉测试会话
    subprocess.run(["tmux", "kill-session", "-t", SELFTEST_SESSION], capture_output=True)


@pytest.fixture
def trusted_env(tmux_session, tmp_path, monkeypatch):
    """假 claude + 假信任记录 + 参数日志，都指到 tmp。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(proj): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_JSON", str(claude_json))
    os.chmod(FAKE_CLAUDE, 0o755)
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_BIN", str(FAKE_CLAUDE))
    fake_log = tmp_path / "fake_claude_args.log"
    monkeypatch.setenv("NIGHTSHIFT_FAKE_LOG", str(fake_log))
    # tmux 窗口里的进程继承的是 server 的环境，不是测试进程的；
    # 把测试用的环境变量写进 ns-selftest 会话环境，随会话生灭。
    subprocess.run(
        ["tmux", "set-environment", "-t", tmux_session,
         "NIGHTSHIFT_FAKE_LOG", str(fake_log)],
        capture_output=True,
    )
    return {"proj": proj, "fake_log": fake_log}


def wait_for_state(task_id: str, timeout: float = 15.0):
    """轮询到 exited 为止，顺路记录见过的状态。"""
    deadline = time.time() + timeout
    seen = []
    status = {}
    while time.time() < deadline:
        status = store.read_status(task_id)
        state = status.get("state")
        if state not in seen:
            seen.append(state)
        if state == "exited":
            return status, seen
        time.sleep(0.1)
    return status, seen


def test_launch_full_cycle(tmux_session, trusted_env, tmp_path):
    task_id, config = make_task(project_path=str(trusted_env["proj"]))
    status = launcher.launch(task_id, config)

    assert re.fullmatch(r"@\d+", status["window_id"])
    assert isinstance(status["pane_pid"], int)
    assert launcher.pid_alive(status["pane_pid"])  # 窗口正停在 read 上
    assert status["state"] in ("launching", "working", "idle")

    status, seen = wait_for_state(task_id)
    assert status["state"] == "exited", f"15 秒没等到 exited，见过 {seen}"
    assert "working" in seen, f"中途没见过 working：{seen}"
    assert "idle" in seen, f"中途没见过 idle：{seen}"
    assert status["exit_reason"] == "other"
    # 假 claude 把参数里的 session_id 通过 hook 坐实回了 status
    assert status["session_id"] == status.get("session_id")
    assert launcher.window_alive(status["window_id"], CONFIG)

    # 屏幕快照有收尾横幅（给一点时间让 echo 落到屏幕）
    text = ""
    for _ in range(50):
        text = launcher.capture_pane(status["window_id"])
        if "claude 已退出" in text:
            break
        time.sleep(0.1)
    assert "claude 已退出" in text

    # 假 claude 的参数日志：run.sh 真把那些参数传下去了
    fake_log = trusted_env["fake_log"].read_text(encoding="utf-8")
    assert "--permission-mode" in fake_log
    assert "--settings" in fake_log
    assert "--session-id" in fake_log
    assert status["session_id"] in fake_log


def test_launch_untrusted_opens_failure_window(tmux_session, trusted_env, tmp_path):
    # 用另一个没登记信任的目录
    other = tmp_path / "other"
    other.mkdir()
    task_id, config = make_task(project_path=str(other))
    status = launcher.launch(task_id, config)

    assert status["state"] == "failed"
    assert "未信任" in status["error"]
    # 假 claude 根本不该被叫起来
    assert not trusted_env["fake_log"].exists()

    # 失败窗口出现在 ns-selftest 会话里，名字含 (失败)
    proc = subprocess.run(
        ["tmux", "list-windows", "-t", SELFTEST_SESSION, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert any("(失败)" in name for name in proc.stdout.splitlines())
