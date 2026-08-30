"""serve --once 的端到端集成测试：真 tmux + 假 claude + 假 /usage，不花钱。

复用 test_launcher 的 tmux_session / trusted_env 思路：
- tmux 缺席则 skip；测试会话只叫 ns-selftest，用完必杀；
- claude 用 tests/fake_claude.sh（环境变量 NIGHTSHIFT_CLAUDE_BIN 换装）；
- /usage 用 NIGHTSHIFT_FAKE_USAGE_FILE 夹具文件（patch 不跨进程，serve 是子进程）。
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nightshift import store

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
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
    "chain_template": "{task}\n\n这是第 {shift} 班。上一班交接如下：\n{handover}\n\n先核对交接里说的状态再动手。",
    "scheduler": {
        "interval_seconds": 30,
        "launch_grace_seconds": 180,
        "postpone_minutes": 30,
        "max_postpone_hours": 6,
        "quota_refresh_minutes": 30,
        "keepalive_idle_minutes": 50,
        "keepalive_text": "保活探针——还在跑吗？",
    },
}


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    store.atomic_write_json(tmp_path / "config.json", CONFIG)
    return tmp_path


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
def trusted_env(tmp_path, monkeypatch, tmux_session):
    """假 claude + 假信任记录 + 假 /usage 文件 + 参数日志，都指到 tmp。"""
    proj = tmp_path / "proj"
    # S5：工作树任务的项目必须是 Git 仓库（树建在 .claude/worktrees/ 下）
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(proj), "config", "user.email", "ns@example.test"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(proj), "config", "user.name", "ns"],
                   check=True, capture_output=True)
    (proj / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)
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
    usage_file = tmp_path / "usage.txt"
    usage_file.write_text(
        (FIXTURES / "usage_output.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("NIGHTSHIFT_FAKE_USAGE_FILE", str(usage_file))
    # tmux 窗口里的进程继承 server 的环境：假 claude 的参数日志必须挂会话上
    subprocess.run(
        ["tmux", "set-environment", "-t", tmux_session,
         "NIGHTSHIFT_FAKE_LOG", str(fake_log)],
        capture_output=True,
    )
    # 数据目录里的 config.json 也要把 project 指到真正信任的目录
    config = dict(CONFIG)
    config["projects"] = {"demo": str(proj)}
    store.atomic_write_json(store.home() / "config.json", config)
    return {"proj": proj, "fake_log": fake_log}


def serve_once() -> str:
    """子进程跑一轮 `python3 -m nightshift serve --once`，返回 stdout。"""
    proc = subprocess.run(
        [sys.executable, "-m", "nightshift", "serve", "--once"],
        cwd=REPO_ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def wait_for_state(task_id: str, timeout: float = 30.0):
    """轮询到 exited 为止，顺路记录见过的状态序列。"""
    deadline = time.time() + timeout
    seen = []
    status = {}
    while time.time() < deadline:
        status = store.read_status(task_id)
        state = status.get("state")
        if not seen or state != seen[-1]:
            seen.append(state)
        if state == "exited":
            return status, seen
        time.sleep(0.1)
    return status, seen


def test_serve_once_launches_then_idle_on_second_pass(trusted_env):
    config = store.load_config()
    task_id = store.create_task(
        {
            "title": "集成夜班",
            "project": "demo",
            "model": "claude-fable-5",
            "effort": "high",
            "run_at": "2026-08-27T00:00:00Z",  # 过去：serve --once 立刻到点
            "task_text": "正文",
            "prompt_final": "只回复：好",
        },
        config,
    )

    # 第一次 serve --once：到点预检（假 /usage）→ 开窗起假 claude → 一轮后退出
    out = serve_once()
    assert task_id in out  # 动作描述里有任务 id

    status, seen = wait_for_state(task_id)
    assert status["state"] == "exited", f"30 秒没等到 exited，见过 {seen}"
    assert "launching" in seen, f"中途没见过 launching：{seen}"
    assert "working" in seen, f"中途没见过 working：{seen}"
    assert "idle" in seen, f"中途没见过 idle：{seen}"
    assert status["exit_reason"] == "other"  # 假 claude 的 SessionEnd reason
    assert "quota_at_launch" in status  # 预检顺手记了额度
    assert status["quota_at_launch"]["session_pct"] == 13

    # R1：run.sh 在 claude 退出后写下 exit_code（SessionEnd 正常时轮不到
    # 调度器兜底，但文件必须在）；它比 SessionEnd hook 晚一拍，轮询等它出现
    exit_file = store.task_dir(task_id) / "exit_code"
    deadline = time.time() + 10
    while time.time() < deadline and not exit_file.is_file():
        time.sleep(0.1)
    assert exit_file.is_file(), "claude 退出后 run.sh 没写下 exit_code"
    assert exit_file.read_text(encoding="utf-8").strip() == "0"

    # 假 claude 只被起过一次
    fake_log = trusted_env["fake_log"].read_text(encoding="utf-8")
    assert fake_log.count("--session-id") == 1
    # 预检写下的 quota.json
    quota_data = json.loads(
        (store.home() / "quota.json").read_text(encoding="utf-8")
    )
    assert quota_data["claude"]["usage"]["session_pct"] == 13  # S6：claude 分片

    # 第二次 serve --once：exited 不动、没活跃任务不刷额度 → 什么都不做
    out2 = serve_once()
    assert out2.strip() == ""
    assert store.read_status(task_id)["state"] == "exited"
    assert trusted_env["fake_log"].read_text(
        encoding="utf-8"
    ).count("--session-id") == 1


def test_chain_two_shifts_end_to_end(trusted_env):
    """换班全链：第一班 exited → 写交接 NEXT: continue → 后继 scheduled →
    后继被起 → 写 NEXT: done → 后继 finished。假 claude 一共被起两次。"""
    config = store.load_config()
    task_id = store.create_task(
        {
            "title": "换班夜班",
            "project": "demo",
            "model": "claude-fable-5",
            "effort": "high",
            "run_at": "2026-08-27T00:00:00Z",  # 过去：serve --once 立刻到点
            "task_text": "正文",
            "prompt_final": "只回复：好",
        },
        config,
    )

    # ① 第一班：起 → 跑完 → exited
    serve_once()
    parent_status, parent_seen = wait_for_state(task_id)
    assert parent_status["state"] == "exited", f"没等到 exited：{parent_seen}"

    # ② 测试自己当模型：往交接文件写 NEXT: continue
    handover = store.task_dir(task_id) / "handover-1.md"
    handover.write_text(
        "第一班干完了登录页，还差支付页。\nNEXT: continue\n", encoding="utf-8"
    )

    # ③ 第二次 serve --once：父转 chained，后继进 scheduled（本 tick 不起跑）
    out2 = serve_once()
    parent_status = store.read_status(task_id)
    assert parent_status["state"] == "chained"
    successor_id = parent_status["successor_id"]
    assert successor_id and successor_id in out2
    assert store.read_status(successor_id)["state"] == "scheduled"
    successor = store.load_task(successor_id)
    assert successor["shift"] == 2
    assert successor["parent_id"] == task_id
    assert "还差支付页" in successor["prompt_final"]
    assert "第 2 班" in successor["prompt_final"]
    # 假 claude 还是只被起过一次（后继要等下一轮预检）
    assert trusted_env["fake_log"].read_text(
        encoding="utf-8"
    ).count("--session-id") == 1

    # ④ 第三次 serve --once：后继走完整预检后起跑 → exited
    serve_once()
    succ_status, succ_seen = wait_for_state(successor_id)
    assert succ_status["state"] == "exited", f"后继没等到 exited：{succ_seen}"
    assert trusted_env["fake_log"].read_text(
        encoding="utf-8"
    ).count("--session-id") == 2

    # ⑤ 交接写 NEXT: done → 后继 awaiting_merge（worktree=true + manual，
    #    不再直接 finished）；收工边界打过存档点账（假 claude 没改文件 → 不 commit）
    (store.task_dir(successor_id) / "handover-2.md").write_text(
        "支付页也干完了。\nNEXT: done\n", encoding="utf-8"
    )
    out4 = serve_once()
    succ_status = store.read_status(successor_id)
    assert succ_status["state"] == "awaiting_merge"
    assert "awaiting_merge" in out4
    assert succ_status.get("checkpoint_done") is True
    assert "未打存档点" in (store.task_dir(successor_id) / "events.log").read_text(
        encoding="utf-8"
    )
    # 整条链只有一棵树：后继沿用首班的 worktree 元数据
    parent_status = store.read_status(task_id)
    assert parent_status["state"] == "chained"
    for key in ("worktree_path", "branch", "base_ref"):
        assert succ_status[key] == parent_status[key]
    # 一期路径回归：显式 worktree=false 的任务 NEXT: done 仍 finished
    old_task = store.create_task(
        {
            "title": "老式集成", "project": "demo", "model": "claude-fable-5",
            "effort": "high", "run_at": "2026-08-27T00:00:00Z",
            "task_text": "正文", "prompt_final": "只回复：好", "worktree": False,
        },
        config,
    )
    serve_once()
    wait_for_state(old_task)
    (store.task_dir(old_task) / "handover-1.md").write_text(
        "干完了。\nNEXT: done\n", encoding="utf-8"
    )
    serve_once()
    assert store.read_status(old_task)["state"] == "finished"
