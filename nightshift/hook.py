"""Claude Code hook 接收端：`python3 -m nightshift.hook <task_id> <event>`。

规矩：
- stdin 收 CC 给的 JSON（空/坏 JSON 一律当 {}）；
- 任务目录不存在 → 静默退出；
- 任何异常都吞掉、记 events.log 后退出码 0；
- stdout 只在回注时打印且只打印一个 hookSpecificOutput JSON 对象
  （additionalContext 会作为系统侧上下文塞进本轮工具结果后面给模型看；
  8/27 实测的协议）。其余时候、其余事件一律沉默——CC 会把 stdout 当
  hook 结果解析，Stop 等事件的 stdout 语义不同，不许碰；
- 整个进程 100 ms 量级完成，不 import 重东西。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import store
from .context import context_limit_for, read_context_tokens

__all__ = ["handle_event", "handover_path", "main", "warn_threshold"]

# 额度到线的回注文案（占位符由 store.render 做字面替换）
QUOTA_WARN_TEXT = (
    "[nightshift] 账号额度：五小时 {session_pct}% / 七日 {week_all_pct}%"
    "（线 {session_max}%/{weekly_max}%），已到线或将到线，请尽快收尾并写交接。"
)


def _context_limit(task: dict, config: dict) -> int:
    """上下文上限：任务 guards 里的 context_limit_tokens 优先，否则按模型查 config。"""
    limit = (task.get("guards") or {}).get("context_limit_tokens")
    if limit:
        return int(limit)
    return context_limit_for(task.get("model", ""), config)


def warn_threshold(task: dict, config: dict) -> int:
    """回注警戒线（tokens）：guards.context_warn_tokens 有就用；
    否则 context_warn_ratio × 上下文上限（上限算法同 _refresh_context）。"""
    guards = task.get("guards") or {}
    explicit = guards.get("context_warn_tokens")
    if explicit is not None:
        return int(explicit)
    ratio = guards.get("context_warn_ratio")
    if ratio is None:
        ratio = (config.get("guards") or {}).get("context_warn_ratio")
    if ratio is None:
        raise ValueError("guards 里既没有 context_warn_tokens 也没有 context_warn_ratio")
    return int(ratio * _context_limit(task, config))


def handover_path(task: dict) -> Path:
    """交接文件路径：task_dir/handover-<shift>.md（shift 从 task.json 取，默认 1）。"""
    shift = int(task.get("shift") or 1)
    return store.task_dir(task["id"]) / f"handover-{shift}.md"


def _read_fresh_usage(config: dict) -> dict | None:
    """读 home()/quota.json（调度器在刷新），返回 usage dict。

    文件缺/坏 JSON/缺键/`fetched_at` 超过 2 × scheduler.quota_refresh_minutes
    都当没有——回注提醒宁缺勿滥，过期额度只会吓唬人。
    """
    path = store.home() / "quota.json"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        refresh_minutes = (config.get("scheduler") or {}).get(
            "quota_refresh_minutes", 30
        )
        max_age_seconds = 2 * float(refresh_minutes) * 60
        age = datetime.now(timezone.utc) - datetime.fromisoformat(data["fetched_at"])
        if age.total_seconds() > max_age_seconds:
            return None
        usage = data["usage"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return usage if isinstance(usage, dict) else None


def _quota_warning(task: dict, config: dict) -> str:
    """额度到线判定：五小时 ≥ session_pct_max、七日全部 ≥ weekly_pct_max、
    任务模型自己的单模型周线 ≥ weekly_pct_max，任一命中就回注一段文案。
    不命中 / 没有新鲜额度返回空串。"""
    guards = task.get("guards") or {}
    session_max = guards.get("session_pct_max")
    weekly_max = guards.get("weekly_pct_max")
    if session_max is None or weekly_max is None:
        return ""
    usage = _read_fresh_usage(config)
    if usage is None:
        return ""
    session_pct = usage.get("session_pct")
    week_all_pct = usage.get("week_all_pct")
    hit = (
        (isinstance(session_pct, int) and session_pct >= session_max)
        or (isinstance(week_all_pct, int) and week_all_pct >= weekly_max)
    )
    if not hit:
        label = (config.get("models") or {}).get(task.get("model", ""), {}).get(
            "usage_label"
        )
        pct = (usage.get("per_model") or {}).get(label)
        if isinstance(pct, int) and pct >= weekly_max:
            hit = True
    if not hit:
        return ""
    return store.render(
        QUOTA_WARN_TEXT,
        session_pct="?" if session_pct is None else session_pct,
        week_all_pct="?" if week_all_pct is None else week_all_pct,
        session_max=session_max,
        weekly_max=weekly_max,
    )


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
        limit = _context_limit(task, store.load_config())
    except Exception as exc:  # config 缺失/损坏等，只留痕不炸
        store.append_event(task["id"], f"算不出上限：{exc!r}")
        return
    if limit:
        fields["context_pct"] = round(100 * tokens / limit)


def _over_warn_line(task: dict, tokens: int | None) -> bool:
    """Stop 用：当前水位是否已过回注警戒线（写进 status 供调度器换班判断）。"""
    if tokens is None:
        return False
    try:
        return tokens >= warn_threshold(task, store.load_config())
    except Exception:
        return False


def _post_tool_use_refresh(task: dict, status: dict, payload: dict) -> str | None:
    """每 20 次工具调用一次的上下文刷新 + 回注判定。

    返回要回注给模型的文案（None = 沉默）。上下文与额度两种提醒同时命中时
    合成一段（空行隔开），只回注一次。状态写盘全部完成后才返回文案，
    打印交给 main（打印失败也不许拖垮已落盘的状态）。
    """
    task_id = task["id"]
    now = store.utc_now_iso()
    fields: dict = {}
    _refresh_context(task, payload, fields)
    store.update_status(task_id, **fields)
    store.append_event(task_id, f"hook PostToolUse #{status['tool_calls']} 刷新上下文")

    try:
        config = store.load_config()
    except Exception:
        config = None
    inject: list[str] = []
    extra: dict = {}
    if config is not None:
        tokens = fields.get("context_tokens")
        if tokens is not None:
            try:
                threshold = warn_threshold(task, config)
                limit = _context_limit(task, config)
            except Exception as exc:
                store.append_event(task_id, f"算不出警戒线：{exc!r}")
            else:
                if tokens >= threshold:
                    text = (task.get("guards") or {}).get(
                        "context_warn_text"
                    ) or config.get("context_warn_text")
                    if text:
                        ctx = store.render(
                            text,
                            ctx_k=round(tokens / 1000),
                            limit_k=round(limit / 1000),
                            handover_path=str(handover_path(task)),
                        )
                        inject.append(ctx)
                        count = int(status.get("context_warn_count") or 0) + 1
                        extra["context_warned_at"] = (
                            status.get("context_warned_at") or now
                        )
                        extra["context_warn_count"] = count
                        extra["handover_path"] = str(handover_path(task))
                        store.append_event(
                            task_id, f"回注上下文提醒 #{count}（{tokens}）"
                        )
        quota_text = _quota_warning(task, config)
        if quota_text:
            inject.append(quota_text)
            count = int(status.get("quota_warn_count") or 0) + 1
            extra["quota_warned_at"] = status.get("quota_warned_at") or now
            extra["quota_warn_count"] = count
            store.append_event(task_id, f"回注额度提醒 #{count}")
    if extra:
        store.update_status(task_id, **extra)
    return "\n\n".join(inject) if inject else None


def handle_event(task_id: str, event: str, payload: dict) -> str | None:
    """按事件类型更新 status.json / events.log，返回要回注的文案（通常 None）。

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
            # 实测 --permission-mode auto 会被 CC 对某些模型静默回落成 default，
            # 回报里带着实际生效的模式，调度器靠它开提醒窗（R2）；没有就不覆盖旧值
            if payload.get("permission_mode"):
                status["permission_mode"] = payload["permission_mode"]
            status["last_event_at"] = now

        store.modify_status(task_id, bump_turns)
        store.append_event(task_id, "hook UserPromptSubmit → working")

    elif event in ("SubagentStart", "SubagentStop"):
        delta = 1 if event == "SubagentStart" else -1
        agent_id = payload.get("agent_id")

        def bump_subagents(status: dict) -> None:
            if agent_id:
                # 按 agent_id 记集合：并发 Start/Stop 乱序也不再丢计数。
                # Stop 先到（agent 还没记上）时落一枚墓碑，把迟到的 Start 拦下，
                # 否则"无论顺序最后归零"根本保证不了——这正是原计数抖动的根。
                active = list(status.get("subagents") or [])
                retired = list(status.get("subagents_retired") or [])
                if delta == 1:
                    if agent_id not in retired and agent_id not in active:
                        active.append(agent_id)
                else:
                    if agent_id in active:
                        active.remove(agent_id)
                    if agent_id not in retired:
                        retired.append(agent_id)
                status["subagents"] = active
                status["subagents_retired"] = retired
                status["subagents_running"] = len(active)
            else:
                # 没有 agent_id 的回报退回原来的计数逻辑
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
        # Stop 不回注（stdout 语义不同），但同样刷新水位并把"是否过警戒线"
        # 落盘，供调度器换班判断（"这班收到过注入"用 context_warned_at 判）
        fields["over_warn_line"] = _over_warn_line(task, fields["context_tokens"])
        fields["state"] = (
            "waiting_background"
            if any(t.get("status") == "running" for t in background_tasks)
            else "idle"
        )
        store.update_status(task_id, **fields)
        store.append_event(task_id, f"hook Stop → {fields['state']}")
        return None

    elif event == "PostToolUse":
        refresh = False

        def bump_tool_calls(status: dict) -> None:
            nonlocal refresh
            calls = int(status.get("tool_calls") or 0) + 1
            status["tool_calls"] = calls
            status["last_event_at"] = now
            if calls % 20 == 0:  # 每 20 次工具调用刷新一次上下文水位
                refresh = True

        status = store.modify_status(task_id, bump_tool_calls)
        if refresh:  # 锁内算出的新值；上下文字段不是计数，锁外合并即可
            return _post_tool_use_refresh(task, status, payload)

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

    inject: str | None = None
    try:
        inject = handle_event(task_id, event, payload)
    except Exception as exc:  # 吞掉一切，只留痕
        try:
            store.append_event(task_id, f"hook {event} 异常：{exc!r}")
        except Exception:
            pass

    if inject:
        # 只有回注时打印，且只打印这一个 JSON 对象（末尾换行）；
        # 状态写盘早已完成，打印失败也不许抛
        try:
            sys.stdout.write(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": inject,
                        }
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            sys.stdout.flush()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
