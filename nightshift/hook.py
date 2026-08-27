"""Claude Code hook 接收端：`python3 -m nightshift.hook <task_id> <event>`。

规矩：
- stdin 收 CC 给的 JSON（空/坏 JSON 一律当 {}）；
- 任务目录不存在 → 静默退出；
- 任何异常都吞掉、记 events.log 后退出码 0；stdout 永远不输出任何东西
  （CC 会把 stdout 当 hook 结果解析）；
- 整个进程 100 ms 量级完成，不 import 重东西。
"""

from __future__ import annotations

import json
import sys

from . import store
from .context import context_limit_for, read_context_tokens

__all__ = ["handle_event", "main"]


def _refresh_context(task: dict, payload: dict, fields: dict) -> None:
    """读 transcript 刷新 context_tokens / context_pct（读不到就置 None）。

    算上限失败（config 缺/坏）只记 events.log，不拖垮整个事件：
    context_tokens 照常写，context_pct 留空。
    """
    transcript_path = payload.get("transcript_path")
    fields["context_tokens"] = None
    fields["context_pct"] = None
    if not transcript_path:
        return
    tokens = read_context_tokens(transcript_path)
    fields["context_tokens"] = tokens
    if tokens is None:
        return
    try:
        limit = (task.get("guards") or {}).get("context_limit_tokens")
        if not limit:
            from .store import load_config

            limit = context_limit_for(task.get("model", ""), load_config())
    except Exception as exc:  # config 缺失/损坏等，只留痕不炸
        store.append_event(task["id"], f"算不出上限：{exc!r}")
        return
    if limit:
        fields["context_pct"] = round(100 * tokens / limit)


def handle_event(task_id: str, event: str, payload: dict) -> None:
    """按事件类型更新 status.json / events.log。

    turns / tool_calls / subagents_running 这类计数增量必须整个在
    modify_status 的锁内读改写——两个 hook 进程并行时会互相吃掉增量。
    """
    task = store.load_task(task_id)
    now = store.utc_now_iso()

    if event == "UserPromptSubmit":

        def bump_turns(status: dict) -> None:
            status["state"] = "working"
            status["turns"] = int(status.get("turns") or 0) + 1
            status["session_id"] = payload.get("session_id")
            status["transcript_path"] = payload.get("transcript_path")
            status["last_event_at"] = now

        store.modify_status(task_id, bump_turns)
        store.append_event(task_id, "hook UserPromptSubmit → working")

    elif event in ("SubagentStart", "SubagentStop"):
        delta = 1 if event == "SubagentStart" else -1

        def bump_subagents(status: dict) -> None:
            status["subagents_running"] = max(
                0, int(status.get("subagents_running") or 0) + delta
            )
            status["last_event_at"] = now
            if payload.get("agent_type"):
                status["agent_type"] = payload["agent_type"]

        status = store.modify_status(task_id, bump_subagents)
        store.append_event(
            task_id, f"hook {event} subagents={status['subagents_running']}"
        )

    elif event == "Stop":
        background_tasks = payload.get("background_tasks") or []
        fields = {
            "background_tasks": background_tasks,
            "last_message": (payload.get("last_assistant_message") or "")[:2000],
            "last_event_at": now,
        }
        _refresh_context(task, payload, fields)
        fields["state"] = (
            "waiting_background"
            if any(t.get("status") == "running" for t in background_tasks)
            else "idle"
        )
        store.update_status(task_id, **fields)
        store.append_event(task_id, f"hook Stop → {fields['state']}")

    elif event == "PostToolUse":
        refresh = False

        def bump_tool_calls(status: dict) -> None:
            nonlocal refresh
            calls = int(status.get("tool_calls") or 0) + 1
            status["tool_calls"] = calls
            status["last_event_at"] = now
            if calls % 20 == 0:  # 每 20 次工具调用刷新一次上下文水位（回注是 S3 的事）
                refresh = True

        status = store.modify_status(task_id, bump_tool_calls)
        if refresh:  # 锁内算出的新值；上下文字段不是计数，锁外合并即可
            fields: dict = {}
            _refresh_context(task, payload, fields)
            store.update_status(task_id, **fields)
            store.append_event(
                task_id, f"hook PostToolUse #{status['tool_calls']} 刷新上下文"
            )

    elif event == "PreCompact":
        store.append_event(task_id, "hook PreCompact（有人开了 compact？）")

    elif event == "SessionEnd":
        store.update_status(
            task_id,
            state="exited",
            exit_reason=payload.get("reason"),
            last_event_at=now,
        )
        store.append_event(task_id, f"hook SessionEnd reason={payload.get('reason')}")

    else:
        store.append_event(task_id, f"hook 未处理事件 {event}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        return 0
    task_id, event = argv[0], argv[1]

    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    payload: dict = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            payload = {}

    if not (store.task_dir(task_id) / "task.json").is_file():
        return 0

    try:
        handle_event(task_id, event, payload)
    except Exception as exc:  # 吞掉一切，只留痕
        try:
            store.append_event(task_id, f"hook {event} 异常：{exc!r}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
