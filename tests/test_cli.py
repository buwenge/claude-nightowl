"""命令行入口的测试：add 的工作树旗标、serve --once 的启动对账。"""

import json

import pytest

from nightshift import __main__ as cli, store

CONFIG = {
    "tmux_session": "ns-selftest",
    "window_prefix": "ns:",
    "claude_bin": "claude",
    "probe_model": "claude-haiku-4-5-20251001",
    "display_tz_offset_hours": 8,
    "memory_max_bytes": 2147483648,
    "projects": {"demo": "/home/user/projects/demo"},
    "models": {"claude-fable-5": {"context_limit": 500000}},
    "default_context_limit": 200000,
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
    "chain": {"max_windows": 3, "on_no_handover": "continue"},
    "prompt_template": "任务 {title}。{worktree_instruction}正文：{task}",
    "chain_template": "{task} 第 {shift} 班 {handover}",
}


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    store.atomic_write_json(tmp_path / "config.json", CONFIG)
    return tmp_path


def add_args(*extra):
    return [
        "add", "--title", "CLI建的", "--project", "demo",
        "--model", "claude-fable-5", "--effort", "high",
        "--run-at", "2026-08-30 02:30", "--task-text", "正文", *extra,
    ]


def test_add_defaults_worktree_true_manual_policy():
    rc = cli.main(add_args())
    assert rc == 0
    items = store.list_tasks()
    assert len(items) == 1
    task = items[0]["task"]
    assert task["worktree"] is True
    assert task["review"] == {"enabled": False, "merge_policy": "manual"}
    # 提示词经统一 builder 渲染出运行时安全前言（网页所见即所发）
    assert store.WORKTREE_INSTRUCTION in task["prompt_final"]


def test_add_no_worktree_keeps_old_path():
    rc = cli.main(add_args("--no-worktree", "--merge-policy", "auto"))
    assert rc == 0
    task = store.list_tasks()[0]["task"]
    assert task["worktree"] is False
    assert task["review"] == {"enabled": False, "merge_policy": "auto"}
    # 老式任务的提示词不带工作树前言
    assert store.WORKTREE_INSTRUCTION not in task["prompt_final"]


def test_add_prompt_file_gets_runtime_preamble(tmp_path):
    """--prompt-file 的自定义全文也逃不掉工作树约束：launcher 写 prompt.txt
    时缺前言就补一层（task.json 里 prompt_final 保持用户原文）。"""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("自定义全文，没有前言。\n", encoding="utf-8")
    rc = cli.main(add_args("--prompt-file", str(prompt_file)))
    assert rc == 0
    task = store.list_tasks()[0]["task"]
    assert task["prompt_final"] == "自定义全文，没有前言。\n"
    assert task["worktree"] is True
    from nightshift import launcher
    launcher.write_task_files(task, CONFIG, "01234567-89ab-cdef-0123-456789abcdef")
    written = (store.task_dir(task["id"]) / "prompt.txt").read_text(encoding="utf-8")
    assert written.startswith(store.WORKTREE_INSTRUCTION)
    assert written.endswith("自定义全文，没有前言。\n")


def test_serve_once_runs_reconcile_and_tick():
    """--once 也跑一次启动对账：无孤儿也原子写 orphan_worktrees.json = []。"""
    rc = cli.main(["serve", "--once"])
    assert rc == 0
    data = json.loads(
        (store.home() / "orphan_worktrees.json").read_text(encoding="utf-8")
    )
    assert data == []
