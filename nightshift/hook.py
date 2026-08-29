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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store
from .context import context_limit_for, read_context_tokens

__all__ = ["alarm_plan", "handle_event", "handover_path", "main", "warn_threshold"]

# 五小时额度到线：停下等刷新（缓存闹钟）；周额度到线：收尾交接。文案在 config 里可改。
QUOTA_PAUSE_TEXT = (
    "[nightshift] 五小时额度只剩 {session_left}%（线 {session_line_left}%），约 {resets_in} 分钟后刷新。"
    "现在停止干活，不要再开新的工具调用：按家规定缓存闹钟——用 ScheduleWakeup 连续设 {alarm_plan}，"
    "每个闹钟醒来只输出一个“·”再设下一个，最后一个闹钟醒来后从刚才停下的地方继续。"
)
# 别的模型的周线到了：只提示别去用它，不叫停本会话
QUOTA_OTHER_MODEL_TEXT = (
    "[nightshift] 提示：{label} 的周额度只剩 {left}%（线 {line_left}%）。本会话的模型不受影响，"
    "但接下来别再派 {label} 的子 agent、也别切到它。"
)
QUOTA_WRAPUP_TEXT = (
    "[nightshift] 周额度只剩 {week_left}%（线 {week_line_left}%）{model_note}，一时半会儿刷新不了。"
    "现在收尾：把已完成/未完成/下一步写进 {handover_path}，末行写 NEXT: done（本周不再续班，交接留给下次）；"
    "未提交的改动 commit；然后停下。"
)
# 闹钟规矩：50 分钟一个（缓存 TTL 约 1 小时），尾数补一个短的，再加几分钟缓冲
ALARM_UNIT_MINUTES = 50
ALARM_BUFFER_MINUTES = 3
ALARM_FALLBACK_COUNT = 6  # 刷新时间没查到：按五小时最长等


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


def alarm_plan(resets_in: int | None) -> tuple[str, int]:
    """把"几分钟后刷新"排成闹钟串，返回 (文案, 总分钟)。"""
    if resets_in is None:
        total = ALARM_UNIT_MINUTES * ALARM_FALLBACK_COUNT
        return f"{ALARM_UNIT_MINUTES} 分钟 × {ALARM_FALLBACK_COUNT} 个（刷新时间没查到，按最长等）", total
    total = resets_in + ALARM_BUFFER_MINUTES
    full, rem = divmod(total, ALARM_UNIT_MINUTES)
    parts = [f"{ALARM_UNIT_MINUTES} 分钟"] * full
    if rem:
        parts.append(f"{rem} 分钟")
    if not parts:
        parts = [f"{ALARM_BUFFER_MINUTES} 分钟"]
        total = ALARM_BUFFER_MINUTES
    return "、".join(parts) + f"（共 {len(parts)} 个）", total


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


def _other_model_notes(task: dict, config: dict, status: dict) -> list[tuple[str, str]]:
    """不是本任务模型的单模型周线到了 model_weekly_pct_max → 每个模型提示一次。"""
    guards = task.get("guards") or {}
    model_max = guards.get("model_weekly_pct_max", guards.get("weekly_pct_max"))
    if model_max is None:
        return []
    usage = _read_fresh_usage(config)
    if usage is None:
        return []
    own = (config.get("models") or {}).get(task.get("model", ""), {}).get("usage_label")
    warned = set(status.get("other_model_warned") or [])
    out = []
    for label, pct in (usage.get("per_model") or {}).items():
        if label == own or label in warned or not isinstance(pct, int) or pct < model_max:
            continue
        out.append((label, store.render(
            config.get("quota_other_model_text") or QUOTA_OTHER_MODEL_TEXT,
            label=label, left=100 - pct, line_left=100 - model_max,
        )))
    return out


