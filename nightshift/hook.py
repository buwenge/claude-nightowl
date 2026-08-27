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
    """读 transcript 刷新 context_tokens / context_pct（读不到就置 None）。"""
    transcript_path = payload.get("transcript_path")
    fields["context_tokens"] = None
    fields["context_pct"] = None
    if not transcript_path:
        return
    tokens = read_context_tokens(transcript_path)
    fields["context_tokens"] = tokens
    if tokens is None:
        return
    limit = (task.get("guards") or {}).get("context_limit_tokens")
    if not limit:
        from .store import load_config

        limit = context_limit_for(task.get("model", ""), load_config())
    if limit:
        fields["context_pct"] = round(100 * tokens / limit)


def handle_event(task_id: str, event: str, payload: dict) -> None:
    """按事件类型更新 status.json / events.log。"""
    task = store.load_task(task_id)
    status = store.read_status(task_id)
    now = store.utc_now_iso()

    if event == "UserPromptSubmit":
        store.update_status(
            task_id,
            state="working",
            turns=int(status.get("turns") or 0) + 1,
            session_id=payload.get("session_id"),
            transcript_path=payload.get("transcript_path"),
            last_event_at=now,
        )
        store.append_event(task_id, "hook UserPromptSubmit → working")

    elif event in ("SubagentStart", "SubagentStop"):
        delta = 1 if event == "SubagentStart" else -1
        current = int(status.get("subagents_running") or 0)
        fields: dict = {
            "subagents_running": max(0, current + delta),
            "last_event_at": now,
        }
        if payload.get("agent_type"):
            fields["agent_type"] = payload["agent_type"]
        store.update_status(task_id, **fields)
        store.append_event(task_id, f"hook {event} subagents={fields['subagents_running']}")

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
        calls = int(status.get("tool_calls") or 0) + 1
        fields = {"tool_calls": calls, "last_event_at": now}
        if calls % 20 == 0:  # 每 20 次工具调用刷新一次上下文水位（回注是 S3 的事）
            _refresh_context(task, payload, fields)
        store.update_status(task_id, **fields)
        if calls % 20 == 0:
            store.append_event(task_id, f"hook PostToolUse #{calls} 刷新上下文")

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
