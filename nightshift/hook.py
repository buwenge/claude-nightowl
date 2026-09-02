"""Claude/Codex hook 接收端：`python3 -m nightshift.hook <task_id> <event>`
（Claude）或 `python3 -m nightshift.hook --codex <event>`（Codex）。

规矩：
- stdin 收 CC/Codex 给的 JSON（空/坏 JSON 一律当 {}）；
- 任务目录不存在 → 静默退出；
- 任何异常都吞掉、记 events.log 后退出码 0；
- stdout 只在回注时打印且只打印一个 hookSpecificOutput JSON 对象
  （additionalContext 会作为系统侧上下文塞进本轮工具结果后面给模型看；
  8/27 实测的协议，只对 Claude 成立）。其余时候、其余事件一律沉默——CC
  会把 stdout 当 hook 结果解析，Stop 等事件的 stdout 语义不同，不许碰；
  Codex 一律不打印任何东西，守卫文案改由调度器 tmux send-keys 投递；
- 整个进程 100 ms 量级完成，不 import 重东西。
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store
from .context import context_limit_for, read_codex_context, read_context_tokens

__all__ = [
    "alarm_plan", "commit_step", "handle_event", "handover_path", "main",
    "review_file_path", "warn_threshold",
]

# G19：主会话撞额度线时若有子 agent 还在跑，先让它收尾/停掉再执行下面的
# 动作——不然子 agent 在主会话睡下/收尾之后仍在后台继续烧额度。四段"到线"
# 文案（build 的 pause/wrapup、review 的 pause/wrapup）末尾都加这句。
_SUBAGENT_HEADS_UP = "若有子 agent 在跑，先让它收尾或停掉，再执行上面的动作。"

# 五小时额度到线：停下等刷新（缓存闹钟）；周额度到线：收尾交接。文案在 config 里可改。
QUOTA_PAUSE_TEXT = (
    "[nightshift] 五小时额度只剩 {session_left}%（线 {session_line_left}%），约 {resets_in} 分钟后刷新。"
    "现在停止干活，不要再开新的工具调用：按家规定缓存闹钟——用 ScheduleWakeup 连续设 {alarm_plan}，"
    "每个闹钟醒来只输出一个“·”再设下一个，最后一个闹钟醒来后从刚才停下的地方继续。"
    f"{_SUBAGENT_HEADS_UP}"
)
# 别的模型的周线到了：只提示别去用它，不叫停本会话
QUOTA_OTHER_MODEL_TEXT = (
    "[nightshift] 提示：{label} 的周额度只剩 {left}%（线 {line_left}%）。本会话的模型不受影响，"
    "但接下来别再派 {label} 的子 agent、也别切到它。"
)
QUOTA_WRAPUP_TEXT = (
    "[nightshift] 周额度只剩 {week_left}%（线 {week_line_left}%）{model_note}，一时半会儿刷新不了。"
    "现在收尾：把已完成/未完成/下一步写进 {handover_path}，末行写 NEXT: done（本周不再续班，交接留给下次）；"
    "{commit_step}然后停下。"
    f"{_SUBAGENT_HEADS_UP}"
)
# S7：审稿班（role=review）额度到线收尾，跟 build 的收尾话术不同——
# 写进最终回复正文、末行 NEXT: pending，不叫它写交接文件或 commit。
DEFAULT_REVIEW_WRAPUP_TEXT = (
    "[nightshift] 审稿额度只剩 {week_left}%（线 {week_line_left}%）{model_note}，一时半会儿刷新不了。"
    "现在把已经看到的意见写进本次最终回复正文，末行写 NEXT: pending（本轮不计数），然后停下。"
    f"{_SUBAGENT_HEADS_UP}"
)
# S7.1 阻断二：review 撞五小时额度线时，不走 build 那套 ScheduleWakeup 多轮
# 自我唤醒闹钟（那套后续每个闹钟醒来的回复都不带 NEXT，会被 review 的 Stop
# 协议解析成"协议缺失→保守按 fix"，产生假退回）。review 角色改成当场一次
# 性停下、末行写 NEXT: pending——跟审稿自己的周额度收尾走同一套协议，之后
# 由 nightshift 调度器（scheduler._check_running 的 review 专属恢复分支）
# 按额度刷新时间主动敲它继续，不需要它自己设闹钟。
DEFAULT_REVIEW_QUOTA_PAUSE_TEXT = (
    "[nightshift] 审稿五小时额度只剩 {session_left}%（线 {session_line_left}%），约 {resets_in} 分钟后刷新。"
    "现在把已经看到的意见写进本次最终回复正文，末行写 NEXT: pending（本轮不计数）；"
    "不用自己设闹钟，额度刷新后 nightshift 会主动敲你继续这一轮审稿。"
    f"{_SUBAGENT_HEADS_UP}"
)
# G19：额度提醒同样要给正在跑的子 agent——上下文水位不给（量的是主会话），
# 但额度是两边共用的，主会话在前台等子 agent 时，欠账要等它下一次工具调用
# 才补，这段时间子 agent 一直烧。这段短文案直接打进子 agent 自己那次工具
# 结果，不写主会话的 quota_paused_until/quota_warned_at（那些仍由主会话
# 补注时落盘）。
SUBAGENT_QUOTA_NOTICE_TEXT = (
    "来自nightshift：账号额度已到线（五小时剩 {session_left}% / 七日剩 {week_left}%），"
    "请立刻收尾，把已有结果汇报给主会话后停下，不要再开新的工具调用。"
)
# S7.1 阻断二：review 撞上下文警戒线时，不能沿用 build 那套"写 handover、
# 末行 NEXT: continue/done"协议——review 没有 handover 概念，且 continue/
# done 不在 review 的 NEXT 三选一里，回复末行写 NEXT: continue 会被
# `_parse_review_verdict` 判协议缺失、保守转成 fix（假退回）。改成跟额度
# 收尾同款：当场把已经看到的意见写进最终回复正文，末行 NEXT: pending。
DEFAULT_REVIEW_CONTEXT_WARN_TEXT = (
    "[nightshift] 上下文已到 {ctx_k}k/{limit_k}k，快满了。"
    "现在把已经看到的意见写进本次最终回复正文，末行写 NEXT: pending（本轮不计数），然后停下；"
    "下一轮审稿会是一个新会话，接着看。"
)
# 闹钟规矩：50 分钟一个（缓存 TTL 约 1 小时），尾数补一个短的，再加几分钟缓冲
ALARM_UNIT_MINUTES = 50
ALARM_BUFFER_MINUTES = 3
ALARM_FALLBACK_COUNT = 6  # 刷新时间没查到：按五小时最长等


def _context_limit(task: dict, config: dict) -> int:
    """上下文上限：任务 guards 里的 context_limit_tokens 优先，否则按模型查 config。

    S6.1 二次返修 B3：本 hook 只服务 Claude（Codex 的 `_refresh_context` 在
    调用这条之前就已经 return 了），显式传 `runner="claude"` 而不是依赖
    `context_limit_for` 的默认参数，避免以后默认值改动时这里静默跟着变。
    """
    limit = (task.get("guards") or {}).get("context_limit_tokens")
    if limit:
        return int(limit)
    return context_limit_for(store.effective_model(task), config, runner="claude")


def warn_threshold(task: dict, config: dict, limit: int | None = None) -> int:
    """回注警戒线（tokens）：guards.context_warn_tokens 有就用；
    否则 context_warn_ratio × 上下文上限（上限算法同 _refresh_context）。

    总review三 H2：`limit` 允许调用方直接传入已经测得的上限——Codex 走
    `_context_limit`（内部按 runner="claude" 查模型表）查不到东西
    （`context_limit_for` 对非 claude runner 恒定 None），它自己的上限只能
    从 rollout 现读（`context.read_codex_context` 的 `model_context_window`），
    调用方测出来多少就传多少。不传时保持原样，走 `_context_limit`（Claude
    走这条）。
    """
    guards = task.get("guards") or {}
    explicit = guards.get("context_warn_tokens")
    if explicit is not None:
        return int(explicit)
    ratio = guards.get("context_warn_ratio")
    if ratio is None:
        ratio = (config.get("guards") or {}).get("context_warn_ratio")
    if ratio is None:
        raise ValueError("guards 里既没有 context_warn_tokens 也没有 context_warn_ratio")
    if limit is None:
        limit = _context_limit(task, config)
    return int(ratio * limit)


def commit_step(task: dict) -> str:
    """收尾话术里的 commit 那一步：工作树任务由调度器打存档点，渲染成空；
    老式任务（worktree=false）保留"把未提交的改动 commit"的原规矩。

    总review三 H3：scheduler.py 的 Codex 上下文收尾提醒（send-keys 投递，
    hook 的 stdout 回注对 Codex 不成立）复用这条渲染同一个占位符，不再各
    写一份——原来是模块内部私有的 `_commit_step`，改成公开名字。
    """
    return "" if store.worktree_enabled(task) else "把未提交的改动 commit；"


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
    """读 home()/quota.json 的 claude 分片（调度器在刷新），返回 usage dict。

    二次返修阻断二修正：S6 起 quota.json 是双 runner 分片
    `{"claude": {...}, "codex": {...}}`；这里必须走 `quota.load_quota_file()`
    只取 `claude` 那一份再判有效，不能再直接读文件顶层
    `data["fetched_at"]`/`data["usage"]`——双分片写法下顶层根本没有这两个
    键，会让这个 hook 无声失效（Claude 的五小时暂停、周线收尾、别的模型
    周线提示全部读不到新数据）。

    分片有 error、没有 usage dict、或 `fetched_at` 超过新鲜度上限都当没有
    ——回注提醒宁缺勿滥，过期额度只会吓唬人。

    总review F4：新鲜度上限是 `max(2 × quota_refresh_minutes, 30)` 分钟，
    不再是单纯的 2 倍。刷新间隔从 30 分钟缩到 5 分钟之后，"2 倍"只剩
    10 分钟——调度器一次 `/usage` 超时（120 s）或服务重启，回注就会静默
    失效（分片一过期就整段返回 None，五小时暂停/周线收尾/别的模型周线
    提示全部读不到）。下限 30 分钟保住刷新间隔缩短之前那档容忍度量级。
    """
    from .quota import load_quota_file

    slice_ = load_quota_file().get("claude") or {}
    if slice_.get("error"):
        return None
    usage = slice_.get("usage")
    if not isinstance(usage, dict):
        return None
    fetched_at = slice_.get("fetched_at")
    if not fetched_at:
        return None
    try:
        refresh_minutes = (config.get("scheduler") or {}).get(
            "quota_refresh_minutes", 30
        )
        max_age_seconds = max(2 * float(refresh_minutes), 30) * 60
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
        if age.total_seconds() > max_age_seconds:
            return None
    except (ValueError, TypeError):
        return None
    return usage


def _guard_line(task: dict, config: dict, key: str, fallback=None):
    """总review F7：guards 判定的统一口径——task.guards 缺这条线/为 null
    就回退 config.guards（与 D2 修后的 `quota.check_guards.line()` 一致：
    网页编辑把某条线清空就是"回到默认"，server 只做 task.update 不回填）。
    两处都没有就返回 fallback（通常是 None，调用方按"这条线没配，跳过
    这一条判定"处理，不拖累其余没有依赖它的判定）。
    """
    guards = task.get("guards") or {}
    cfg_guards = config.get("guards") or {}
    value = guards.get(key)
    if value is None:
        value = cfg_guards.get(key)
    return fallback if value is None else value


def _other_model_notes(task: dict, config: dict, status: dict) -> list[tuple[str, str]]:
    """不是本任务模型的单模型周线到了 model_weekly_pct_max → 每个模型提示一次。

    S6.1 二次返修 B3：模型表必须查 `store.runner_config(config)["claude"]`
    这份 Claude runner view，不能再看顶层 `config.models`——`runner_config`
    的语义是"`config.runners` 里有 `claude` 键就整个原样返回，不做字段级
    合并"，顶层 `models` 分裂出去之后就只是个过期快照。
    """
    model_max = _guard_line(
        task, config, "model_weekly_pct_max", _guard_line(task, config, "weekly_pct_max")
    )
    if model_max is None:
        return []
    usage = _read_fresh_usage(config)
    if usage is None:
        return []
    claude_rc = store.runner_config(config).get("claude") or {}
    own = claude_rc.get("models", {}).get(store.effective_model(task), {}).get("usage_label")
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
    阈值取 task.guards.session_pct_max / weekly_pct_max（"已用"百分比）；
    缺 key/None 回退 config.guards（`_guard_line`，总review F7，与
    `quota.check_guards` 口径统一）；三条线各自独立，两处都没配的那一条
    只是不判它，不拖累其余两条（不再"session_max 或 weekly_max 有一个
    没配就整段不判定"）。
    """
    session_max = _guard_line(task, config, "session_pct_max")
    weekly_max = _guard_line(task, config, "weekly_pct_max")
    model_max = _guard_line(task, config, "model_weekly_pct_max", weekly_max)  # 单模型周线单独一个数，没配就跟全模型线
    if session_max is None and weekly_max is None and model_max is None:
        return None, "", {}  # 三条线都没配 = 这个任务完全不受额度守卫管
    usage = _read_fresh_usage(config)
    if usage is None:
        return None, "", {}
    session_pct = usage.get("session_pct")
    week_all_pct = usage.get("week_all_pct")
    claude_rc = store.runner_config(config).get("claude") or {}
    label = claude_rc.get("models", {}).get(store.effective_model(task), {}).get("usage_label")
    per_model = usage.get("per_model") or {}
    model_pct = per_model.get(label) if label else None

    week_hit = weekly_max is not None and isinstance(week_all_pct, int) and week_all_pct >= weekly_max
    model_hit = model_max is not None and isinstance(model_pct, int) and model_pct >= model_max
    if week_hit or model_hit:
        model_note = f"，{label} 单独周线剩 {100 - model_pct}%（线 {100 - model_max}%）" if model_hit else ""
        # weekly_max 可能是 None（这条线本身没配、纯靠 model_hit 触发）——
        # week_line_left 跟 week_left 一样用 "?" 占位，不能直接 100 - None。
        week_line_left = "?" if weekly_max is None else 100 - weekly_max
        if store.role_of(task) == "review":
            # S7：审稿班收尾话术不一样——写进最终回复正文、末行 NEXT: pending，
            # 不叫它写 handover_path/commit（那是 build 角色的收尾协议）。
            text = store.render(
                config.get("review_wrapup_text") or DEFAULT_REVIEW_WRAPUP_TEXT,
                week_left=("?" if week_all_pct is None else 100 - week_all_pct),
                week_line_left=week_line_left,
                model_line_left=100 - model_max,
                model_note=model_note,
            )
        else:
            text = store.render(
                config.get("quota_wrapup_text") or QUOTA_WRAPUP_TEXT,
                week_left=("?" if week_all_pct is None else 100 - week_all_pct),
                week_line_left=week_line_left,
                model_line_left=100 - model_max,
                model_note=model_note,
                handover_path=str(handover_path(task)),
                commit_step=commit_step(task),
            )
        return "wrapup", text, {}

    if session_max is not None and isinstance(session_pct, int) and session_pct >= session_max:
        from .quota import resets_in_minutes

        resets_in = resets_in_minutes(usage.get("session_resets"))
        if resets_in == 0:
            # F6：resets_in==0 意味着缓存里这份 usage 自己说的刷新时刻已经
            # 过去了——这份数据早于本轮刷新（多半是闹钟刚醒、这次工具调用
            # 触发的刷新还没把 quota.json 换成新数据），不能拿着它再判一次
            # "到线"，那只会让模型白等一轮。等下一次真正刷新（resets_in
            # is None 时不知道新旧，按原行为放行判定，不在这里拦）。
            store.append_event(
                task["id"], "缓存额度早于刷新时刻，跳过五小时暂停判定，等下一次刷新"
            )
            return None, "", {}
        if store.role_of(task) == "review":
            # S7.1 阻断二：review 不走 build 那套 ScheduleWakeup 多轮自我唤醒
            # 闹钟——那套闹钟醒来的中间回复不带 NEXT，会被 review 的 Stop
            # 协议解析成协议缺失、保守转成 fix（假退回）。改成当场一次性
            # NEXT: pending，之后由调度器按额度刷新时间主动敲它继续。
            total = (resets_in if resets_in is not None else ALARM_FALLBACK_COUNT * ALARM_UNIT_MINUTES)
            text = store.render(
                config.get("review_quota_pause_text") or DEFAULT_REVIEW_QUOTA_PAUSE_TEXT,
                session_left=100 - session_pct,
                session_line_left=100 - session_max,
                resets_in=("未知" if resets_in is None else resets_in),
            )
        else:
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


