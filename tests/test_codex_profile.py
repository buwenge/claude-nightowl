"""codex_profile.py 的测试：hooks.json / nightowl.config.toml 渲染。

只测渲染出的内容形状，不写生产文件——安装是监理照验收单手动做的一次性动作。
S6.1 A2 的两条真实子进程测试例外：那条修复本身就是"这条 shell 命令在
真实进程里跑起来到底是不是 no-op"，字符串断言证明不了，必须真跑。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from nightshift import codex_profile, store

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_hooks_json_shape_and_seven_events():
    doc = codex_profile.hooks_json()
    assert set(doc["hooks"]) == {
        "SessionStart", "UserPromptSubmit", "PostToolUse",
        "SubagentStart", "SubagentStop", "Stop", "SessionEnd",
    }
    assert "description" in doc
    for event, entries in doc["hooks"].items():
        assert len(entries) == 1
        entry = entries[0]
        assert entry["matcher"] == ""
        inner = entry["hooks"][0]
        assert inner["type"] == "command"
        # S6.1 A2：不能裸跑 python——先在 shell 层判断 NIGHTOWL_TASK_ID，
        # 没有就 exit 0 压根不碰 python（真机实测过裸跑会 ModuleNotFoundError）
        assert inner["command"] == (
            f'[ -n "$NIGHTOWL_TASK_ID" ] && exec {sys.executable} '
            f"-m nightshift.hook --codex {event} || exit 0"
        )
        # SessionEnd 会被 CLI 强制夹到 3 秒（真机实测警告），如实写 3
        assert inner["timeout"] == (3 if event == "SessionEnd" else 10)


def test_hooks_json_command_has_no_per_task_content():
    """命令必须不随任务变——改一字都要重新走信任，绝不能塞任务 id 之类的东西。
    NIGHTOWL_TASK_ID 是固定的环境变量名（不是某个具体任务的 id），要先剔除
    才能判断命令本身有没有偷偷带任务相关内容。"""
    doc = codex_profile.hooks_json()
    for entries in doc["hooks"].values():
        command = entries[0]["hooks"][0]["command"]
        stripped = (
            command.lower()
            .replace("nightshift.hook", "")
            .replace("nightowl_task_id", "")
        )
        assert "task" not in stripped


def test_hook_command_real_noop_without_task_context(tmp_path):
    """A2 真机复现的反面：普通 cwd、环境里同时没有 NIGHTOWL_TASK_ID 和
    PYTHONPATH（模拟用户自己交互式开 codex），七件生成命令逐个真跑，
    必须 exit 0、stdout/stderr 都是空——绝不能再裸跑 python 炸
    ModuleNotFoundError 产生全局噪音。"""
    doc = codex_profile.hooks_json()
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("NIGHTOWL_TASK_ID", "PYTHONPATH")
    }
    for event, entries in doc["hooks"].items():
        command = entries[0]["hooks"][0]["command"]
        proc = subprocess.run(
            ["sh", "-c", command], cwd=str(tmp_path), env=env,
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0, f"{event}: rc={proc.returncode} stderr={proc.stderr!r}"
        assert proc.stdout == "", f"{event}: stdout={proc.stdout!r}"
        assert proc.stderr == "", f"{event}: stderr={proc.stderr!r}"


def test_hook_command_still_routes_to_task_with_full_env(tmp_path):
    """夜班自己的环境（NIGHTOWL_TASK_ID + PYTHONPATH 都在，run.sh 保证两者
    总是一起 export）：shell 判断为真，真的执行到 nightshift.hook 并落到
    目标任务的 events.log——不是无论如何都 exit 0 的假 no-op。"""
    task_id = "20260830-000000-cafe"
    d = None
    env = dict(os.environ)
    env["NIGHTSHIFT_HOME"] = str(tmp_path)
    env["NIGHTOWL_TASK_ID"] = task_id
    env["PYTHONPATH"] = str(REPO_ROOT)
    old_home = os.environ.get("NIGHTSHIFT_HOME")
    os.environ["NIGHTSHIFT_HOME"] = str(tmp_path)
    try:
        d = store.task_dir(task_id)
        d.mkdir(parents=True)
        store.atomic_write_json(d / "task.json", {"id": task_id, "title": "x", "runner": "codex"})
    finally:
        if old_home is None:
            os.environ.pop("NIGHTSHIFT_HOME", None)
        else:
            os.environ["NIGHTSHIFT_HOME"] = old_home

    doc = codex_profile.hooks_json()
    command = doc["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    proc = subprocess.run(
        ["sh", "-c", command], cwd="/tmp", env=env,
        input=json.dumps({"session_id": "sess-1"}),
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    events = (d / "events.log").read_text(encoding="utf-8")
    assert "UserPromptSubmit" in events


def test_profile_toml_text_no_hooks_no_private_info():
    """profile 里不含 hooks（S6 靶测证伪了这个假设，见靶测记录第 9 项），
    也不含任何具体项目路径/任务信息。"""
    config = {"runners": {"codex": {}}}
    text = codex_profile.profile_toml_text(config)
    assert "hooks" not in text.lower()
    assert 'approval_policy = "never"' in text
    assert 'sandbox_mode = "workspace-write"' in text
    assert "nightshift.codex_notify" in text
    assert "network_access = false" in text  # 缺省关网络
    assert "/root/" not in text
    assert "task" not in text.lower()


def test_profile_toml_text_network_access_opt_in():
    config = {"runners": {"codex": {"network_access": True}}}
    text = codex_profile.profile_toml_text(config)
    assert "network_access = true" in text


def test_profile_filenames():
    assert codex_profile.PROFILE_NAME == "nightowl"
    assert codex_profile.PROFILE_FILENAME == "nightowl.config.toml"
    assert codex_profile.HOOKS_FILENAME == "hooks.json"