def _quota_check(task: dict, config: dict) -> tuple[str | None, str, dict]:
    """额度判定，返回 (种类, 回注文案, 要写进 status 的字段)。

    种类：
    - "wrapup"：七日全模型线或任务模型自己的周线到了 → 收尾交接（优先级高，刷新不了）；
    - "pause"：五小时线到了 → 停下定缓存闹钟等刷新；
    - None：都没到，或没有新鲜额度。
    阈值取 task.guards.session_pct_max / weekly_pct_max（"已用"百分比）。
    """
    guards = task.get("guards") or {}
    session_max = guards.get("session_pct_max")
    weekly_max = guards.get("weekly_pct_max")
    model_max = guards.get("model_weekly_pct_max", weekly_max)  # 单模型周线单独一个数，没配就跟全模型线
    if session_max is None or weekly_max is None:
        return None, "", {}
    usage = _read_fresh_usage(config)
    if usage is None:
        return None, "", {}
    session_pct = usage.get("session_pct")
    week_all_pct = usage.get("week_all_pct")
    label = (config.get("models") or {}).get(task.get("model", ""), {}).get("usage_label")
    per_model = usage.get("per_model") or {}
    model_pct = per_model.get(label) if label else None

    week_hit = isinstance(week_all_pct, int) and week_all_pct >= weekly_max
    model_hit = isinstance(model_pct, int) and model_pct >= model_max
    if week_hit or model_hit:
        model_note = f"，{label} 单独周线剩 {100 - model_pct}%（线 {100 - model_max}%）" if model_hit else ""
        text = store.render(
            config.get("quota_wrapup_text") or QUOTA_WRAPUP_TEXT,
            week_left=("?" if week_all_pct is None else 100 - week_all_pct),
            week_line_left=100 - weekly_max,
            model_line_left=100 - model_max,
            model_note=model_note,
            handover_path=str(handover_path(task)),
        )
        return "wrapup", text, {}

    if isinstance(session_pct, int) and session_pct >= session_max:
        from .quota import resets_in_minutes

        resets_in = resets_in_minutes(usage.get("session_resets"))
        plan, total = alarm_plan(resets_in)
        text = store.render(
            config.get("quota_pause_text") or QUOTA_PAUSE_TEXT,
            session_left=100 - session_pct,
            session_line_left=100 - session_max,
            resets_in=("未知" if resets_in is None else resets_in),
            alarm_plan=plan,
        )
        paused_until = datetime.now(timezone.utc) + timedelta(minutes=total)
        return "pause", text, {"quota_paused_until": paused_until.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return None, "", {}


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
        # 别的模型周线到了：提示一次别去用它（sonnet 会话派 fable 子 agent 会撞限流）
        for label, note in _other_model_notes(task, config, status):
            inject.append(note)
            warned = list(status.get("other_model_warned") or [])
            warned.append(label)
            extra["other_model_warned"] = warned
            status["other_model_warned"] = warned
            store.append_event(task_id, f"回注其他模型周线提示：{label}")
        kind, quota_text, quota_fields = _quota_check(task, config)
        if kind == "wrapup":
            inject.append(quota_text)
            count = int(status.get("quota_warn_count") or 0) + 1
            # 周额度收尾等同"被提醒过要交接"，换班判定认 context_warned_at
            extra["context_warned_at"] = status.get("context_warned_at") or now
            extra["quota_warned_at"] = status.get("quota_warned_at") or now
            extra["quota_warn_count"] = count
            extra["handover_path"] = str(handover_path(task))
            store.append_event(task_id, f"回注周额度收尾提醒 #{count}")
        elif kind == "pause":
            inject.append(quota_text)
            count = int(status.get("quota_pause_count") or 0) + 1
            extra["quota_pause_count"] = count
            extra["quota_paused_at"] = now
            extra.update(quota_fields)
            store.append_event(
                task_id, f"回注五小时额度暂停提醒 #{count}，预计 {quota_fields.get('quota_paused_until')} 后可继续"
            )
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
            status["stuck"] = False  # S4：来新事件就是缓过来了，疑似卡住解除
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
            "stuck": False,  # S4：有事件就是缓过来了，疑似卡住解除
            "last_event_at": now,
        }
        _refresh_context(task, payload, fields)
        # Stop 不回注（stdout 语义不同），但同样刷新水位并把"是否过警戒线"
        # 落盘，供调度器换班判断（"这班收到过注入"用 context_warned_at 判）
        fields["over_warn_line"] = _over_warn_line(task, fields["context_tokens"])
        crons = payload.get("session_crons") or []
        fields["session_crons"] = crons
        if any(t.get("status") == "running" for t in background_tasks):
            fields["state"] = "waiting_background"
        elif crons:
            fields["state"] = "waiting_wakeup"  # 它自己定了闹钟（如额度暂停的缓存闹钟），不是干完了
        else:
            fields["state"] = "idle"
        store.update_status(task_id, **fields)
        store.append_event(task_id, f"hook Stop → {fields['state']}")
        return None

    elif event == "PostToolUse":
        refresh = False

        def bump_tool_calls(status: dict) -> None:
            nonlocal refresh
            calls = int(status.get("tool_calls") or 0) + 1
            status["tool_calls"] = calls
            status["stuck"] = False  # S4：有事件就是缓过来了，疑似卡住解除
            status["last_event_at"] = now
            if calls % 20 == 0:  # 每 20 次工具调用刷新一次上下文水位
                refresh = True

        status = store.modify_status(task_id, bump_tool_calls)
        if refresh:  # 锁内算出的新值；上下文字段不是计数，锁外合并即可
            return _post_tool_use_refresh(task, status, payload)

    elif event == "PreCompact":
        store.append_event(task_id, "hook PreCompact（有人开了 compact？）")

    elif event == "SessionEnd":
        # 已经收尾的终态（finished/chained/…）不被"关窗口"盖成 exited：
        # 网页上"已完成"不该因为用户关了窗就变"已退出"（8/27 真机发现）
        keep = ("finished", "chained", "chain_exhausted", "needs_attention")

        def mark_exit(status: dict) -> None:
            if status.get("state") not in keep:
                status["state"] = "exited"
            status["exit_reason"] = payload.get("reason")
            status["session_ended_at"] = now
            status["last_event_at"] = now

        store.modify_status(task_id, mark_exit)
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