def _subagent_quota_notice(
    task: dict, status: dict, payload: dict, config: dict
) -> str | None:
    """G19：这次 PostToolUse 落在子 agent 里（`_is_subagent_call`）时，除了
    照旧记"欠账"（context_refresh_pending，留给主会话下一次工具调用补刷
    补注），额外只做一次针对额度的判定——命中就直接把提醒打进这次子
    agent 自己的工具结果，不用等主会话。

    跟主会话的 `_quota_check` 走同一套线（wrapup/pause 判定不变），但：
    - 文案是给子 agent 看的独立短文案（`SUBAGENT_QUOTA_NOTICE_TEXT`），
      不是主会话那套"设 ScheduleWakeup 闹钟"/"写交接文件"的话术——子
      agent 做不了这些事，只能收尾停下；
    - 不写 quota_paused_until / quota_warned_at 等主会话字段，那些仍由
      主会话下一次工具调用经 `_post_tool_use_refresh` 补落盘；
    - 只记一条 events，不计入 quota_pause_count/quota_warn_count。

    同一个 agent 不重复注：去重键优先用 `payload.agent_id`，没有就退到
    `tool_use_id` 前缀（都没有就没法去重，直接不注——宁可漏提醒，不能
    每次工具调用都注一遍刷屏）。
    """
    agent_key = payload.get("agent_id") or str(payload.get("tool_use_id") or "")[:16]
    if not agent_key:
        return None
    noted = list(status.get("subagent_quota_noted") or [])
    if agent_key in noted:
        return None
    kind, _text, _fields = _quota_check(task, config)
    if kind not in ("wrapup", "pause"):
        return None
    usage = _read_fresh_usage(config) or {}
    session_pct = usage.get("session_pct")
    week_pct = usage.get("week_all_pct")
    text = store.render(
        config.get("subagent_quota_notice_text") or SUBAGENT_QUOTA_NOTICE_TEXT,
        session_left=("?" if not isinstance(session_pct, int) else 100 - session_pct),
        week_left=("?" if not isinstance(week_pct, int) else 100 - week_pct),
    )
    noted.append(agent_key)
    task_id = task["id"]
    store.update_status(task_id, subagent_quota_noted=noted)
    store.append_event(task_id, f"额度提醒已注入子 agent（{agent_key}）")
    return text


