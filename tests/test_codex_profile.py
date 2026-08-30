"""codex_profile.py 的测试：hooks.json / nightowl.config.toml 渲染。

只测渲染出的内容形状，不写生产文件——安装是监理照验收单手动做的一次性动作。
"""

import sys

from nightshift import codex_profile


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
        assert inner["command"] == f"{sys.executable} -m nightshift.hook --codex {event}"
        # SessionEnd 会被 CLI 强制夹到 3 秒（真机实测警告），如实写 3
        assert inner["timeout"] == (3 if event == "SessionEnd" else 10)


def test_hooks_json_command_has_no_per_task_content():
    """命令必须不随任务变——改一字都要重新走信任，绝不能塞任务 id 之类的东西。"""
    doc = codex_profile.hooks_json()
    for entries in doc["hooks"].values():
        command = entries[0]["hooks"][0]["command"]
        assert "task" not in command.lower().replace("nightshift.hook", "")


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
