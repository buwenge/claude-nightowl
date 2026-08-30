"""Codex nightowl profile + hooks.json：渲染/校验，不直接覆盖生产文件。

S6 靶测证伪了开工令最初"hooks 装在具名 profile 里"的假设：官方文档原文
"Codex discovers hooks next to active config layers"，四个落点只有
``~/.codex/hooks.json``、``~/.codex/config.toml``、``<repo>/.codex/hooks.json``、
``<repo>/.codex/config.toml``，不含 ``$CODEX_HOME/<name>.config.toml``（具名
profile）；真机把 hooks 塞进 profile 一次没有触发信任提示、目标脚本没被调用。
复测通过的形态（详见 reports/夜班-S6-靶测记录.md 第 9 项）：

- ``~/.codex/hooks.json``（用户级，全局）：七件固定 hooks，命令一律
  ``python3 -m nightshift.hook --codex <event>``，task id 从 run.sh
  export 的 ``NIGHTOWL_TASK_ID`` 环境变量读；读不到就静默 no-op（hook.py
  已有逻辑），日常交互式 codex/Sol 会话不受影响，只多一次几毫秒的子进程
  启动开销。**这份文件是用户级、全局的，装上之后不论谁先起下一个 codex
  会话（夜班任务窗口，还是工头自己手动开的 Sol），都会撞到一次性的
  "Hooks need review" 信任提示**——这就是开工令要求的"正常人工确认"，
  但可能落在工头自己的交互会话里，部署时要提前告知。
- ``~/.codex/nightowl.config.toml``（profile，``--profile nightowl`` 加载）：
  只放 approval_policy / sandbox_mode / notify / sandbox_workspace_write，
  不含 hooks、不含任何具体任务、项目路径或私人信息。

本模块只管渲染与冲突检测，不写生产文件——安装是监理按验收单给的原子命令
手动做的一次性动作（含首次 hook 信任的人工确认步骤，施工班自己不执行）。
"""

from __future__ import annotations

import sys

__all__ = [
    "HOOKS_FILENAME",
    "PROFILE_FILENAME",
    "PROFILE_NAME",
    "hooks_json",
    "profile_toml_text",
]

PROFILE_NAME = "nightowl"
PROFILE_FILENAME = f"{PROFILE_NAME}.config.toml"
# 用户级、全局：不是这份 profile 的一部分（见模块 docstring）
HOOKS_FILENAME = "hooks.json"

# 七件固定 hooks（开工令 §commit②）。SessionEnd 的 timeout 会被 CLI 强制
# 夹到 3 秒（真机实测警告：clamping SessionEnd hook timeout to 3s），
# 这里如实写 3，不写一个会被悄悄改写的假值。
_HOOK_TIMEOUTS = {
    "SessionStart": 10,
    "UserPromptSubmit": 10,
    "PostToolUse": 10,
    "SubagentStart": 10,
    "SubagentStop": 10,
    "Stop": 10,
    "SessionEnd": 3,
}


def hooks_json() -> dict:
    """``~/.codex/hooks.json`` 的内容：命令不随任务变（改一字都要重新走信任），
    task id 全部走 ``NIGHTOWL_TASK_ID`` 环境变量。"""

    def entry(event: str) -> dict:
        return {
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": f"{sys.executable} -m nightshift.hook --codex {event}",
                "timeout": _HOOK_TIMEOUTS[event],
            }],
        }

    return {
        "description": "nightshift/nightowl 固定 hooks —— 内容不随任务变，改一字要重新走信任",
        "hooks": {event: [entry(event)] for event in _HOOK_TIMEOUTS},
    }


def profile_toml_text(config: dict) -> str:
    """``~/.codex/nightowl.config.toml`` 的内容：approval/sandbox/notify，不含 hooks。

    network_access 缺省关闭（比现有个人 Sol 配置更保守——无人值守跑代码，
    默认不给网络；config.runners.codex.network_access=true 才开）。
    """
    rc = (config.get("runners") or {}).get("codex") or {}
    network_access = bool(rc.get("network_access", False))
    lines = [
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
        f'notify = ["{sys.executable}", "-m", "nightshift.codex_notify"]',
        "",
        "[sandbox_workspace_write]",
        f"network_access = {'true' if network_access else 'false'}",
    ]
    return "\n".join(lines) + "\n"