def _drop_expired_quota_pause(task: dict, status: dict, now_iso: str) -> bool:
    """总review F2：闹钟响完模型自己接着干、干完收工之后，`quota_paused_until`
    若还留在 status 里，调度器下一 tick 会把它当"还没恢复"补敲一句"额度应
    已刷新，请继续"——模型其实早就自己继续/收工了，多烧一轮、还把换班判定
    推迟一轮（9/1 真机 `ce5f` 任务实录）。

    只对 Claude build 角色清：
    - Codex 没有 ScheduleWakeup，闹钟到点必须由调度器主动 send-keys 叫醒
      （`_check_running` 的五小时暂停分支/F3），这里不能替它清掉，否则
      调度器再也不会去敲它；
    - review 角色的 hold/resume 协议依赖 `quota_paused_until`
      （`scheduler._check_running` 的 review-hold 恢复分支、
      `_review_hold_resume_eta`），提前清掉会让恢复分支永远等不到。

    调用方必须在 `store.modify_status` 的锁内闭包里直接对 status 原地操作
    （不经过 `update_status`），返回 True 时由调用方记一笔 events。
    """
    if store.effective_runner(task) != "claude" or store.role_of(task) == "review":
        return False
    paused_until = status.get("quota_paused_until")
    if not paused_until:
        return False
    try:
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        paused_dt = datetime.fromisoformat(paused_until.replace("Z", "+00:00"))
    except ValueError:
        return False
    if now < paused_dt:
        return False
    status.pop("quota_paused_until", None)
    status.pop("quota_resume_sent", None)
    return True


def _refresh_context(task: dict, payload: dict, fields: dict) -> None:
    """读 Claude transcript 刷新 context_tokens / context_pct（读不到就置 None）。

    算上限失败（config 缺/坏）只记 events.log，不拖垮整个事件：
    context_tokens 照常写，context_pct 留空。

    只服务 Claude——Codex 走配对的 `_refresh_context_codex`（总review三 H1/
    H2：Codex 的水位来自 rollout 自己带的 token_count 记录，跟 Claude
    transcript 的 assistant usage 记录是两套完全不同的格式，`config` 里也
    没有稳定的模型表可查，`_context_limit` 对它恒定查不到东西）。
    """
    fields["context_tokens"] = None
    fields["context_pct"] = None
    # S7.1 阻断四 Part A：这一步实际只在 effective_runner=="claude" 的班上被
    # 调用（PostToolUse 的 codex 分支在 handle_event 里已经按 effective_runner
    # 提前 return，不会走到这里），但仍要按这一班自己的有效工人判断，不能看
    # 顶层 task["runner"]——否则 Codex 施工 + Claude 审稿这类跨家组合下，
    # review 这一班会被顶层的 build runner 误判成 codex，白白跳过上下文刷新。
    if store.effective_runner(task) == "codex":
        return
    transcript_path = payload.get("transcript_path")
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


def _refresh_context_codex(task: dict, payload: dict, fields: dict) -> None:
    """读 Codex rollout 刷新 context_tokens / context_limit / context_pct
    （读不到就置 None）。跟 `_refresh_context`（Claude）配对，上限来源不同：

    Codex 在 config 里的上下文上限恒为 null（S6.1 B3 的设计决定：
    `context_limit_for` 对非 claude runner 如实返回 None，不编数字），只能
    从 rollout 自己带的 `model_context_window` 现读；任务
    `guards.context_limit_tokens` 仍然优先（跟 `_context_limit` 同一个优先
    级口径），因此测得的上限落进新字段 `status.context_limit`，不写回
    config（那是 Claude 的字段来源）。
    """
    fields["context_tokens"] = None
    fields["context_limit"] = None
    fields["context_pct"] = None
    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return
    found = read_codex_context(transcript_path)
    if found is None:
        return
    tokens, window = found
    fields["context_tokens"] = tokens
    guard_limit = (task.get("guards") or {}).get("context_limit_tokens")
    limit = int(guard_limit) if guard_limit else window
    if not limit:
        return
    fields["context_limit"] = limit
    fields["context_pct"] = round(100 * tokens / limit)


