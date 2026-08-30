"""Codex 官方 notify 端点：``notify = ["python3", "-m", "nightshift.codex_notify"]``。

只负责 turn 完成记账 / 兜底观测（真机实测事件类型 ``agent-turn-complete``，
连内部生成标题的那次子请求也会各触发一次，thread-id 与主会话不同），
**不是**任意外部进程的 exit callback——F12 的后台命令完成回调另有
``background_runner.py`` 的登记簿 + scheduler 主动唤醒，notify 绝不能冒充它，
也不许把任务直接标 idle/finished。

任务 id 从 ``NIGHTOWL_TASK_ID`` 环境变量读（run.sh 已 export）；坏 JSON、
缺 task、任务目录不存在都要 fail-safe 静默退出，不许把任务状态写坏。
"""

from __future__ import annotations

import json
import os
import sys

from . import store

__all__ = ["handle_notify", "main"]


def handle_notify(task_id: str, payload: dict) -> None:
    """只记一笔事件日志，不碰 status.json——状态权威来源永远是 Stop hook。"""
    if not (store.task_dir(task_id) / "task.json").is_file():
        return
    kind = payload.get("type") or "?"
    thread_id = payload.get("thread-id") or payload.get("thread_id") or "?"
    turn_id = payload.get("turn-id") or payload.get("turn_id") or "?"
    store.append_event(
        task_id, f"codex notify: {kind} thread={thread_id} turn={turn_id}"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    task_id = os.environ.get("NIGHTOWL_TASK_ID") or ""
    if not task_id or not argv:
        return 0
    try:
        payload = json.loads(argv[0])
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        handle_notify(task_id, payload)
    except Exception:  # 吞掉一切：notify 绝不能因为记账失败把 codex 卡住
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