# S8 审查 B：上下文水位不再只按"固定每 20 次工具调用"刷。审稿班一次 Read 吞
# 两三万 token，20 次之间上下文能从警戒线下直接越过硬上限（9/1 实测第 20 次
# 67k、第 30 次真值 249k），提醒来得再晚也没用。增量触发：transcript 自上次
# 刷新后长了超过"上限的 5%（按 4 字节/token 折算，实测中文为主的 transcript
# 约 5.3 字节/token，折算偏保守）"就提前刷一次；最少 32 KB。每 20 次照旧。
_REFRESH_EVERY_CALLS = 20
_REFRESH_GROWTH_RATIO = 0.05
_REFRESH_GROWTH_BYTES_PER_TOKEN = 4
_REFRESH_GROWTH_MIN_BYTES = 32 * 1024
# 判断"这次工具调用是不是主会话自己的"时只看主 transcript 尾部这么多字节
_MAIN_TRANSCRIPT_TAIL_BYTES = 512 * 1024
# 总review F5：每次工具调用跑几分钟测试的班，"每 20 次"之间可能一小时都
# 不查一次额度/水位（B 组报告 B-2：注入不够及时）。距上次刷新决定成立
# 超过这么多秒也提前刷一次，跟"每 20 次"/"transcript 增量"两个条件并列。
_REFRESH_MAX_INTERVAL_SECONDS = 300


def _transcript_size(payload: dict) -> int | None:
    """payload 里 transcript 的当前字节数；没有/读不到 → None（增量触发退化成每 20 次）。"""
    path = payload.get("transcript_path")
    if not path:
        return None
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _refresh_growth_bytes(task: dict) -> int:
    """transcript 长多少字节就该提前刷一次水位。config/上限读不到就退回 32 KB 的
    保守值——宁可多刷几次（一次刷新只是读 transcript 尾部 512 KB）。"""
    try:
        limit = _context_limit(task, store.load_config())
    except Exception:
        limit = None
    if not limit:
        return _REFRESH_GROWTH_MIN_BYTES
    return max(
        _REFRESH_GROWTH_MIN_BYTES,
        int(limit * _REFRESH_GROWTH_RATIO * _REFRESH_GROWTH_BYTES_PER_TOKEN),
    )


def _decide_refresh(
    status: dict, now: str, calls: int, size: int | None, growth_limit: int
) -> tuple[bool, bool]:
    """这次 PostToolUse 该不该刷新水位——Claude、Codex 两条 PostToolUse 分支
    共用同一套节奏判定（总review三 H2：Codex 以前这里只计数，现在补齐跟
    Claude 一样的刷新节奏，不许各写一份）：每 `_REFRESH_EVERY_CALLS` 次一刷；
    或上次该刷却欠着的补刷（`was_pending`——Codex 不做子 agent 判定，这个
    分支恒为 False，见 `handle_event` 里 Codex PostToolUse 分支的说明）；
    或距上次刷新已经过了 `_REFRESH_MAX_INTERVAL_SECONDS`；或 transcript
    长得太快提前触发（`size`/`growth_limit`，S8 审查 B）。

    副作用：原地更新 `status` 的 `context_refresh_pending` /
    `context_refresh_size` / `context_refreshed_at` 三个字段——调用方必须
    在 `store.modify_status` 的锁内闭包里调用。返回 `(refresh, was_pending)`。
    """
    was_pending = bool(status.get("context_refresh_pending"))
    refreshed_at = status.get("context_refreshed_at")
    time_trigger = False
    if refreshed_at:
        try:
            elapsed = (
                datetime.fromisoformat(now.replace("Z", "+00:00"))
                - datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
            ).total_seconds()
            time_trigger = elapsed >= _REFRESH_MAX_INTERVAL_SECONDS
        except ValueError:
            time_trigger = False
    refresh = False
    if calls % _REFRESH_EVERY_CALLS == 0 or was_pending or time_trigger:
        refresh = True  # 每 20 次一刷；或欠着的补刷；或距上次刷新太久了
    elif size is not None:
        base = status.get("context_refresh_size")
        if base is None or size < base:
            # 没基线（或换了文件/被截断）：只登记基线，不刷
            status["context_refresh_size"] = size
        elif size - base >= growth_limit:
            refresh = True  # transcript 长得太快，提前刷（见 _REFRESH_GROWTH_*）
    if refresh:
        status.pop("context_refresh_pending", None)
        if size is not None:
            status["context_refresh_size"] = size
        status["context_refreshed_at"] = now
    elif refreshed_at is None:
        # F5：从没记过刷新时刻（老状态/这一班第一次调用）——先登记
        # 一个起点，不强行触发刷新（不然每个任务第一次调用就必刷）。
        status["context_refreshed_at"] = now
    return refresh, was_pending


def _is_subagent_call(payload: dict) -> bool:
    """这次 PostToolUse 是不是子 agent 的工具调用。

    CC 对子 agent 的工具调用同样触发本 hook（tool_calls 计数里混着它们，
    8/28 一个任务 1002 次里 880 次是子 agent 的），payload.transcript_path 仍是
    主会话的（9/1 本机实证：子 agent 体内那次刷新写下的水位是主会话的值），
    但 stdout 回注只会进**子 agent**的工具结果，主会话根本看不见——9/1 本机
    真机：五小时额度暂停提醒 #1/#2 全进了一个一次性 haiku 探针，主会话继续
    烧额度，status 却已记 quota_paused_until，调度器按"它已停下"处理。
    上下文提醒同理：子 agent 消费掉提醒、context_warned_at 落盘，主会话没写
    交接就收工，调度器按"提醒过没交接"走 on_no_handover。

    判据：payload.tool_use_id 在主 transcript 尾部找不到。主会话自己的调用，
    assistant 那条 tool_use 记录在工具执行前就已落盘；子 agent 的记录在它
    自己的 agent-<id>.jsonl 里。payload 没有 tool_use_id（老版本 CC）或
    transcript 读不了 → 按主会话算（回到原有行为，不会把提醒吞掉）。
    """
    tool_use_id = payload.get("tool_use_id")
    path = payload.get("transcript_path")
    if not tool_use_id or not path:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _MAIN_TRANSCRIPT_TAIL_BYTES))
            tail = f.read()
    except OSError:
        return False
    return str(tool_use_id).encode("utf-8") not in tail


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
                    is_review = store.role_of(task) == "review"
                    guard_text = (task.get("guards") or {}).get("context_warn_text")
                    if is_review:
                        # S7.1 阻断二：review 撞上下文线不能沿用 build 的
                        # handover/NEXT:continue-done 协议（协议缺失会被
                        # 判协议缺失、保守转成 fix）——总有一份内置默认
                        # 兜底，不依赖运维记得单独配 review_context_warn_text。
                        text = (
                            guard_text
                            or config.get("review_context_warn_text")
                            or DEFAULT_REVIEW_CONTEXT_WARN_TEXT
                        )
                    else:
                        text = guard_text or config.get("context_warn_text")
                    if text:
                        ctx = store.render(
                            text,
                            ctx_k=round(tokens / 1000),
                            limit_k=round(limit / 1000),
                            handover_path=str(handover_path(task)),
                            commit_step=commit_step(task),
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


def _post_tool_use_refresh_codex(task: dict, status: dict, payload: dict) -> None:
    """Codex 版本的刷新 + 到线判定：只刷水位、判到线，不回注——Codex 没有
    stdout 回注这条路（模块开头说明），到线只落 `context_warn_pending`，
    真正 send-keys 投递交给 `scheduler._check_codex_context_warn`（总review
    三 H3）。跟 `_post_tool_use_refresh`（Claude）配对，但不做额度判定
    ——Codex 的额度到线走 `scheduler._check_codex_quota_pause` 那条独立的
    路（S6③已有），这里不重复。
    """
    task_id = task["id"]
    now = store.utc_now_iso()
    fields: dict = {}
    _refresh_context_codex(task, payload, fields)
    store.update_status(task_id, **fields)
    store.append_event(
        task_id, f"hook PostToolUse(codex) #{status['tool_calls']} 刷新上下文"
    )

    tokens = fields.get("context_tokens")
    limit = fields.get("context_limit")
    if tokens is None or not limit:
        return
    if status.get("context_warned_at") or status.get("context_warn_pending"):
        return  # 已经提醒过、或已经在等调度器投递，不重复判定/不刷屏 events.log
    try:
        threshold = warn_threshold(task, store.load_config(), limit=limit)
    except Exception as exc:
        store.append_event(task_id, f"算不出警戒线：{exc!r}")
        return
    if tokens >= threshold:
        store.update_status(task_id, context_warn_pending=True)
        store.append_event(
            task_id,
            f"上下文到线，待调度器敲收尾提醒（{tokens} tokens / 上限 {limit}）",
        )


# ---------- S7：审稿意见文件协议（只读班如何交付文件） ----------

_RE_REVIEW_NEXT = re.compile(r"^NEXT:\s*(done|fix|pending)\s*$")

# S7.3 阻断一：review file claim 的过期阈值。真实 hook 进程是 ~100ms 量级完成
# （见模块顶部说明），30 秒足够跟"另一个 Stop 还没写完文件"的正常并发区分开，
# 又不会让真崩溃卡太久没人接手。
_REVIEW_CLAIM_STALE_SECONDS = 30


def review_file_path(task: dict) -> Path:
    """审稿班这一轮的意见文件路径：task_dir(审稿 task id)/review-<round>.md
    ——由 Stop hook 在沙箱外原子落盘，reviewer 自己不调用 Write、不用 shell
    重定向，不写 NIGHTSHIFT_HOME。"""
    return store.task_dir(task["id"]) / f"review-{store.round_of(task)}.md"


def _review_file_staging_path(task: dict, token: str) -> Path:
    """某个 claim（按 token 区分）私有的暂存文件路径——S7.4 阻断一：不同
    claim 绝不共用同一个可写路径，写文件这一步不再直接碰 canonical 路径
    `review_file_path()`，只有锁内核验 token 通过才会把暂存文件原子替换
    成 canonical，避免被取代的旧 claim 在恢复后把 canonical 文件覆盖成
    自己的旧内容（S7.3 的 token 检查只保护了 status 提交，没保护文件本身）。
    """
    return store.task_dir(task["id"]) / f"review-{store.round_of(task)}.{token}.pending"


def _parse_review_verdict(text: str) -> tuple[str, bool]:
    """最终回复正文最后非空行严格三选一：done=通过，fix=退回，
    pending=意见未完（不计轮数）。没写 NEXT、空消息或未知值一律保守按
    fix——协议缺失时的安全默认，绝不能被当 done 放行。

    返回 (verdict, protocol_ok)；protocol_ok=False 时调用方要在 events.log
    记一笔"协议缺失，保守按 fix"。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "fix", False
    m = _RE_REVIEW_NEXT.match(lines[-1])
    if not m:
        return "fix", False
    return m.group(1), True


def _handle_review_stop(task: dict, payload: dict, now: str) -> None:
    """审稿班（role=review，Claude 或 Codex 皆同一套）的 Stop：把最终回复
    正文原子落成 review-<round>.md，解析末行 NEXT 记 review_verdict，state
    转 idle——具体下一步（起返工班/合并/告栏）由调度器 tick 时按
    role=review 分流决定，hook 本身只管落盘事实，不做流程判断。

    S7.1 阻断二：review 角色永远转 idle，不再像 build 那样在
    quota_paused_until 有值时转 waiting_wakeup——review 的"额度到线等
    刷新"统一记在 status.state=held（scheduler._review_pending 的 hold
    分支），waiting_wakeup 是 build 专属的"自己设了缓存闹钟"语义；这两套
    混用会让 review 的 verdict 落盘之后卡在 waiting_wakeup，
    _check_review_idle 只在 state=="idle" 时才会被调度器分流到，
    卡在 waiting_wakeup 的 verdict 会被无限期晾在那儿没人处理。

    并发安全 + 幂等（S7.1 阻断二）：
    - "这一轮是否已经接受过不可覆盖的 verdict"判断挪进 modify_status 的
      锁内闭包一起做，不再锁外 read_status 判重复——两个并发 Stop 不能
      都判定"我可以写"。
    - 只有 done/fix 是不可覆盖的终态（review_verdict_final=True）；
      pending 只是"这一轮还没完"，同一轮后续真正的 done/fix 仍然要能
      正常记录，不能被 pending 自己设的 review_recorded_round 挡住。
      （没有 review_verdict_final 字段的旧数据按"verdict 不是 pending
      就当已确认"向下兼容。）
    - 控制 turn 隔离：`review_awaiting_verdict` 为 False 时，说明这次
      Stop 是保活/我来看之类"不要求正式 verdict"的回复，只清运行期字段
      （stuck/auto_interrupted/last_event_at），**不碰 review_verdict/
      review_recorded_round**。

      S7.2 阻断五.1：state 不再一律"保持原样不动"——按 `review_control_kind`
      分流：`"hold"` 一律转 `held`（不管发送前是 working/idle/held 哪一种，
      "我来看"打进去、这一班真的停下来之后，账面结果都该是"停在这里等
      工头"，不能停留在 working 误导调度器继续按活跃任务处理）；
      `"keepalive"` 或缺省/未知 kind 保持原样不动（keepalive 本来就只会
      戳 held/waiting_background 两种状态，收到控制回复不该变；缺省是
      向后兼容旧数据的防御性默认）。

    总review二 G9：审稿班若自己用 Bash 起了个后台长命令并结束这一回合，
    这次 Stop 的正文里不会有 NEXT——不是"协议缺失"，是活还没干完，不该按
    fix 假退回。控制 turn 判断（上面这条）优先级排在这条检查**之前**：
    "我来看"/继续这类控制消息即使背景任务还在跑也该按控制语义处理（比如
    "我来看"要停到 held，不能被这里截胡成 waiting_background）；只有真正
    在等 verdict 的普通 Stop（`review_awaiting_verdict` 为 True）才检查
    背景任务，命中就只落 `state=waiting_background`、`last_event_at`，
    不进 claim/写文件/解析 verdict 那一整套——后台跑完 CC 会重新拉起这个
    会话，真正的结论在下一次 Stop。

    S7.2 阻断四：verdict 不再先于 review 文件落盘。旧写法在同一次
    `modify_status` 里既判断"是否放行"又直接把 verdict/final 钉死，锁外
    才写文件——文件写失败时 verdict 已经不可逆，之后同一轮任何 Stop 都会
    被当 duplicate 挡住，永远没机会补文件。改成三段：锁内 claim（只标记
    "这一轮我在处理"，不写 verdict）→ 锁外写文件 → 写成功后再锁内一次性
    commit verdict/file/final。文件写失败时把 claim 撤回，status 净效果
    等于没变，同一轮后续 Stop（含 CC 的静默重试）可以重新 claim、重新
    尝试。两个并发 Stop 的安全性没有丢：claim 阶段已经把"谁在处理这一轮"
    钉住，第二个并发 Stop 在 claim 阶段就会被判 duplicate，不会跑到写
    文件那步跟第一个撞车。
    """
    task_id = task["id"]
    round_ = store.round_of(task)
    text = payload.get("last_assistant_message") or ""

    claim: dict = {}

    def do_claim(status: dict) -> None:
        # S7.3 阻断二：任何一次 Stop 都算"消费掉了"上一个还没收尾的
        # send_review_control 投递意图——不管这次 Stop 最终判成 control 还是
        # claimed，都不该让发送方后续的 finalize 步骤覆盖这次已经做出的
        # 判断（见 scheduler.send_review_control 的 delivery_id 核验）。
        status.pop("review_control_delivery", None)
        if not status.get("review_awaiting_verdict", True):
            claim["kind"] = "control"
            control_kind = status.get("review_control_kind")
            claim["control_kind"] = control_kind
            status["last_event_at"] = now
            status["stuck"] = False
            status.pop("auto_interrupted", None)
            status.pop("stuck_since", None)
            status.pop("review_control_kind", None)
            if control_kind == "hold":
                status["state"] = "held"
                status["held_since"] = now
                status["held_reason"] = "我来看：工头要来看，已停在这里"
            return
        # 总review二 G9：不是控制 turn（真的在等 verdict），但这次 Stop
        # 背后还有后台任务在跑——不进下面的 claim/写文件/解析 verdict，
        # 免得没有 NEXT 被当"协议缺失"误判成 fix。
        background_tasks = payload.get("background_tasks") or []
        if any(t.get("status") == "running" for t in background_tasks):
            claim["kind"] = "background"
            status["state"] = "waiting_background"
            status["last_event_at"] = now
            return
        verdict_final = status.get("review_verdict_final")
        if verdict_final is None:  # 旧数据兼容：没这个字段就按 verdict 本身推断
            verdict_final = status.get("review_verdict") not in (None, "pending")
        if status.get("review_recorded_round") == round_ and verdict_final:
            claim["kind"] = "duplicate"
            return
        # S7.3 阻断一：claim 从裸整数换成带 token/claimed_at 的字典——裸整数
        # 一旦落盘、hook 进程在写文件之前就崩溃，永远不会被清掉，后续所有
        # Stop 都会被当"正在处理中"挡住，谁都不会真正写文件/落 verdict。
        # 加一个过期判断：claim 超过 _REVIEW_CLAIM_STALE_SECONDS 还没收尾，
        # 视为上一个 claim 者已经崩溃/消失，允许这次 Stop 重新 claim、
        # 重新走一遍完整流程（不尝试从半途恢复——这次 Stop 自己的 payload
        # 就是完整正文，直接重做一遍比"猜上一次写到哪了"更可靠）。
        existing = status.get("review_file_claim") or {}
        if existing.get("round") == round_:
            claimed_at = existing.get("claimed_at")
            stale = True
            if claimed_at:
                try:
                    age = datetime.fromisoformat(now) - datetime.fromisoformat(claimed_at)
                    stale = age >= timedelta(seconds=_REVIEW_CLAIM_STALE_SECONDS)
                except ValueError:
                    stale = True
            if not stale:
                # 已经有别的 Stop 在写这一轮的文件、还没提交完，且还没过期
                # ——当重复处理，不并发写两份内容可能不同的文件
                # （atomic_write_text 的 nonce 只保证不撞文件名，不保证两份
                # 内容谁该赢）。
                claim["kind"] = "duplicate"
                return
        claim["kind"] = "claimed"
        claim["token"] = uuid.uuid4().hex
        status["review_file_claim"] = {
            "round": round_, "token": claim["token"], "claimed_at": now,
        }

    store.modify_status(task_id, do_claim)

    if claim["kind"] == "control":
        note = f"（{claim['control_kind']}）" if claim.get("control_kind") else ""
        store.append_event(
            task_id, f"hook Stop(review) 控制 turn{note}，未解析 verdict"
        )
        return None
    if claim["kind"] == "duplicate":
        store.append_event(
            task_id, f"hook Stop(review) 第 {round_} 轮已记过/正在处理 verdict，忽略重复 Stop"
        )
        return None
    if claim["kind"] == "background":
        store.append_event(task_id, "审稿班后台任务仍在跑，这次 Stop 不当结论")
        return None

    # 走到这里说明上面锁内 claim 已经放行了这一次（并发的第二个 Stop 在
    # claim 阶段就已经被挡成 duplicate/control，不会跑到这里跟这次撞车）。
    verdict, protocol_ok = _parse_review_verdict(text)
    # S7.4 阻断一：写文件这一步不再直接碰 canonical 路径——每个 claim 写
    # 自己私有的暂存文件，只有锁内核验 token 通过才把它原子替换成
    # canonical，跟 status 提交在同一把锁里。S7.3 的 token 检查只在提交
    # status 这一步核验，写文件那一步任何 claim 者（不管是不是当前有效
    # 的那个）都直接 os.replace 同一个 canonical 路径——被取代的旧 claim
    # 恢复后仍然会把 canonical 文件覆盖成自己的旧内容，造成 status 说
    # fix、文件正文却是 done 这类互相矛盾的结果。
    staging_path = _review_file_staging_path(task, claim["token"])
    try:
        store.atomic_write_text(
            staging_path, text if text.strip() else "（空消息，无审稿正文）\n"
        )
    except OSError as exc:
        # 文件没写成：把 claim 撤回——只清自己的 token，如果 claim 已经被
        # 别人接管（token 不一样了），不能把接管者的 claim 一并删掉（这是
        # S7.3 遗留的一个真实 bug：旧 except 分支不核验 token 就直接 pop）。
        def rollback_claim(status: dict) -> None:
            current = status.get("review_file_claim") or {}
            if current.get("token") == claim["token"]:
                status.pop("review_file_claim", None)
        store.modify_status(task_id, rollback_claim)
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass
        store.append_event(
            task_id, f"hook Stop(review) 写审稿文件失败：{exc!r}，本轮可重试"
        )
        return None

    path = review_file_path(task)
    commit_result: dict = {}

    def do_commit(status: dict) -> None:
        # S7.3 阻断一：提交前核验这个 claim 还是不是"我们的"——极端情况下
        # 这次写文件写了很久，同时另一个更晚到达的 Stop 因为看到 claim 过期
        # 又重新 claim 了一次，两边不能都以为自己是当前处理者。token 不匹配
        # 说明已经被取代，这次写文件白做了，不提交（不算错误，只是这次
        # Stop 的结果被让位给了后来者）。
        #
        # S7.4 阻断一：token 不匹配时绝不 os.replace canonical 文件——被
        # 取代的 claim 连替换都不做，canonical 文件永远只能来自当前仍然
        # 有效的那个 claim。os.replace 与 status 提交必须在同一把锁内一起
        # 做，不给"文件已经换了、status 还没提交"或反过来的窗口。
        current_claim = status.get("review_file_claim") or {}
        if current_claim.get("token") != claim.get("token"):
            commit_result["superseded"] = True
            return
        try:
            os.replace(staging_path, path)
        except OSError as exc:
            # S7.4 阻断一：替换 canonical 这一步本身也可能失败（磁盘问题、
            # 目标路径类型不对等）——不能让异常带着"claim 还留着"一起
            # 逃出去，那样会重现阻断一原本要修的那个洞（claim 永久卡住，
            # 后续 Stop 全部被判 duplicate）。按"写文件失败"同一个口径处理：
            # 清掉 claim，允许下一次 Stop 重新 claim、重新尝试。
            commit_result["replace_failed"] = exc
            status.pop("review_file_claim", None)
            return
        status["review_verdict"] = verdict
        status["review_file"] = str(path)
        status["review_recorded_round"] = round_
        status["review_verdict_final"] = verdict != "pending"
        status["state"] = "idle"
        status["last_message"] = text[:2000]
        status["last_event_at"] = now
        status.pop("review_file_claim", None)
        status.pop("auto_interrupted", None)
        status.pop("stuck_since", None)

    updated = store.modify_status(task_id, do_commit)
    if commit_result.get("superseded"):
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass
        store.append_event(
            task_id,
            f"hook Stop(review) 第 {round_} 轮 claim 已被更新的一次取代，未提交"
            "（暂存文件已清理，canonical 文件未被碰过）",
        )
        return None
    if commit_result.get("replace_failed"):
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass
        store.append_event(
            task_id,
            f"hook Stop(review) 写审稿文件失败：{commit_result['replace_failed']!r}，本轮可重试",
        )
        return None
    note = "" if protocol_ok else "（协议缺失：没写合法 NEXT，保守按 fix）"
    store.append_event(
        task_id,
        f"hook Stop(review) 第 {round_} 轮 → verdict={verdict}{note}，"
        f"state={updated['state']}",
    )
    return None


def handle_event(task_id: str, event: str, payload: dict) -> str | None:
    """按事件类型更新 status.json / events.log，返回要回注的文案（通常 None）。

    turns / tool_calls / subagents_running 这类计数增量必须整个在
    modify_status 的锁内读改写——两个 hook 进程并行时会互相吃掉增量。
    """
    task = store.load_task(task_id)
    now = store.utc_now_iso()
    # S7：审稿角色可能跟顶层 build runner 不同（task.review.runner），事件
    # 分派一律按这一班自己的有效工人，不能只看 task["runner"]。
    runner = store.effective_runner(task)

    if event == "SessionStart":
        # Claude 不挂这个事件（launcher 起跑前已用 --session-id 预先分配好，
        # 提前落过 status），只有 Codex 靠它才第一次知道自己的 session/thread id。
        if runner != "codex":
            store.append_event(task_id, "hook SessionStart（非 codex，忽略）")
            return None

        def set_codex_session(status: dict) -> None:
            status["thread_id"] = payload.get("session_id")
            status["session_id"] = payload.get("session_id")
            status["transcript_path"] = payload.get("transcript_path")
            status["quota_source"] = "codex"
            if payload.get("permission_mode"):
                status["permission_mode"] = payload["permission_mode"]
            status["last_event_at"] = now

        store.modify_status(task_id, set_codex_session)
        store.append_event(
            task_id, f"hook SessionStart(codex) → thread_id={payload.get('session_id')}"
        )
        return None

    if event == "UserPromptSubmit":

        def bump_turns(status: dict) -> None:
            # F1：调度器投递的控制文字（保活/我来看/停工）本身也会触发这个
            # hook——build_control_kind/review_control_kind 任一个还留着，
            # 说明这轮 UserPromptSubmit 是控制 turn 造成的，state 必须保持
            # 发送前的值（held/chained），改去向要交给对应的 Stop 分支判断，
            # 这里不能抢先覆盖成 working。
            if not status.get("build_control_kind") and not status.get("review_control_kind"):
                status["state"] = "working"
                # F2：不是控制 turn 说明模型自己真的又开了一轮——如果它是自己
                # 缓存闹钟醒来接着干，残留的 quota_paused_until 该在这里清掉。
                if _drop_expired_quota_pause(task, status, now):
                    store.append_event(
                        task_id, "额度刷新时间已过，会话已自行继续/收工，取消调度器补敲"
                    )
            status["turns"] = int(status.get("turns") or 0) + 1
            status["session_id"] = payload.get("session_id")
            status["transcript_path"] = payload.get("transcript_path")
            # 实测 --permission-mode auto 会被 CC 对某些模型静默回落成 default，
            # 回报里带着实际生效的模式，调度器靠它开提醒窗（R2）；没有就不覆盖旧值
            if payload.get("permission_mode"):
                status["permission_mode"] = payload["permission_mode"]
            # S4：来新事件就是缓过来了，疑似卡住解除。
            # S4.1：连本次卡住周期的 auto_interrupted / stuck_since 一起清——
            # 只清 stuck 的话，会话恢复后第二次卡住永远不会再自动 Esc
            status["stuck"] = False
            status.pop("auto_interrupted", None)
            status.pop("stuck_since", None)
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

    elif event == "Stop" and store.role_of(task) == "review":
        # S7：审稿角色不分 Claude/Codex，Stop 一律走审稿意见文件协议
        # （落 review-<round>.md + 记 review_verdict），不进 build 那两支。
        return _handle_review_stop(task, payload, now)

    elif (
        event == "Stop"
        and store.read_status(task_id).get("build_control_kind") == "hold"
    ):
        # S7.4 阻断三：working build 被"我来看"打断——这次 Stop 是控制回复
        # 造成的，不是正常收工，不能跑存档点/换班判定/审稿流程（那会把
        # 中途暂停误判成这一班已经收工，甚至提前起审稿）。不分 Claude/
        # Codex，两家共用这一支：直接转 held，等"继续"用带 resume 语气的
        # 文案真正让它接着干活（见 server._api_pipeline_continue）。
        def consume_build_hold(status: dict) -> None:
            status["state"] = "held"
            status["held_since"] = now
            status["held_reason"] = "我来看：工头要来看，已停在这里"
            status["last_event_at"] = now
            status["stuck"] = False
            status.pop("auto_interrupted", None)
            status.pop("stuck_since", None)
            status.pop("build_control_kind", None)

        store.modify_status(task_id, consume_build_hold)
        store.append_event(task_id, "hook Stop(build) 我来看：已停在这里等工头")
        return None

    elif event == "Stop" and (
        (_s := store.read_status(task_id)).get("build_control_kind") in ("keepalive", "stop")
        and _s.get("state") in ("held", "chained")
    ):
        # F1：build 角色跟 review 对称的控制 turn 分支——调度器投递的保活
        # 探针（build_control_kind="keepalive"）或审稿通过后敲的"请停下"
        # （build_control_kind="stop"）打进会话后，必然触发这一次 Stop；
        # 这不是正常收工，不能走存档点/换班判定/审稿流程——那会把"只是
        # 回了一句探针/确认"误判成这一班干完了，state 从 held/chained
        # 掉回 idle（Fable 审查 A1/N1，9/1：held 的 build 收到保活回一句就变 idle；
        # `_review_done` 敲"请停下"写完 chained 之后 build 回一句又变
        # idle）。state 原样保持 send 之前的值不动，只清运行期字段，把
        # build_control_kind 标记消费掉——不然会残留到下一次 Stop，让一次
        # 正常收工也被当成控制 turn 吞掉。
        kind = _s.get("build_control_kind")
        state = _s.get("state")

        def consume_build_control(status: dict) -> None:
            status.pop("build_control_kind", None)
            status["last_message"] = (payload.get("last_assistant_message") or "")[:2000]
            status["last_event_at"] = now
            status["stuck"] = False
            status.pop("auto_interrupted", None)
            status.pop("stuck_since", None)
            # state 不动：由发起这次控制 turn 的一方（保活探针的下一次
            # tick，或 _review_done 的 success_only_fields）决定去向。

        store.modify_status(task_id, consume_build_control)
        store.append_event(
            task_id, f"hook Stop(build) 控制 turn（{kind}），state 保持 {state}"
        )
        return None

    elif event == "Stop" and runner == "codex":
        # Codex 的 Stop payload 没有 background_tasks/session_crons（那是
        # Claude 概念）；后台完成登记（F12）是夜班自己的登记簿，S6④ 才落地，
        # 由 scheduler 在那之后核对登记簿，必要时把 idle 拦回
        # waiting_background——这里只按已知事实（额度缓存闹钟）落 idle/
        # waiting_wakeup，不猜后台状态。
        status_now = store.read_status(task_id)
        fields = {
            "last_message": (payload.get("last_assistant_message") or "")[:2000],
            "stuck": False,
            "last_event_at": now,
        }
        fields["state"] = "waiting_wakeup" if status_now.get("quota_paused_until") else "idle"
        # 总review三 H2：Codex 的 Stop 也刷新一次水位（跟 Claude 的 Stop 一样
        # 只刷不注——到线判定/send-keys 投递只在 PostToolUse 那条路做，见
        # `_post_tool_use_refresh_codex`）。
        _refresh_context_codex(task, payload, fields)

        def clear_stuck_cycle_codex(status: dict) -> None:
            status.update(fields)
            status.pop("auto_interrupted", None)
            status.pop("stuck_since", None)
            # F1：保活戳中的 build 走 waiting_background/waiting_wakeup 时
            # state 不是 held，会落到这里按 payload 重算——build_control_kind
            # 必须在这里消费掉，不能残留到下一次 Stop 被误判成控制 turn。
            status.pop("build_control_kind", None)

        store.modify_status(task_id, clear_stuck_cycle_codex)
        store.append_event(task_id, f"hook Stop(codex) → {fields['state']}")
        return None

    elif event == "Stop":
        background_tasks = payload.get("background_tasks") or []
        fields = {
            "background_tasks": background_tasks,
            "last_message": (payload.get("last_assistant_message") or "")[:2000],
            "stuck": False,  # S4：有事件就是缓过来了，疑似卡住解除
            "last_event_at": now,
        }
        _refresh_context(task, payload, fields)
        crons = payload.get("session_crons") or []
        fields["session_crons"] = crons
        if any(t.get("status") == "running" for t in background_tasks):
            fields["state"] = "waiting_background"
        elif crons:
            fields["state"] = "waiting_wakeup"  # 它自己定了闹钟（如额度暂停的缓存闹钟），不是干完了
        else:
            fields["state"] = "idle"

        def clear_stuck_cycle(status: dict) -> None:
            # S4.1：恢复要连本次卡住周期的 auto_interrupted / stuck_since
            # 一起清，不然会话缓过来后第二次卡住永远不会再自动 Esc
            status.update(fields)
            status.pop("auto_interrupted", None)
            status.pop("stuck_since", None)
            # F1：保活戳中的 build 走 waiting_background/waiting_wakeup 时
            # state 不是 held，会落到这里按 payload 重算——build_control_kind
            # 必须在这里消费掉，不能残留到下一次 Stop 被误判成控制 turn。
            status.pop("build_control_kind", None)
            # F2：这一支是"真正收工/自己设了新闹钟"的普通 Stop（不是控制
            # turn）——闹钟响完模型自己继续干完这一轮的场景也会落到这里，
            # 顺手把过期的 quota_paused_until 清掉。
            if _drop_expired_quota_pause(task, status, now):
                store.append_event(
                    task_id, "额度刷新时间已过，会话已自行继续/收工，取消调度器补敲"
                )

        store.modify_status(task_id, clear_stuck_cycle)
        store.append_event(task_id, f"hook Stop → {fields['state']}")
        return None

    elif event == "PostToolUse" and runner == "codex":
        # Codex 一律靠调度器 tmux send-keys 投递守卫文案（设计稿 §5.2），
        # 不依赖 hook stdout 回注——这里不查额度（额度到线走
        # `scheduler._check_codex_quota_pause` 那条独立的路，S6③已有）；
        # 上下文刷新节奏（每 20 次/增量触发/超时触发）总review三 H2 起改成
        # 跟 Claude 共用 `_decide_refresh`，到线只落 `context_warn_pending`
        # 交给调度器投递（`_post_tool_use_refresh_codex`）。
        #
        # 子 agent 判定（`_is_subagent_call`）认的是 Claude transcript 里
        # tool_use 记录的形状；Codex rollout 里有没有等价物、怎么定位不
        # 确定，这里先不做——Codex 一律按主会话处理（H2 原料 3）。
        size = _transcript_size(payload)
        growth_limit = _refresh_growth_bytes(task)
        refresh = False

        def bump_tool_calls_codex(status: dict) -> None:
            nonlocal refresh
            calls = int(status.get("tool_calls") or 0) + 1
            status["tool_calls"] = calls
            status["stuck"] = False
            status.pop("auto_interrupted", None)
            status.pop("stuck_since", None)
            status["last_event_at"] = now
            refresh, _was_pending = _decide_refresh(status, now, calls, size, growth_limit)

        status = store.modify_status(task_id, bump_tool_calls_codex)
        if refresh:
            _post_tool_use_refresh_codex(task, status, payload)
        return None

    elif event == "PostToolUse":
        refresh = False
        was_pending = False
        size = _transcript_size(payload)
        growth_limit = _refresh_growth_bytes(task)

        def bump_tool_calls(status: dict) -> None:
            nonlocal refresh, was_pending
            calls = int(status.get("tool_calls") or 0) + 1
            status["tool_calls"] = calls
            # S4：有事件就是缓过来了，疑似卡住解除；
            # S4.1：连本次卡住周期的 auto_interrupted / stuck_since 一起清
            status["stuck"] = False
            status.pop("auto_interrupted", None)
            status.pop("stuck_since", None)
            status["last_event_at"] = now
            refresh, was_pending = _decide_refresh(status, now, calls, size, growth_limit)

        status = store.modify_status(task_id, bump_tool_calls)
        if not refresh:
            return None
        if _is_subagent_call(payload):
            # 回注会进子 agent 的工具结果、主会话看不见（见 _is_subagent_call）：
            # 上下文这次不刷不注，记一笔"欠着"，主会话下一次工具调用立刻补刷。
            store.update_status(task_id, context_refresh_pending=True)
            if not was_pending:
                store.append_event(
                    task_id,
                    f"hook PostToolUse #{status['tool_calls']} 该刷新但落在子 agent 的"
                    "工具调用里，不回注；留待主会话下一次工具调用",
                )
            # G19：上下文提醒不给子 agent 是对的（量的是主会话的水位），但
            # 额度是两边共用的——主会话在前台等子 agent 时，欠账要等它下
            # 一次工具调用才补，这段时间子 agent 一直烧。额外做一次只针对
            # 额度的判定，命中就直接注进这次子 agent 的工具结果（见
            # _subagent_quota_notice），不等"欠账补注"那条慢路径。
            try:
                config = store.load_config()
            except Exception:
                return None
            return _subagent_quota_notice(task, status, payload, config)
        return _post_tool_use_refresh(task, status, payload)  # 锁内算出的新值；上下文字段不是计数，锁外合并即可

    elif event == "SessionEnd":
        # 已经收尾的终态（finished/chained/…）不被"关窗口"盖成 exited：
        # 网页上"已完成"不该因为用户关了窗就变"已退出"（8/27 真机发现）。
        # S5②：awaiting_merge/merged/discarded 同理——等合并/已合并的卡片
        # 不许被顺手关窗冲掉
        keep = (
            "finished", "chained", "chain_exhausted", "needs_attention",
            "awaiting_merge", "merged", "discarded",
        )

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
    """`python3 -m nightshift.hook <task_id> <event>`（Claude：per-task settings.json，
    task id 直接写进命令行）或 `python3 -m nightshift.hook --codex <event>`
    （Codex：固定的 nightowl profile，同一份 hooks.json 服务所有任务，
    task id 从 run.sh export 的 NIGHTOWL_TASK_ID 环境变量读——profile 内容
    不能随任务变，否则每个任务都要重新走一次 hook 信任）。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return 0
    if argv[0] == "--codex":
        if len(argv) < 2:
            return 0
        event = argv[1]
        task_id = os.environ.get("NIGHTOWL_TASK_ID") or ""
        if not task_id:
            return 0
    else:
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
