"""调度循环：到点预检起跑、崩溃恢复、保活戳、推迟/失败窗口、常驻主循环。

时间约定：一律 UTC；所有"现在"都从参数 now（aware，UTC）进来，便于测试注入。
ISO 字符串与 datetime 互转用 parse_iso / to_iso（与 store.utc_now_iso 同格式）。

预检顺序（设计稿 §5.1，按本仓库开工令定为：信任 → 同目录 → 额度 → 起跑）。
崩溃恢复四条（设计稿 §3）：PID 复用三条件、宽限期、先落盘再碰 tmux、重试上限。
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import background_runner, hook, launcher, quota, store, warmup, worktree

__all__ = ["parse_iso", "to_iso", "tick", "run_forever", "reconcile_worktrees"]

# F4：模块级 logger——tick 内按任务隔离异常时要记日志，不能等到
# _setup_logging（常驻主循环才调用一次）才有 logger 可用；_setup_logging
# 挂好 handler 之后返回的也是这同一个 logger（logging.getLogger 按名字
# 缓存单例），行为不变。
logger = logging.getLogger("nightshift.scheduler")

# retry_max 的兜底值（task.json 里没写时按 3）
DEFAULT_RETRY_MAX = 3
# 视为"活跃"的状态：额度刷新看它们，同目录不并跑也拦它们。S7：held 加入
# 活跃态（会话还活着，只是明确不施工）；worktree=true 的同项目并跑豁免
# （_try_launch 里已有）天然覆盖"同一流水线一个 held 一个 working"的例外，
# 不需要给 held 单独开一条豁免规则。
ACTIVE_STATES = ("launching", "working", "waiting_background", "waiting_wakeup", "held")
# config.scheduler 缺 keepalive_text 时的兜底文案（与 config.example.json 一致）
DEFAULT_KEEPALIVE_TEXT = (
    "来自nightshift：保活探针——背景任务还在跑吗？简短回一句即可。"
)
# config 缺 stuck_interrupt_text 时的兜底文案（与 config.example.json 一致）。
# Esc 中断不会触发 Stop hook（CC 官方文档明说"不会在用户中断时触发"），
# 光按 Esc 只是让它停在输入提示符，调度器收不到任何信号、auto_interrupted
# 也清不掉；必须紧接着敲一段文字进去起一个新轮次，靠这个新轮次自然结束
# 时触发的 Stop/UserPromptSubmit 才能让状态机缓过来。
DEFAULT_STUCK_INTERRUPT_TEXT = (
    "来自nightshift：你疑似卡在一条工具调用里已经 {stuck_minutes} 分钟没反应，"
    "刚按了 Esc 把这一轮打断。看看刚才在等的命令/进程是不是真卡死了——"
    "换个方式重试、跳过，或者判断已经没救了就当它失败处理，然后照常继续或收尾。"
)
# S6③：Codex 没有 ScheduleWakeup，五小时线到点/刷新都要调度器主动
# send-keys（config.codex_quota_pause_text / codex_resume_text 网页可改）
DEFAULT_CODEX_QUOTA_PAUSE_TEXT = (
    "来自nightshift：五小时额度只剩 {session_left}%（线 {session_line_left}%），"
    "约 {resets_at} 刷新。现在停下，不要再开新的工具调用，调度器到点会敲你继续。"
)
DEFAULT_CODEX_RESUME_TEXT = (
    "来自nightshift：五小时额度应已刷新，请从刚才停下的地方继续。"
)
# Claude 走缓存闹钟自己醒来的正常路径不需要这句（那是它自己接着干）；
# 只有 idle 分支（闹钟已经响完但没等到 UserPromptSubmit）与 F3 的
# 60 分钟宽限期兜底（闹钟大概率丢了）两处会真的 send-keys 这句。
DEFAULT_CLAUDE_RESUME_TEXT = (
    "来自nightshift：五小时额度应已刷新，请从刚才停下的地方继续。"
)
# F3：Claude 没有调度器能感知的"闹钟到底响没响"信号——ScheduleWakeup 若被
# CC 的 cron 丢了（没触发），waiting_wakeup 会永远等下去。给一段宽限期：
# 超过额度刷新时间这么多分钟仍没等到它自己醒（UserPromptSubmit/Stop 都没
# 来），调度器主动 send-keys 叫它继续，跟 idle 分支走同一套文案/失败处理。
CLAUDE_WAKEUP_GRACE_MINUTES = 60
# F11：build 的 idle 去抖。hook 的 Stop→idle 与紧接着（排队消息/后台通知
# 重新拉起会话）触发的 UserPromptSubmit→working 之间有个缝——tick 恰好
# 落在这个缝里会把还在干活的班误判成收工（9/1 真机 ce5f 任务：
# 20:33:05Z Stop→idle 与同一秒 UserPromptSubmit→working）。idle 落定不满
# 这么多秒就不评估，下一 tick 再看。
IDLE_SETTLE_SECONDS = 20
# S7.1 阻断二/三：review 角色因额度到线转 held（on_no_quota=hold）后的
# 恢复文案——明确要求"继续完成这一轮审稿"，结尾仍然只用 NEXT: done/fix/
# pending 三选一，不能沿用 build 那句"从刚才停下的地方继续"（不成协议）。
DEFAULT_REVIEW_RESUME_TEXT = (
    "来自nightshift：额度应已刷新，请继续完成这一轮审稿——"
    "结束时仍然只用 NEXT: done / NEXT: fix / NEXT: pending 三选一。"
)
# S7.3 阻断三："我来看"中途暂停一个还没给出 verdict 的 reviewer，"继续"要
# 真正让它接着干活——不是额度刷新，不能沿用 DEFAULT_REVIEW_RESUME_TEXT
# 那句"额度应已刷新"（说法不对）。
DEFAULT_REVIEW_HOLD_RESUME_TEXT = (
    "来自nightshift：工头看完了，继续这一轮审稿——"
    "结束时仍然只用 NEXT: done / NEXT: fix / NEXT: pending 三选一。"
)
# S7.4 阻断三：working build 被"我来看"中途打断（还没走到收工边界，没有
# 完成本轮交接），"继续"要真正让它接着干活——跟 review 那句一样不能提
# "额度"，也不能提 verdict（build 的协议是交接文件末行 NEXT: done/continue，
# 不是 done/fix/pending）。
DEFAULT_BUILD_HOLD_RESUME_TEXT = (
    "来自nightshift：工头看完了，继续刚才的工作——"
    "收工时照常写交接，末行 NEXT: done 或 NEXT: continue。"
)
# 交接文件末行的换班指令（设计稿 §4.4）
_RE_NEXT_CONTINUE = re.compile(r"^NEXT:\s*continue\s*$")
_RE_NEXT_DONE = re.compile(r"^NEXT:\s*done\s*$")


def parse_iso(s: str) -> datetime:
    """ISO 字符串 → aware UTC datetime；裸时间按 UTC 处理。"""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    """aware datetime → Z 结尾的秒级 UTC ISO（与 store.utc_now_iso 同格式）。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- tick：调度循环的单轮 ----------


def tick(config: dict, now: datetime) -> list[str]:
    """跑一遍所有任务，返回本轮做过的动作描述（给日志与测试用）。

    按 status.state 分支（设计稿 §3 状态机）：
    - scheduled：到点（max(run_at, retry_at)）→ 预检起跑；
    - postponed：next_attempt_at 到点 → 同上；
    - launching：exit_code 铁证、宽限期与 PID 复用几条崩溃恢复在这里落；
    - working/waiting_background/idle：exit_code 兜底退场；窗口没了标 exited；
      非 auto 权限模式开窗提醒；waiting_background 静默超时戳保活（idle 永远
      不戳，设计稿 §5.2）；idle 首次评估换班（设计稿 §4.4）；
    - exited：也评估一次换班（只认交接文件）；
    - 其余状态（chained/finished/…）不动。

    F4：单个任务的异常不影响其它任务——循环体整个包了一层
    try/except，一个任务处理时炸了只记日志、留一条 events，跳到下一个
    任务；不会让它后面排队的任务这一轮全部陪葬。
    """
    if now.tzinfo is None:
        raise ValueError("now 必须是带时区的 aware datetime（UTC）")
    now = now.astimezone(timezone.utc)
    store.ensure_dirs()
    actions: list[str] = []
    items = store.list_tasks()
    for item in items:
        task = item["task"]
        try:
            status = item["status"]
            state = status.get("state")
            after = (task.get("trigger") or {}).get("type") == "after"
            if state == "scheduled":
                if after:
                    # S4：等前置任务——"到点"的定义换成前置链状态，
                    # 不满足就什么都不做（不推迟、不开窗）
                    if not _after_ready(task, status, config, now):
                        continue
                    status = _note_trigger_met(task, status, now)
                    actions.extend(_try_launch(task, status, config, now))
                    continue
                # R4：到点锚用 max(run_at, retry_at)——有 retry_at（启动重试时刻）
                # 就用它，task.json 的 run_at 一个字不改
                retry_at = status.get("retry_at")
                due = parse_iso(task["run_at"])
                if retry_at:
                    due = max(due, parse_iso(retry_at))
                if due <= now:
                    actions.extend(_try_launch(task, status, config, now))
            elif state == "postponed":
                next_at = status.get("next_attempt_at")
                if next_at and parse_iso(next_at) <= now:
                    if after:
                        # S4：到点后照旧再判一次前置条件，不满足就原地等
                        if not _after_ready(task, status, config, now):
                            continue
                        status = _note_trigger_met(task, status, now)
                    actions.extend(_try_launch(task, status, config, now))
            elif state == "launching":
                actions.extend(_check_launching(task, status, config, now))
            elif state in ("working", "waiting_background", "waiting_wakeup", "idle", "held"):
                actions.extend(_check_running(task, status, config, now))
            elif state == "exited":
                # S3 换班：exited 也评估一次（写完交接后会话被关/崩了的情形）
                actions.extend(_check_exited_chain(task, status, config, now))
            # 其余状态（chained/finished/…）不动
        except Exception as exc:
            # F4：不按任务隔离的话，一个任务每 tick 都复现的异常（比如
            # scheduled 任务的 project 已经不在 config.projects）会让排在
            # 它后面的任务整晚起不来——这里兜住，留日志+events，跳过继续。
            logger.exception("tick：任务 %s 处理异常，本轮跳过", task["id"])
            try:
                store.append_event(
                    task["id"], f"tick 处理异常（本轮跳过，下轮重试）：{exc!r}"
                )
            except OSError:
                pass
            continue

    # 每轮末尾：只刷有活跃任务在等的那家 runner，且它自己的分片缺失/过期才刷
    # （零开销原则；两家各自独立，S6 前只有 claude，行为不变）。
    # S7.1 阻断四 Part A：必须走 effective_runner——review 角色可能跟顶层
    # build runner 不同（task.review.runner），只读 task["runner"] 在
    # Codex 施工/Claude 审稿这类跨家组合下会漏刷正在等的那一家。
    active_runners = {
        store.effective_runner(item["task"])
        for item in items
        if store.read_status(item["task"]["id"]).get("state") in ACTIVE_STATES
    }
    if active_runners:
        _maybe_refresh_quota(config, now, actions, runners=active_runners)
    # 预热五小时窗口（config.warmup，网页可改）：到点发一句话给 haiku，一天一次
    # ——这是 Claude 专属机制，跟 Codex 无关
    slots = warmup.due(config, now)
    for slot in slots:
        result = warmup.run_warmup(config, now, slot=slot)
        actions.append(
            f"预热窗口（{slot}）：" + ("成功" if result.get("ok") else f"失败 {result.get('error', '')[:80]}")
        )
    if slots:
        # 预热后额度窗口已开始，顺手刷一次 quota.json 让页面立刻看到新刷新时间
        _maybe_refresh_quota(config, now, actions, force=True, runners={"claude"})
    return actions


# ---------- 起跑前预检（设计稿 §5.1） ----------


def _after_ready(task: dict, status: dict, config: dict, now: datetime) -> bool:
    """after 触发的"到点"判定：前置链最新一班的状态是否满足 when。

    - when == "finished"：前置链最新一班必须正好 finished（整条链完工）；
    - when == "ended"：落在 store.ENDED_STATES 里就算；
    - 前置任务不存在（被删了）→ 标 needs_attention 并开提醒窗（attention_noted
      只开一次），返回 False；
    - 不满足时返回 False，调用方什么都不做（不推迟、不开窗）。
    """
    task_id = task["id"]
    trigger = task.get("trigger") or {}
    pre_id = str(trigger.get("task") or "")
    try:
        store.load_task(pre_id)
    except (OSError, ValueError):
        if not status.get("attention_noted"):
            store.update_status(
                task_id, state="needs_attention", attention_noted=True,
                last_event_at=to_iso(now),
            )
            store.append_event(
                task_id, f"前置任务 {pre_id} 不存在（被删了？）→ needs_attention"
            )
            launcher.open_notice_window(
                task, "(需要人工)",
                [f"前置任务 {pre_id} 不存在（被删了？），请改成按时间或换前置"],
                config,
            )
        return False
    state = store.chain_state(pre_id)
    when = trigger.get("when") or "finished"
    if when == "finished":
        # 老式任务以 finished 成功收口；工作树任务只有真正合入主线的 merged
        # 才算“整条链完工”。awaiting_merge 只对 when=ended 算一班已结束。
        return state in ("finished", "merged")
    return state in store.ENDED_STATES


def _note_trigger_met(task: dict, status: dict, now: datetime) -> dict:
    """首次满足前置条件时落 trigger_met_at（推迟窗口的起算锚），返回新 status。"""
    if status.get("trigger_met_at"):
        return status
    store.update_status(task["id"], trigger_met_at=to_iso(now))
    return store.read_status(task["id"])


def _fetch_and_record_usage(
    runner: str, config: dict, now: datetime
) -> tuple[dict | None, str]:
    """按 runner 查一次新鲜额度并落盘对应分片（quota.json 的 claude/codex
    各自一份，互不覆盖）。查不到时也落盘 error，返回 (None, 原因)。"""
    try:
        usage = (
            quota.fetch_usage_codex(config)
            if runner == "codex" else quota.fetch_usage_claude(config)
        )
    except (quota.UsageUnavailable, quota.UsageParseError) as exc:
        quota.write_quota_runner(
            runner, {"usage": None, "fetched_at": to_iso(now), "error": str(exc)}
        )
        return None, str(exc)
    quota.write_quota_runner(
        runner, {"usage": usage, "fetched_at": to_iso(now), "error": None}
    )
    return usage, ""


def _try_launch(task: dict, status: dict, config: dict, now: datetime) -> list[str]:
    task_id = task["id"]
    # F4：跟 _checkpoint_shift/_finalize_done 口径一致——项目已经从
    # config.projects 里删掉时 fail-closed 到 failed，不能让 KeyError
    # 从 tick 里直接炸出来（那会连累它后面排队的任务这一轮全部处理不到）。
    project_path = (config.get("projects") or {}).get(task["project"])
    if not project_path:
        return _fail_now(
            task, config, now, f"项目 {task.get('project')} 已不在 config.projects，不能起跑"
        )
    # S7：这一班自己的有效工人（review 角色可能跟顶层 build runner 不同）。
    runner = store.effective_runner(task)

    # a. 目录信任：没点过信任，交互式 claude 会卡在信任问答——等人也没用，直接判失败。
    # Codex 不吃这份信任记录（自己的信任状态在 ~/.codex/config.toml），跳过
    # 这条预检——它的信任由 launcher.launch() 在起会话前调用
    # ensure_codex_trusted() 持久化写盘解决（S7.6）。
    if runner == "claude" and not launcher.is_trusted(project_path):
        return _fail_now(
            task, config, now,
            f"目录未信任，请先手动在该目录开一次 claude：{project_path}",
        )

    # b. 同 pipeline 互斥（S7.1 阻断四 Part B）：以前只要候选与对方都是
    # worktree=true 就无条件放行，不比较是不是同一条 pipeline——同 pipeline
    # 内部本该"一个 held（角色 A）+ 对侧角色新班要起跑"这一种组合并存，任何
    # 其它组合（对方真的在跑、或对方是同角色 held）都是状态异常，必须先在
    # 这里挡住，不能被下面 c 段"两边都是 worktree就放行"的跨 pipeline 逻辑
    # 顺带放过。
    candidate_pid = store.pipeline_id_of(task)
    candidate_role = store.role_of(task)
    if not candidate_pid:  # pipeline_id_of 理论上总有兜底值，防御性 fail-closed
        return _fail_now(task, config, now, "算不出这一班所属的 pipeline_id，拒绝起跑")
    for other in store.list_tasks():
        other_task = other["task"]
        if other_task["id"] == task_id:
            continue
        other_pid = store.pipeline_id_of(other_task)
        if not other_pid or other_pid != candidate_pid:
            continue
        other_state = (other["status"] or {}).get("state")
        if other_state in ("launching", "working", "waiting_background"):
            reason = f"同流水线任务 {other_task['id']} 正在跑（{other_state}）"
            return _postpone(task, status, config, now, reason, notify=False)
        if other_state == "held" and store.role_of(other_task) == candidate_role:
            # 正常流程里同一条 pipeline 同一时刻只会有一个角色 held 着等对侧——
            # 走到"同角色也 held"说明状态机别处出了问题，不能当放行处理。
            reason = (
                f"同流水线已有 held 的同角色（{candidate_role}）任务 "
                f"{other_task['id']}，疑似状态异常"
            )
            store.update_status(
                task_id, state="needs_attention", error=reason,
                last_event_at=to_iso(now),
            )
            store.append_event(task_id, reason)
            launcher.open_notice_window(task, "(需要人工)", [reason], config)
            return [f"{task_id} 同流水线状态异常 → needs_attention"]
        # 其余情况（对方是对侧角色的 held，或对方是终态）不拦，交给下面
        # c 段按项目目录做跨 pipeline 判断。

    # c. 同目录不并跑：开第二个窗口两边抢文件系统，纯坏事。
    # S5 起：两个 worktree=true 的流水线各在各的树里施工，互不相干，允许并跑；
    # 只要候选或正在跑的一方是 worktree=false（一期老路径，直接在项目目录），
    # 仍按一期同目录锁推迟
    for other in store.list_tasks():
        if other["task"]["id"] == task_id:
            continue
        if (
            other["task"]["project"] == task["project"]
            and other["status"].get("state") in ACTIVE_STATES
        ):
            if worktree.wants_worktree(task) and worktree.wants_worktree(other["task"]):
                continue
            reason = f"同目录任务 {other['task']['id']} 还在跑"
            return _postpone(task, status, config, now, reason, notify=False)

    # d. 额度：只查这一班自己的 runner，查不到一律不放行（fail-closed）；
    # Claude 额度坏了不能拦 Codex 起跑，反之亦然
    usage, err = _fetch_and_record_usage(runner, config, now)
    if usage is None:
        return _postpone(task, status, config, now, f"额度查不到（fail-closed）：{err}")
    ok, reason = quota.check_guards(
        usage, store.effective_model(task), config, task.get("guards") or {}, runner=runner
    )
    if not ok:
        if store.role_of(task) == "review":
            _apply_review_no_quota_policy(task, config, now)
        return _postpone(task, status, config, now, reason)

    # e. 全过 → 起跑（launcher.launch 自己会先落盘 launching 再碰 tmux）
    store.update_status(
        task_id,
        quota_at_launch={
            "session_pct": usage.get("session_pct"),
            "week_all_pct": usage.get("week_all_pct"),
            "per_model": usage.get("per_model") or {},
            "fetched_at": to_iso(now),
            "quota_source": runner,
        },
    )
    # R5：launch 在 tmux 失败/未信任时会把 state 写成 failed 并返回该 status，
    # 不能闭着眼报"已启动"
    launched = launcher.launch(task_id, config)
    if launched.get("state") == "failed":
        return [f"{task_id} 启动失败：{launched.get('error')}"]
    return [f"{task_id} 已启动"]


# ---------- 推迟 / 判失败 ----------


def _postpone(
    task: dict, status: dict, config: dict, now: datetime, reason: str,
    notify: bool = True,
) -> list[str]:
    """推迟 = postponed + next_attempt_at；推迟窗口只在第一次推迟时开一个。"""
    task_id = task["id"]
    sch = config.get("scheduler") or {}
    postpone_minutes = sch.get("postpone_minutes", 30)
    max_postpone_hours = sch.get("max_postpone_hours", 6)
    run_at = parse_iso(task["run_at"])
    # S4 after 任务：6 小时上限从第一次满足前置的时刻（trigger_met_at）起算，
    # 不从 run_at 起算——等前置等了多久都不该吃掉推迟额度
    anchor = run_at
    if (task.get("trigger") or {}).get("type") == "after" and status.get("trigger_met_at"):
        anchor = parse_iso(status["trigger_met_at"])
    give_up_at = anchor + timedelta(hours=max_postpone_hours)
    if now >= give_up_at:
        return _fail_now(
            task, config, now,
            f"推迟超过 {max_postpone_hours} 小时仍不满足：{reason}",
        )

    next_attempt_at = now + timedelta(minutes=postpone_minutes)
    first = status.get("state") != "postponed"
    store.update_status(
        task_id,
        state="postponed",
        next_attempt_at=to_iso(next_attempt_at),
        postpone_reason=reason,
        postponed_count=int(status.get("postponed_count") or 0) + 1,
        last_event_at=to_iso(now),
    )
    store.append_event(task_id, f"推迟：{reason}；{to_iso(next_attempt_at)} 再试")
    if first and notify:
        tz = timezone(timedelta(hours=config["display_tz_offset_hours"]))
        launcher.open_notice_window(
            task, "(推迟)",
            [
                f"原因：{reason}",
                f"下次尝试：{next_attempt_at.astimezone(tz).strftime('%Y-%m-%d %H:%M')}"
                "（本地时间）",
                f"最多推到：{give_up_at.astimezone(tz).strftime('%Y-%m-%d %H:%M')}"
                "，届时仍不满足就判失败",
            ],
            config,
        )
    return [f"{task_id} 推迟：{reason}"]


def _fail_now(task: dict, config: dict, now: datetime, error: str) -> list[str]:
    """判失败并开失败窗口（不推迟）。"""
    store.update_status(
        task["id"], state="failed", error=error, last_event_at=to_iso(now)
    )
    store.append_event(task["id"], f"判失败：{error}")
    launcher.open_failure_window(task, error, config)
    return [f"{task['id']} 判失败：{error}"]


# ---------- 崩溃恢复：launching（设计稿 §3） ----------


def _read_exit_code(task_id: str) -> int | None:
    """读 run.sh 在 claude 退出后写下的 exit_code；没有/认不出 → None。"""
    path = store.task_dir(task_id) / "exit_code"
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _check_launching(
    task: dict, status: dict, config: dict, now: datetime
) -> list[str]:
    task_id = task["id"]
    sch = config.get("scheduler") or {}
    grace = timedelta(seconds=sch.get("launch_grace_seconds", 180))

    # R1：run.sh 在 claude 退出后会写 exit_code，而 pane 靠 read 留窗——
    # "窗口在 + pane 在"是假活。文件存在就是死透的铁证，宽限期内也照样重试。
    exit_code = _read_exit_code(task_id)
    launched_at = status.get("launched_at")
    in_grace = not launched_at or now - parse_iso(launched_at) < grace
    if exit_code is None:
        if in_grace:
            return []  # 宽限期内不做恢复判断
        if int(status.get("turns") or 0) > 0:
            return []  # 其实有 hook 来过，不按崩溃处理

        window_id = status.get("window_id")
        pane_pid = status.get("pane_pid")
        # PID 复用兜底：窗口在 + pane 进程在，才算"claude 还在起"
        if (
            window_id
            and launcher.window_alive(str(window_id), config)
            and pane_pid
            and launcher.pid_alive(int(pane_pid))
        ):
            return []

    retries = int(status.get("retries") or 0) + 1
    retry_max = int(task.get("retry_max") or DEFAULT_RETRY_MAX)
    if retries > retry_max:
        return _fail_now(
            task, config, now,
            f"启动重试超限：重试 {retries} 次仍没等到首个 hook 且窗口不在"
            f"（retry_max={retry_max}）",
        )
    # 回到 scheduled，下一 tick 重起。R4：run_at 一个字不改（_postpone 的
    # 6 小时上限锚在原始 run_at 上），重试时刻记进 status.retry_at
    store.update_status(
        task_id,
        state="scheduled",
        retries=retries,
        retry_at=to_iso(now),
        last_event_at=to_iso(now),
    )
    if exit_code is None:
        store.append_event(
            task_id,
            f"启动重试 {retries}/{retry_max}：宽限期过后没有首个 hook，回到 scheduled",
        )
    else:
        # 留证：exit_code 改名 exit_code.<retries>，不碍下一次启动
        (store.task_dir(task_id) / "exit_code").rename(
            store.task_dir(task_id) / f"exit_code.{retries}"
        )
        store.append_event(
            task_id,
            f"启动重试 {retries}/{retry_max}：claude 起来就退了"
            f"（exit_code={exit_code}），回到 scheduled",
        )
    return [f"{task_id} 启动重试 {retries}/{retry_max}"]


# ---------- S6③：Codex 五小时额度到线，调度器主动叫停 ----------


def _check_codex_quota_pause(
    task: dict, status: dict, config: dict, now: datetime, window_id: str
) -> list[str] | None:
    """working 的 Codex 任务查一眼它自己那份 quota.json 分片：五小时线到了
    就 send-keys 叫停、转 waiting_wakeup 并记 quota_paused_until；没到线/
    查不到都返回 None（None ≠ "没发生"，只是"这次没什么可做"，调用方按
    正常流程继续走）。查不到刷新时间就按最长（5 小时）估一个，不卡死。

    S6.1 B2（总review F7 把等号改成拦）：
    - 到线（含等号）就拦——跟 `quota.check_guards` 的 `>=` 语义统一；
    - 分片过期（`fetched_at` 早于一个刷新周期）或它自己记的 `session_resets`
      已经 <= now（意味着这份百分比早该被新一轮刷新覆盖）都不按这份旧快照
      叫停，交给本 tick 末尾的 `_maybe_refresh_quota` 去刷新，不能拿着一份
      本该作废的旧数字先停下再马上因为刷出新数字被恢复；
    - send-keys 真的失败就不能假装已经停下了：不写 waiting_wakeup，留在
      working 让下一 tick 重试。

    S7.2 阻断六：review 角色不能走 build 的这套协议——build 语气的文案不
    要求 NEXT，`waiting_wakeup` 是 build 专属"自己设了缓存闹钟"语义。
    role=review 时改用 `hook.DEFAULT_REVIEW_QUOTA_PAUSE_TEXT`（末行要求
    NEXT: pending），send-keys 成功后**不改 state**（留在 working，等它
    真的发一个 Stop）——S7.1②已经把 review 的 Stop 统一成"永远转
    idle"，随后 `_check_review_idle`→`_review_pending` 按 `on_no_quota`
    接手，跟 Claude 走的是同一条后续路径。只落 `quota_paused_until`（供
    `_review_hold_resume_eta` 复用这份精确刷新时间）与
    `quota_pause_count`。
    """
    task_id = task["id"]
    guards = task.get("guards") or {}
    session_max = guards.get("session_pct_max")
    if session_max is None:
        return None
    slice_ = quota.load_quota_file().get("codex") or {}
    usage = slice_.get("usage")
    if not isinstance(usage, dict):
        return None
    session_pct = usage.get("session_pct")
    if not isinstance(session_pct, int) or session_pct < session_max:
        return None

    fetched_at = slice_.get("fetched_at")
    sch = config.get("scheduler") or {}
    refresh_after = timedelta(minutes=sch.get("quota_refresh_minutes", 30))
    if fetched_at:
        try:
            if now - parse_iso(fetched_at) >= refresh_after:
                return None  # 分片过期，等这轮末尾刷新，不按旧快照叫停
        except ValueError:
            return None  # 时间戳都认不出，更不敢信这份快照

    resets_at = usage.get("session_resets")
    if resets_at:
        try:
            if parse_iso(resets_at) <= now:
                return None  # 这份快照自己说的刷新时间都已经过了，肯定过期
        except ValueError:
            pass

    try:
        paused_until_dt = parse_iso(resets_at) if resets_at else now + timedelta(hours=5)
    except ValueError:
        paused_until_dt = now + timedelta(hours=5)
    paused_until = to_iso(paused_until_dt)
    is_review = store.role_of(task) == "review"
    if is_review:
        # DEFAULT_REVIEW_QUOTA_PAUSE_TEXT 的 {resets_in} 占位符期待"还有几
        # 分钟"的整数（跟 hook.py 里 Claude 那份用法一致），不是原始 ISO
        # 时间戳——`resets_at` 已经在上面被 `parse_iso` 成功解析进
        # `paused_until_dt`（否则会走 5 小时兜底分支），用它跟 now 的差值
        # 换算成分钟数，不能直接把 `resets_at` 塞进去。
        resets_in_val = (
            max(0, int((paused_until_dt - now).total_seconds() // 60)) if resets_at else None
        )
        text = store.render(
            config.get("review_quota_pause_text") or hook.DEFAULT_REVIEW_QUOTA_PAUSE_TEXT,
            session_left=100 - session_pct,
            session_line_left=100 - session_max,
            resets_in=("未知" if resets_in_val is None else resets_in_val),
        )
    else:
        text = store.render(
            config.get("codex_quota_pause_text") or DEFAULT_CODEX_QUOTA_PAUSE_TEXT,
            session_left=100 - session_pct,
            session_line_left=100 - session_max,
            resets_at=resets_at or "未知时间",
        )
    proc = launcher.send_keys(window_id, text)
    if proc.returncode != 0:
        store.append_event(
            task_id,
            f"Codex 五小时额度到线（{session_pct}%）但 send-keys 失败"
            f"（returncode={proc.returncode}），未能让它停下，留在 working 下 tick 重试",
        )
        return [f"{task_id} Codex 额度到线但叫停失败"]
    if is_review:
        # S7.2 阻断六：不转 waiting_wakeup（build 专属语义）——留在
        # working，等它真的发一个 Stop（应该带 NEXT: pending），走
        # S7.1②建好的 review 统一 idle → _check_review_idle →
        # _review_pending 路径接手，不在这里替它判断。
        store.update_status(
            task_id,
            quota_paused_until=paused_until,
            quota_pause_count=int(status.get("quota_pause_count") or 0) + 1,
            last_event_at=to_iso(now),
        )
        store.append_event(
            task_id,
            f"Codex review 五小时额度到线（{session_pct}%）→ 已要求当场 NEXT:pending，"
            f"约 {paused_until} 后可恢复",
        )
        return [f"{task_id} Codex review 额度到线，已要求 pending"]
    store.update_status(
        task_id,
        state="waiting_wakeup",
        quota_paused_until=paused_until,
        quota_resume_sent=False,
        quota_pause_count=int(status.get("quota_pause_count") or 0) + 1,
        last_event_at=to_iso(now),
    )
    store.append_event(
        task_id,
        f"Codex 五小时额度到线（{session_pct}%）→ 已 send-keys 停下，"
        f"约 {paused_until} 后调度器主动叫醒",
    )
    return [f"{task_id} Codex 额度到线，已停下等 {paused_until}"]


# ---------- S6④：Codex 后台进程登记簿核对（F12） ----------


_BACKGROUND_HEARTBEAT_STALE_SECONDS_DEFAULT = 90


def _background_heartbeat_stale(rec: dict, now: datetime, config: dict) -> bool:
    """wrapper 是前台进程，正常跑的话每 ~1 秒刷一次 heartbeat_at；心跳长期不
    动多半是原 wrapper/沙箱丢了（比如那次 exec 的 PID namespace 整个没了），
    不能让任务永远卡在 waiting_background 等一个再也不会来的完成事件。"""
    hb = rec.get("heartbeat_at")
    if not hb:
        return False  # 刚起还没来得及第一次心跳，别误判
    try:
        hb_dt = parse_iso(hb)
    except (ValueError, TypeError):
        return False
    sch = config.get("scheduler") or {}
    grace = sch.get("background_heartbeat_stale_seconds")
    if grace is None:
        grace = _BACKGROUND_HEARTBEAT_STALE_SECONDS_DEFAULT
    return (now - hb_dt).total_seconds() > grace


def _reconcile_codex_background(
    task: dict, status: dict, config: dict, now: datetime, window_id: str, alive: bool,
) -> list[str] | None:
    """核对这个 codex 任务的后台登记簿（background_runner.py）。

    S6.1 A3/A4 修正（详见返修令 A3/A4），S6.1 二次返修修正窗口/thread 身份
    核验的覆盖范围（详见二次返修令"阻断一"）：

    - finished 与 stopped 都是"需要通知原会话读取并继续"的终态——只认
      finished 的话，stop 请求处理完之后落的 stopped 永远不会被通知，
      任务会卡死在 waiting_background；
    - 窗口/thread 身份核验必须覆盖**所有未收口项**（state==running，或
      finished/stopped 但还没 notified），不能只查 finished_pending——
      否则一个新鲜的 running 项赶上窗口消失，会绕过这条检查被通用
      window_gone 分支抢答成 exited；thread 不符也会被误判成"还在正常
      跑"摁回 waiting_background，两者都违反 F12 的"未收口时窗口/会话
      有问题必须 needs_attention，不能普通退场或装作没事"；
    - registry 里只剩已经 notified 的终态项（没有未收口项）时，这条核验
      不生效，窗口消失走回通用 exited(window_gone)——这是正常退场，不该
      被 F12 拦下；
    - 窗口/thread 都没问题，但当前顶层状态是 working（正忙着别的事）：
      不打断，返回 None 交回正常流程，等它自然停到 idle/waiting_background
      再通知；
    - send-keys 真失败（returncode != 0）：不能假装已经通知，留 pending 转
      needs_attention，不能悄悄再试到地老天荒；
    - 有登记中的 running 项但心跳超时：原 wrapper 大概率丢了，同样不分顶层
      状态，转 needs_attention + 单次告警；
    - 只有还在跑、心跳健康、且当前允许打扰（idle/waiting_background）时，
      才把 state 摁回 waiting_background；
    - 都没有：返回 None，交回调用方按原状态机走。
    """
    task_id = task["id"]
    registry = background_runner.load_registry(task_id)
    if not registry:
        return None

    can_notify = status.get("state") in ("idle", "waiting_background")
    current_thread = status.get("thread_id")

    # 二次返修阻断一：先圈出所有"未收口"项（running，或 finished/stopped
    # 但还没 notified）——窗口/thread 身份核验必须看着这整个集合，不能只看
    # finished_pending，否则新鲜的 running 项会绕过这条检查。
    unresolved = [
        r for r in registry.values()
        if r.get("state") == "running"
        or (r.get("state") in ("finished", "stopped") and r.get("notification_state") != "notified")
    ]

    if unresolved:
        mismatched = [
            r for r in unresolved
            if r.get("thread_id_at_start") and current_thread
            and r.get("thread_id_at_start") != current_thread
        ]
        if not alive or mismatched:
            if not status.get("background_attention_noted"):
                if not alive:
                    reason = (
                        f"{len(unresolved)} 个后台任务尚未收口，"
                        "但窗口已消失，无法核对/通知它继续读取结果"
                    )
                else:
                    ids = "、".join(r["background_id"] for r in mismatched)
                    reason = (
                        f"{len(mismatched)} 个后台任务登记时的会话跟当前 thread_id 对不上号，"
                        f"不敢冒充通知：{ids}"
                    )
                store.update_status(
                    task_id, state="needs_attention", error=reason,
                    background_attention_noted=True, last_event_at=to_iso(now),
                )
                store.append_event(task_id, f"后台完成但窗口/会话对不上 → needs_attention：{reason}")
                launcher.open_notice_window(task, "(需要人工)", [reason], config)
            return [f"{task_id} 后台完成但窗口/会话对不上 → needs_attention"]

    finished_pending = [
        r for r in unresolved
        if r.get("state") in ("finished", "stopped")
    ]
    if finished_pending:
        if not can_notify:
            return None  # 窗口/会话都没问题，但正忙着 working，不打断

        def _verb(r: dict) -> str:
            return "已结束" if r.get("state") == "finished" else "已停止"

        lines = [
            f"来自nightshift：后台任务 {r['background_id']} {_verb(r)}"
            f"（exit={r.get('exit_code')}），结果在 {r.get('result_path')}，请读取并继续。"
            for r in finished_pending
        ]
        # 总review F9：send_keys 现在走 paste-buffer -r 保留裸 LF，多条完成
        # 通知拼成的这一块文本对 Codex TUI 是否安全没验证过——改用中文分号
        # 拼成单行，不再指望裸换行块。
        proc = launcher.send_keys(window_id, "；".join(lines))
        if proc.returncode != 0:
            if not status.get("background_attention_noted"):
                reason = f"后台完成但 send-keys 失败（returncode={proc.returncode}），未能通知它继续"
                store.update_status(
                    task_id, state="needs_attention", error=reason,
                    background_attention_noted=True, last_event_at=to_iso(now),
                )
                store.append_event(task_id, f"后台完成通知失败 → needs_attention：{reason}")
                launcher.open_notice_window(task, "(需要人工)", [reason], config)
            return [f"{task_id} 后台完成通知失败 → needs_attention"]

        ids = [r["background_id"] for r in finished_pending]

        def mark_notified(data: dict) -> None:
            for bid in ids:
                rec = data.get(bid)
                if rec is not None:
                    rec["notification_state"] = "notified"
                    rec["notified_at"] = to_iso(now)

        background_runner.modify_registry(task_id, mark_notified)
        store.append_event(task_id, f"后台完成已通知，已 send-keys 唤醒：{'、'.join(ids)}")
        if status.get("state") != "waiting_background":
            store.update_status(task_id, state="waiting_background", last_event_at=to_iso(now))
        return [f"{task_id} 后台完成，已敲醒继续读取结果：{'、'.join(ids)}"]

    running = [r for r in registry.values() if r.get("state") == "running"]
    stale = [r for r in running if _background_heartbeat_stale(r, now, config)]
    if stale:
        if not status.get("background_attention_noted"):
            ids = "、".join(r["background_id"] for r in stale)
            reason = f"{len(stale)} 个后台任务心跳超时（原 wrapper 可能已丢失）：{ids}"
            store.update_status(
                task_id, state="needs_attention", error=reason,
                background_attention_noted=True, last_event_at=to_iso(now),
            )
            store.append_event(task_id, f"后台心跳超时 → needs_attention：{reason}")
            launcher.open_notice_window(task, "(需要人工)", [reason], config)
        return [f"{task_id} 后台心跳超时 → needs_attention"]

    if running and can_notify and status.get("state") != "waiting_background":
        store.update_status(task_id, state="waiting_background", last_event_at=to_iso(now))
        store.append_event(
            task_id,
            f"仍有 {len(running)} 个后台任务在跑，state 摁回 waiting_background"
            "（不许 idle/存档/换班）",
        )
        return [f"{task_id} 后台仍在跑 → waiting_background"]

    return None


# ---------- 运行期巡检：working / waiting_background / idle（设计稿 §5.2） ----------


def _send_quota_resume(task_id: str, window_id: str, text: str) -> list[str] | None:
    """F3：额度刷新时间到了、叫它继续这一步的 send-keys + 失败处理——idle
    分支（闹钟已响完但没等到事件）与 Claude waiting_wakeup 超过 60 分钟
    宽限期两处共用，不许各自复制一份（S6.1 B2：send-keys 真失败不能假装
    已经叫醒了它）。失败只记事件、返回给调用方的 action 提示；成功返回
    None，落盘 quota_resume_sent/清 quota_paused_until 与记事件的措辞由
    调用方决定（两种场景说法不同）。
    """
    proc = launcher.send_keys(window_id, text)
    if proc.returncode != 0:
        store.append_event(
            task_id,
            f"额度刷新时间已到但 send-keys 失败（returncode={proc.returncode}），"
            "未能让它继续",
        )
        return [f"{task_id} 额度刷新但叫醒失败"]
    return None


def _check_running(
    task: dict, status: dict, config: dict, now: datetime
) -> list[str]:
    task_id = task["id"]

    # S7.2 兼容尾巴 2：coordinator 身份核验。`pipeline_id_of(task)` 算出的
    # coordinator id 如果不是自身，必须真的对应一个存在的 task（有
    # task.json）——核验不过就 fail-closed 到 needs_attention，不能让后面
    # 任何 `_update_coordinator()`/`_coordinator_status()` 调用（散布在
    # 审稿流水线各处：`_hold_blocks`/`_start_review_round`/`_review_fix`/
    # `_review_done`/`_reconcile_stale_fix_intent` 等）凭空建出只有
    # status.json、没有 task.json 的"幽灵 coordinator"目录。放在整个函数
    # 最开头，早于 R1 存活判断与任何角色分支逻辑。
    coordinator_id = store.pipeline_id_of(task)
    if coordinator_id != task_id:
        try:
            store.load_task(coordinator_id)
        except (OSError, ValueError):
            reason = f"pipeline_id 指向的 coordinator 任务 {coordinator_id} 不存在，链路损坏"
            already_flagged = (
                status.get("state") == "needs_attention" and status.get("error") == reason
            )
            if not already_flagged:
                store.update_status(
                    task_id, state="needs_attention", error=reason, last_event_at=to_iso(now)
                )
                store.append_event(task_id, reason)
            if not status.get("coordinator_broken_noted"):
                launcher.open_notice_window(task, "(需要人工)", [reason], config)
                store.update_status(task_id, coordinator_broken_noted=True)
            return [f"{task_id} coordinator 链路损坏 → needs_attention"]

    # R1 兜底：run.sh 写了 exit_code 就是 claude 死透（SessionEnd hook 超时/
    # 被杀没来的情形）；窗口还在也只是 read 留窗的假象。SessionEnd 正常时
    # 它会先把 state 写成 exited，轮不到这条。
    exit_code = _read_exit_code(task_id)
    if exit_code is not None:
        store.update_status(
            task_id,
            state="exited",
            exit_reason=f"claude_exit_{exit_code}",
            last_event_at=to_iso(now),
        )
        store.append_event(
            task_id,
            f"run.sh 报 claude 已退出（exit_code={exit_code}）且没等到"
            f" SessionEnd → exited(claude_exit_{exit_code})",
        )
        return [
            f"{task_id} claude 退出（exit_code={exit_code}）"
            f"→ exited(claude_exit_{exit_code})"
        ]

    window_id = status.get("window_id")
    pane_pid = status.get("pane_pid")
    alive = (
        bool(window_id)
        and launcher.window_alive(str(window_id), config)
        and bool(pane_pid)
        and launcher.pid_alive(int(pane_pid))
    )

    # S7.2 阻断二：这条流水线的 coordinator 上如果留着一份没收口的
    # pending_fix_intent（上一次原地返工在写盘中途被打断），这一班（不管是
    # 卡住的 review 还是被摸不清进度的 build）都不该继续往下走正常流程——
    # fail-closed 到 needs_attention，等人工核对清楚。放在存活判断之后、
    # 任何角色分支之前，确保不管当前是哪个角色、哪个状态都逃不过这道关卡。
    stale_intent = _reconcile_stale_fix_intent(task, config, now)
    if stale_intent is not None:
        return stale_intent

    # S7：这一班自己的有效工人（review 角色可能跟顶层 build runner 不同）。
    runner = store.effective_runner(task)

    # S6.1 A4：Codex 的 F12 后台核对必须在通用 window_gone 判断之前做，且不
    # 分当前顶层状态（working 也要查）——原来挂在通用 alive 分支之后、又只
    # 在 idle/waiting_background 时才查，窗口一旦消失就被通用分支抢先判成
    # exited(window_gone)，F12 自己那条"后台做完了但没人能读"的
    # needs_attention 永远没机会触发，registry 里的完成结果就这么静默丢单。
    # `_reconcile_codex_background` 内部自己按当前状态决定要不要真的打扰
    # working 中的会话（只有 idle/waiting_background 才会 send-keys 通知/
    # 摁回 waiting_background；needs_attention 类的告警不分状态都会触发）。
    if runner == "codex":
        registry = background_runner.load_registry(task_id)
        if registry:
            bg_result = _reconcile_codex_background(
                task, status, config, now, str(window_id) if window_id else "", alive,
            )
            if bg_result is not None:
                return bg_result

    if not alive:
        # 窗口没了又没等到 SessionEnd → 按退场处理（F12 有话说的情形已经在
        # 上面被拦下，走不到这里；这里只处理"确实没有未处理的后台完成/丢失"
        # 的普通窗口消失）
        store.update_status(
            task_id, state="exited", exit_reason="window_gone",
            last_event_at=to_iso(now),
        )
        store.append_event(task_id, "窗口不在了且没等到 SessionEnd → exited(window_gone)")
        return [f"{task_id} 窗口消失 → exited(window_gone)"]

    # S4 疑似卡住：working/waiting_background 静默太久（一条前台工具调用里
    # 轮询、轮次不结束、hook 不响）。只标状态与事件，不动会话、不改 state；
    # 恢复由 hook 事件清 stuck（hook.py 唯一允许的改动）。
    #
    # S6.1 A5：Codex 有新鲜心跳的 running 后台项时不适用这条判定——那是给
    # "前台工具调用轮询很久没响应"设计的，F12 后台任务心跳每秒刷新，健康
    # 跑着的后台不该被当成同一回事，否则会被误判成卡住甚至触发自动 Esc
    # 打断前台会话（跟这个后台进程本身毫无关系，纯属误伤）。
    codex_has_fresh_background = False
    if runner == "codex":
        bg_registry = background_runner.load_registry(task_id)
        codex_has_fresh_background = any(
            r.get("state") == "running" and not _background_heartbeat_stale(r, now, config)
            for r in bg_registry.values()
        )

    sch = config.get("scheduler") or {}
    stuck_minutes = sch.get("stuck_minutes")
    if stuck_minutes is None:
        stuck_minutes = 15  # config 没写时的兜底（与 config.example.json 一致）
    stuck = False
    if (
        not codex_has_fresh_background
        and status.get("state") in ("working", "waiting_background")
        and stuck_minutes > 0
        and status.get("last_event_at")
    ):
        stuck = now - parse_iso(status["last_event_at"]) >= timedelta(minutes=stuck_minutes)
    if stuck and not status.get("stuck"):
        store.update_status(task_id, stuck=True, stuck_since=to_iso(now))
        store.append_event(
            task_id,
            f"疑似卡住：已经 {stuck_minutes} 分钟没有任何 hook 事件（可能卡在一条工具调用里）",
        )
    # 可选自动中止：guards.auto_interrupt_minutes（默认关）。stuck 持续超过它
    # 就往窗口按一次 Esc；auto_interrupted 落盘防重复。
    auto_minutes = (task.get("guards") or {}).get("auto_interrupt_minutes")
    if (
        stuck
        and auto_minutes
        and not status.get("auto_interrupted")
        and window_id
        and now - parse_iso(status.get("stuck_since") or to_iso(now))
        >= timedelta(minutes=int(auto_minutes))
    ):
        launcher.send_escape(str(window_id))
        # Esc 本身不会让调度器收到任何回信（Stop hook 不认用户中断）；紧接着
        # 敲一段话进去起个新轮次，靠新轮次自然结束时的 hook 事件才能真正复原。
        interrupt_text = store.render(
            config.get("stuck_interrupt_text") or DEFAULT_STUCK_INTERRUPT_TEXT,
            stuck_minutes=int(auto_minutes),
        )
        launcher.send_keys(str(window_id), interrupt_text)
        store.update_status(task_id, auto_interrupted=True)
        store.append_event(
            task_id, f"疑似卡住已超过 {auto_minutes} 分钟 → 自动中止（Esc）+ 注入自检提示"
        )

    # R2：auto 被 CC 静默回落（如 haiku 只吃 default），无人值守会整晚卡在
    # 权限问答。开窗提醒一次就够——不改 state、不杀窗口，人来处理。
    # S7.1 阻断五：review 角色故意用 dontAsk（无人值守拒绝语义，见
    # launcher._claude_command），不是"被静默回落"，不该被这条误报。
    mode = status.get("permission_mode")
    review_dont_ask = store.role_of(task) == "review" and mode == "dontAsk"
    if (
        mode
        and mode not in ("auto", "bypassPermissions")
        and not review_dont_ask
        and not status.get("mode_warned")
    ):
        launcher.open_notice_window(
            task,
            "(注意)",
            [
                f"会话没进 auto 模式（当前：{mode}），无人值守会卡在权限问答",
                "多半是该模型不支持 auto；换 sonnet/opus/fable 之类的模型重建任务",
            ],
            config,
        )
        store.update_status(task_id, mode_warned=True)
        store.append_event(
            task_id, f"会话权限模式是 {mode} 不是 auto，已开提醒窗口（不改状态）"
        )
        return [f"{task_id} 权限模式 {mode} 非 auto → 提醒窗口"]

    # S6③：Codex 没有 ScheduleWakeup（不能自己定缓存闹钟），working 时五小时
    # 线到了必须由调度器主动 send-keys 叫它停下、转 waiting_wakeup；
    # Claude 走 hook.py 的 _quota_check 自助报告，这条只管 codex。
    if runner == "codex" and status.get("state") == "working":
        codex_pause = _check_codex_quota_pause(task, status, config, now, str(window_id))
        if codex_pause is not None:
            return codex_pause

    # F11：build 的 idle 去抖——刚落定的 idle 可能只是 Stop→idle 与紧接着
    # 的 UserPromptSubmit→working 之间那道缝里的瞬时状态，还没到这一
    # tick 就已经又在干活了。放在五小时暂停补敲判断之前：补敲同样不该
    # 打在瞬时 idle 上（会话其实在忙，被当成"该催了"敲一句自作主张的
    # "请继续"）。review 角色的存活判定走独立分支（S7），不受这条影响。
    # last_event_at 距 now 为负（测试里 now 常年固定在过去某个时刻）视为
    # 已经稳定，不当瞬时。
    if (
        store.role_of(task) != "review"
        and status.get("state") == "idle"
        and status.get("last_event_at")
    ):
        delta = (now - parse_iso(status["last_event_at"])).total_seconds()
        if 0 <= delta < IDLE_SETTLE_SECONDS:
            return []

    # 五小时额度暂停：它该在等缓存闹钟。Claude 没定闹钟就停了的（idle），
    # 刷新时间一到敲一句让它继续；Codex 没有自己定闹钟的能力，闹钟到点
    # 后必须由调度器主动敲，不能"等它自己醒"。刷新时间没到之前 idle
    # 也不算干完，不许收尾/续班
    # S7.2 阻断六（自查补充）：这段是 build 专属的"五小时线自己设了缓存
    # 闹钟，刷新时间到了主动敲它继续"协议。review 角色不用它——
    # `_check_codex_quota_pause` 现在给 review 发的是要求 NEXT:pending 的
    # 文案且不改 state，review 后续的 Stop 会把 state 转回 idle 但
    # `quota_paused_until` 仍然留着；如果不排除 review，这里会在
    # `_check_review_idle` 之前抢先拦截，轻则让已经落盘的 pending verdict
    # 迟迟得不到路由，重了到点还会往 review 窗口发一句 build 语气的"请继续"
    # （不是 review 的 NEXT 协议），产生跟阻断六同一类的误判。review 的
    # idle+quota_paused_until 场景交给下面的 `_check_review_idle` 与专属
    # hold 恢复分支处理，不进这段。
    paused_until = status.get("quota_paused_until")
    if (
        paused_until and status.get("state") in ("idle", "waiting_wakeup")
        and store.role_of(task) != "review"
    ):
        if now < parse_iso(paused_until):
            return []
        codex_waiting_wakeup = (
            runner == "codex" and status.get("state") == "waiting_wakeup"
        )
        if (
            status.get("state") == "idle" or codex_waiting_wakeup
        ) and not status.get("quota_resume_sent"):
            text = (
                config.get("codex_resume_text") or DEFAULT_CODEX_RESUME_TEXT
                if runner == "codex"
                else DEFAULT_CLAUDE_RESUME_TEXT
            )
            # S6.1 B2：send-keys 真失败不能假装已经叫醒了它——不写
            # quota_resume_sent/清 quota_paused_until，留在原状态下 tick 重试
            failure = _send_quota_resume(task_id, str(window_id), text)
            if failure is not None:
                return failure
            store.update_status(task_id, quota_resume_sent=True, quota_paused_until=None)
            store.append_event(task_id, "额度刷新时间已到，已 send-keys 让它继续")
            return [f"{task_id} 额度刷新，敲它继续"]
        if status.get("state") == "waiting_wakeup":
            # F3：Claude 的闹钟若被 CC 的 cron 丢了（没触发），永远没人敲——
            # 给个宽限期，超过 CLAUDE_WAKEUP_GRACE_MINUTES 仍没等到它自己醒
            # 就由调度器主动 send-keys 叫它继续，跟上面 idle 分支同一套
            # 文案/失败处理。没到宽限期（或本来就是 Codex，已经在上面的
            # codex_waiting_wakeup 分支处理过）仍然只能等。
            if (
                runner == "claude"
                and not status.get("quota_resume_sent")
                and now >= parse_iso(paused_until) + timedelta(minutes=CLAUDE_WAKEUP_GRACE_MINUTES)
            ):
                failure = _send_quota_resume(task_id, str(window_id), DEFAULT_CLAUDE_RESUME_TEXT)
                if failure is not None:
                    return failure
                store.update_status(task_id, quota_resume_sent=True, quota_paused_until=None)
                store.append_event(
                    task_id, "额度刷新已过 60 分钟仍未自醒，已 send-keys 让它继续"
                )
                return [f"{task_id} 额度刷新已过 60 分钟未自醒，敲它继续"]
            return []  # Claude：闹钟还没响完/还没到宽限期，等它自己醒

    # S7.1 阻断二/三：review 角色因额度到线转 held（scheduler._review_pending
    # 的 on_no_quota=hold 分支）——上面那段只认 idle/waiting_wakeup，held
    # 状态永远等不到"额度刷新后主动继续"的 send-keys，会永久卡住。
    if (
        paused_until and status.get("state") == "held"
        and store.role_of(task) == "review"
        and not status.get("quota_resume_sent")
    ):
        if now < parse_iso(paused_until):
            return []
        text = config.get("review_resume_text") or DEFAULT_REVIEW_RESUME_TEXT
        # S7.2 阻断五.3：resume 不是控制 turn（接下来期待一份真正的
        # verdict）；S7.3 阻断二：awaiting_verdict=True 必须在 send 之前
        # 落盘（放进 pre_send_fields），否则 Stop 抢在 send 返回前到达时
        # 看到的仍是旧值，会被当成协议缺失误判——不是"send 失败污染状态"
        # 这一种竞态，是"send 成功但落盘还没跟上"这一种，S7.2 的"send 后
        # 才落盘"顺序本身就不够。quota_resume_sent/state 这些不参与"下一
        # 次 Stop 怎么解释"判断的字段留在 success_only_fields，且只在这次
        # 投递没被 hook 抢先消费时才应用。
        sent = send_review_control(
            task_id, str(window_id), text, kind="resume",
            pre_send_fields={"review_awaiting_verdict": True},
            success_only_fields={
                "quota_resume_sent": True, "quota_paused_until": None,
                "state": "working", "last_event_at": to_iso(now),
            },
            failure_note="，未能叫醒它继续",
        )
        if not sent:
            return [f"{task_id} 审稿额度刷新但叫醒失败"]
        store.append_event(task_id, "审稿额度刷新时间已到，已 send-keys 让它继续（等待新一轮 verdict）")
        return [f"{task_id} 审稿额度刷新，敲它继续"]

    # S3 换班：idle 收尾后按交接文件接下一班（每次评估先落 chain_checked
    # 防重复，好过重复开出双份后继；F4：评估中途炸了不再悄悄晾在 idle，
    # _check_idle_chain/_check_exited_chain 会转 needs_attention 并留人话）。
    # S7：review 角色走独立的 verdict 分流，不进 build 的交接判定。
    if status.get("state") == "idle":
        if store.role_of(task) == "review":
            result = _check_review_idle(task, status, config, now)
            if result is not None:
                return result
        elif not status.get("chain_checked"):
            return _check_idle_chain(task, status, config, now)

    if status.get("state") not in ("waiting_background", "held"):
        return []  # idle 永远不戳（8/27 事故的反面，设计稿 §5.2）

    guards = task.get("guards") or {}
    if not guards.get("keepalive", True):
        return []
    if (task.get("keepalive") or {}).get("enabled") is False:
        return []  # S7：task.keepalive.enabled 是长期开关，跟 guards.keepalive 并存
    if status.get("keepalive_paused"):
        return []  # S7：按钮暂停位，只停戳不改状态/流程

    # S6③：保活分家——claude 50 分钟、codex 25 分钟（GPT-5.6 缓存 30 分钟，
    # 见靶测记录 F6），各自文案；旧配置没有 config.runners 时兼容视图会从
    # scheduler.keepalive_* 合成，claude 这条数字/文案跟一期一字不变
    rc = store.runner_config(config).get(runner) or {}
    idle_needed = timedelta(minutes=rc.get("keepalive_idle_minutes", 50 if runner == "claude" else 25))
    stamps = [
        parse_iso(status[key])
        for key in ("last_event_at", "last_keepalive_at")
        if status.get(key)
    ]
    if not stamps or now - max(stamps) < idle_needed:
        return []

    text = rc.get("keepalive_text") or DEFAULT_KEEPALIVE_TEXT
    # S7.1 阻断二：保活探针不是要求正式 verdict 的回复——review 角色要让
    # review_awaiting_verdict=False + review_control_kind="keepalive" 在
    # send 之前就生效（S7.3 阻断二：不能等 send 成功后才落盘，否则 Stop
    # 抢在落盘之前到达仍会被误判协议缺失、记成 fix；build 角色没有这套
    # awaiting_verdict 概念，pre_send_fields 留空）；last_keepalive_at/
    # keepalive_count 这类不参与"下一次 Stop 怎么解释"判断的计数字段留在
    # success_only_fields，只在确认送达后才落盘。
    # F1：build 角色也要走跟 review 对称的控制 turn 标记——保活探针打进
    # held 会话后 UserPromptSubmit/Stop 一样会触发，不落 build_control_kind
    # 的话 hook.py 没法认出这是控制回复，会把 build 误判成收工/idle（Fable
    # 审查 A1，9/1）。build 没有 awaiting_verdict 概念，只带 control_kind 一个字段。
    pre_send_fields: dict = {}
    if store.role_of(task) == "review":
        pre_send_fields = {
            "review_awaiting_verdict": False, "review_control_kind": "keepalive",
        }
    else:
        pre_send_fields = {"build_control_kind": "keepalive"}
    sent = send_review_control(
        task_id, str(window_id), text, kind="keepalive",
        pre_send_fields=pre_send_fields,
        success_only_fields={
            "last_keepalive_at": to_iso(now),
            "keepalive_count": int(status.get("keepalive_count") or 0) + 1,
        },
        failure_note="（计数/控制标记均未落盘，下一 tick 会重试）",
    )
    if sent:
        store.append_event(
            task_id, f"保活戳：{status.get('state')} 静默超时，已 send-keys 探针"
        )
        return [f"{task_id} 保活戳"]
    return [f"{task_id} 保活戳投递失败"]


# ---------- 换班：交接判定与后继任务（设计稿 §4.4） ----------


def _handover_file(task: dict, status: dict) -> Path:
    """交接文件路径：status 里 hook 记下的 handover_path 优先，
    没有就按 task_dir/handover-<shift>.md 算。"""
    recorded = status.get("handover_path")
    if recorded:
        return Path(recorded)
    return store.task_dir(task["id"]) / f"handover-{int(task.get('shift') or 1)}.md"


def _read_handover(path: Path) -> str | None:
    """读整份交接（strip 后为空当没有）；文件不存在返回 None。"""
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace").strip() or None


def _last_nonempty_line(text: str) -> str:
    return [ln for ln in (ln.strip() for ln in text.splitlines()) if ln][-1]


def _chain_eval_failed(
    task: dict, config: dict, now: datetime, exc: Exception
) -> list[str]:
    """F4：`_check_idle_chain`/`_check_exited_chain` 评估中途炸了的统一兜底。

    以前的代价只是"这班不再自动续"——chain_checked 已经落盘、状态原样
    停在 idle，网页上看着像正常空闲，没有任何错误提示，工头根本发现不了。
    现在统一转 needs_attention：写 error、记事件、开提醒窗，工头能在网页
    上看到、处理完可以合并/丢弃/重建。
    """
    task_id = task["id"]
    reason = f"收工评估失败：{exc!r}"
    store.update_status(
        task_id, state="needs_attention", error=reason, last_event_at=to_iso(now)
    )
    store.append_event(task_id, f"收工评估失败 → needs_attention：{exc!r}")
    logger.exception("收工评估失败：任务 %s", task_id)
    launcher.open_notice_window(
        task, "(需要人工)",
        [
            f"收工评估失败：{exc!r}",
            "这班不再自动续班/审稿；处理完可在网页合并、丢弃或重建",
        ],
        config,
    )
    return [f"{task_id} 收工评估失败 → needs_attention"]


def _check_idle_chain(
    task: dict, status: dict, config: dict, now: datetime
) -> list[str]:
    """idle 的换班评估（设计稿 §4.4 第 3 条）。

    先落 chain_checked=True 防重复评估；S5② 起收工边界先打存档点（幂等，
    失败立即止住）；再看交接文件：
    - 有交接按末行 NEXT: continue/done 判（没写 NEXT 按 continue）；
    - 没交接但这班被提醒过 → 按 chain.on_no_handover（continue/stop）；
    - 没交接也从未被提醒 → 正常干完（worktree 任务走 _finalize_done 分流）。

    F4：chain_checked=True 之后的所有逻辑包了一层 try/except——中途异常
    （比如 config.models 改名后 create_successor 的 validate_task 抛
    ValueError）不再让任务悄悄停在 idle 没有任何记录，统一转
    needs_attention 并留人话（见 `_chain_eval_failed`）。
    """
    store.update_status(task["id"], chain_checked=True)
    try:
        blocked = _checkpoint_shift(task, status, config, now)
        if blocked:
            return blocked
        path = _handover_file(task, status)
        text = _read_handover(path)
        if text is not None:
            return _handover_verdict(task, status, text, config, now)

        if status.get("context_warned_at"):  # 这班被提醒过却没留交接
            policy = (task.get("chain") or {}).get("on_no_handover") or "continue"
            if policy == "stop":
                store.update_status(
                    task["id"], state="needs_attention", last_event_at=to_iso(now)
                )
                store.append_event(
                    task["id"],
                    "到线提醒过却没留交接，按 chain.on_no_handover=stop 停下等人",
                )
                launcher.open_notice_window(
                    task,
                    "(需要人工)",
                    [
                        "到线提醒后没留交接，按设置停下等人",
                        f"交接文件应在：{path}",
                    ],
                    config,
                )
                return [f"{task['id']} 提醒过没交接 → needs_attention"]
            store.append_event(
                task["id"],
                "到线提醒过却没留交接，按 chain.on_no_handover=continue 续班（兜底文案）",
            )
            return _chain_continue(task, status, config, now, handover_text=None)

        return _finalize_done(task, config, now)
    except Exception as exc:
        return _chain_eval_failed(task, config, now, exc)


def _handover_verdict(
    task: dict, status: dict, text: str, config: dict, now: datetime
) -> list[str]:
    """有交接时的判定（idle 与 exited 共用）：末行 NEXT: done → 完工分流；
    NEXT: continue（或没写 NEXT，按 continue）→ 续班。"""
    last = _last_nonempty_line(text)
    if _RE_NEXT_DONE.match(last):
        return _finalize_done(task, config, now)
    note = ""
    if not _RE_NEXT_CONTINUE.match(last):
        note = "（交接末行没写 NEXT，按 continue）"
    return _chain_continue(task, status, config, now, handover_text=text, note=note)


def _chain_continue(
    task: dict, status: dict, config: dict, now: datetime,
    handover_text: str | None, note: str = "",
) -> list[str]:
    """续班：班次没到上限就造后继任务（父任务转 chained）；到上限标
    chain_exhausted 并开提醒窗。后继下一 tick 走完整预检（额度不够就推迟）。

    S6.1 A7：Codex 续班在造完后继后，最好把父班窗口关掉——下一班要
    `codex resume` 同一个 thread，父窗口还开着的话会跟后继窗口同时持有
    同一个会话（两开）。这里是 best-effort（tmux 抽风/窗口已经不在都不算
    错误，`close_windows` 本来就只关它确认还活着的窗口）；真正兜底的是
    `launcher.launch()` 里那道"父窗还活着就拒绝 resume"的硬检查——就算这里
    关闭失败，后继下一 tick 也不会悄悄两开，而是 fail-closed。Claude 换班
    原样保留旧窗口（一期行为不变，Claude 没有"同一个会话"这个概念）。
    """
    task_id = task["id"]
    shift = int(task.get("shift") or 1)
    # S7：换班上限比较 role_shift（本角色本轮的续班计数），不再拿全局 shift
    # ——否则角色轮转（build→review→build…）会让审稿班吃掉施工班的窗口
    # 额度。纯 build 流水线（没有审稿）里 role_shift 与 shift 从头到尾同步
    # 递增，这条改动对它们零行为差异。
    role_shift = int(task.get("role_shift") or 1)
    chain = task.get("chain") or {}
    max_windows = int(chain.get("max_windows") or 3)
    if role_shift >= max_windows:
        store.update_status(
            task_id, state="chain_exhausted", last_event_at=to_iso(now)
        )
        store.append_event(
            task_id,
            f"本角色已连开 {role_shift} 班（上限 {max_windows}，全局第 {shift} 班）"
            "→ chain_exhausted",
        )
        launcher.open_notice_window(
            task,
            "(班次用尽)",
            [
                f"本角色已连开 {role_shift} 班（chain.max_windows={max_windows}），不再自动续班",
                "任务可能没做完；要继续可调大上限后在网页重建任务",
            ],
            config,
        )
        return [f"{task_id} 第 {shift} 班结束：班次用尽"]
    successor_id = store.create_successor(task, handover_text, config)
    if store.effective_runner(task) == "codex":
        window_id = status.get("window_id")
        if window_id:
            closed = launcher.close_windows([window_id], config)
            store.append_event(
                task_id,
                f"续班：已关闭父班窗口 {window_id}" if closed
                else f"续班：父班窗口 {window_id} 关闭未确认成功"
                "（launch() 会在 resume 前再核验一次，关不掉就 fail-closed，不会两开）",
            )
    store.append_event(task_id, f"续班 → {successor_id}（第 {shift + 1} 班）{note}")
    return [f"{task_id} 续班 → {successor_id}（第 {shift + 1} 班）{note}"]


def _check_exited_chain(
    task: dict, status: dict, config: dict, now: datetime
) -> list[str]:
    """exited 也评估一次换班（会话被关/崩了但交接已写完的情形）：
    只认交接文件——有交接先打存档点再按 NEXT 判，没交接不动。

    F4：chain_checked=True 之后的所有逻辑包了一层 try/except，评估中途
    异常转 needs_attention 并留人话（见 `_chain_eval_failed`），不再悄悄
    停在 exited 没有任何记录。
    """
    if status.get("chain_checked"):
        return []
    store.update_status(task["id"], chain_checked=True)
    try:
        text = _read_handover(_handover_file(task, status))
        if text is None:
            return []
        blocked = _checkpoint_shift(task, status, config, now)
        if blocked:
            return blocked
        return _handover_verdict(task, status, text, config, now)
    except Exception as exc:
        return _chain_eval_failed(task, config, now, exc)


# ---------- S5②：收工存档点与完工分流 ----------


def _checkpoint_shift(
    task: dict, status: dict, config: dict, now: datetime
) -> list[str] | None:
    """收工边界的存档点（幂等：checkpoint_done 锁住，重复 tick 不再打）。

    - 老式任务（worktree=false）原样跳过，一期路径一字不变；
    - worktree=true 但没登记树是元数据损坏：needs_attention，不能假装收工；
    - 有改动 → commit 并把完整 sha 写本班 checkpoint_sha；无改动 → 只落
      checkpoint_done 并记"无改动，未打存档点"；
    - add / commit 失败 → 本班 needs_attention，止住收工流程（不判 NEXT、
      不造后继、不合并、不把失败伪装成 finished）。
    """
    task_id = task["id"]
    if not worktree.wants_worktree(task) or status.get("checkpoint_done"):
        return None
    wt = status.get("worktree_path")
    if not wt:
        reason = "工作树任务没有登记 worktree_path，不能打存档点或收工"
        store.update_status(
            task_id, state="needs_attention", error=reason,
            last_event_at=to_iso(now),
        )
        store.append_event(task_id, f"存档点失败：{reason}")
        launcher.open_notice_window(task, "(需要人工)", [reason], config)
        return [f"{task_id} 工作树元数据缺失 → needs_attention"]
    project_path = (config.get("projects") or {}).get(task.get("project"))
    if not project_path:
        reason = f"项目 {task.get('project')} 已不在 config.projects，不能核验工作树或收工"
        store.update_status(
            task_id, state="needs_attention", error=reason,
            last_event_at=to_iso(now),
        )
        store.append_event(task_id, f"存档点失败：{reason}")
        launcher.open_notice_window(task, "(需要人工)", [reason], config)
        return [f"{task_id} 项目配置缺失 → needs_attention"]
    identity_error = worktree.check_task_tree(task, project_path, status)
    if identity_error:
        reason = f"存档点前工作树核验失败：{identity_error}"
        store.update_status(
            task_id, state="needs_attention", error=reason,
            last_event_at=to_iso(now),
        )
        store.append_event(task_id, reason)
        launcher.open_notice_window(task, "(需要人工)", [reason], config)
        return [f"{task_id} 工作树核验失败 → needs_attention"]
    try:
        sha = worktree.checkpoint(task, wt)
    except worktree.WorktreeError as exc:
        store.update_status(
            task_id, state="needs_attention", error=f"存档点失败：{exc}",
            last_event_at=to_iso(now),
        )
        store.append_event(task_id, f"存档点失败：{exc}")
        launcher.open_notice_window(
            task, "(需要人工)",
            [
                f"收工存档点失败：{exc}",
                "工作树和分支都保留着；处理完可在卡片上合并/丢弃，这班不再自动续",
            ],
            config,
        )
        return [f"{task_id} 存档点失败 → needs_attention"]
    shift = int(task.get("shift") or 1)
    if sha:
        store.update_status(task_id, checkpoint_sha=sha, checkpoint_done=True)
        store.append_event(task_id, f"已打存档点 {sha[:12]}（第 {shift} 班）")
    else:
        store.update_status(task_id, checkpoint_done=True)
        store.append_event(task_id, f"第 {shift} 班无改动，未打存档点")
    return None


def _finalize_done(
    task: dict, config: dict, now: datetime, *, skip_review: bool = False
) -> list[str]:
    """最后一班完工分流（判过 NEXT: done 或没交接正常干完都走这里）：
    - worktree=false：原状态机一字不变 → finished；
    - S7：build 角色 + review.enabled=true（且未 skip_review）→ 不直接
      分流，起同 round 审稿（_start_review_round），真正的 merge_policy
      分流要等审稿 done 之后由 _review_done 再次调用本函数（那时角色已是
      review，不会再折回 _start_review_round，见下面的角色判断）；
    - S7④：skip_review=True（网页"跳过审稿"控制 API）明确跳过起审稿，直接
      按 merge_policy 分流——只有调用方已经核验过"build 已 checkpoint、
      尚无 done verdict"这个边界才该传 True；
    - true + manual：存档后 → awaiting_merge，树与分支保留，等工头；
    - true + auto：调度器合并 → merged / needs_attention（原因见红字）。
    """
    task_id = task["id"]
    if not worktree.wants_worktree(task):
        store.update_status(task_id, state="finished", last_event_at=to_iso(now))
        store.append_event(task_id, "收工：worktree=false 走一期路径 → finished")
        return [f"{task_id} 正常干完 → finished"]
    review_cfg = store.review_config(task, config)
    if review_cfg.get("enabled") and store.role_of(task) == "build" and not skip_review:
        return _start_review_round(task, config, now)
    policy = review_cfg.get("merge_policy") or "manual"
    if policy == "manual":
        store.update_status(
            task_id, state="awaiting_merge", last_event_at=to_iso(now)
        )
        store.append_event(
            task_id, "最后一班已存档 → awaiting_merge（等工头合并/丢弃）"
        )
        return [f"{task_id} 干完 → awaiting_merge（manual）"]
    project_path = (config.get("projects") or {}).get(task.get("project"))
    if not project_path:
        reason = f"项目 {task.get('project')} 已不在 config.projects，不能自动合并"
        store.update_status(
            task_id, state="needs_attention", error=reason,
            last_event_at=to_iso(now),
        )
        launcher.open_notice_window(task, "(需要人工)", [reason], config)
        return [f"{task_id} 干完但自动合并没成：{reason}"]
    ok, note = worktree.merge_task(
        task, project_path,
        store.read_status(task_id), config,
        close_windows=lambda ids: launcher.close_windows(ids, config),
    )
    if ok:
        return [f"{task_id} 干完并自动合并：{note}"]
    latest = store.read_status(task_id)
    if not latest.get("merge_attention_noted"):
        store.update_status(task_id, merge_attention_noted=True)
        launcher.open_notice_window(
            task, "(需要人工)",
            [f"自动合并没有完成：{note}", "工作树与分支都保留着，请在网页处理"],
            config,
        )
    return [f"{task_id} 干完但自动合并没成：{note}"]


# ---------- S7：审稿流水线（build ↔ review 轮转、held、返工轮数、我来看） ----------

DEFAULT_REVIEW_STOP_BUILD_TEXT = (
    "来自nightshift：审稿已经通过（NEXT: done），这条流水线收尾了，请停下不要再继续动代码。"
)


def send_review_control(
    task_id: str, window_id: str, text: str, *, kind: str,
    pre_send_fields: dict, success_only_fields: dict | None = None,
    failure_note: str = "",
) -> bool:
    """review 控制/恢复消息的统一投递。

    S7.2 阻断五.2/5.3：keepalive/hold/resume 三处以前都是"先落状态字段
    （awaiting_verdict/control_kind/计数器），再 send-keys"——send 失败时
    状态已经被污染（keepalive 计数虚增却什么都没发出去；resume 把
    awaiting_verdict 提前置 True，没回滚，导致下一次任何控制回复都可能被
    误解析成正式 verdict）。S7.2 把顺序倒转成"send 成功才落状态"，但这
    还不够。

    S7.3 阻断二：真实世界里 send-keys 这个系统调用"返回"和"目标会话真的
    处理完、已经开始新一轮直到发出 Stop"这两件事之间没有硬先后保证（测试
    环境里更是可以让假 send_keys 同步触发 Stop 回调）——如果 Stop 抢在
    我们落盘 `review_awaiting_verdict=False`（或 resume 场景的 True）
    之前到达，Stop 看到的仍是旧值，会把这次控制/恢复回复解析错。

    改法：`pre_send_fields`——那些"决定下一次 Stop 该怎么解释"的字段
    （控制 turn 是 awaiting_verdict=False+control_kind=kind；resume 是
    awaiting_verdict=True）——必须在 send-keys **之前**原子落盘，堵住这个
    竞态窗口。send 失败时精确回滚到 send 之前的值。`success_only_fields`
    （计数器/时间戳这类不参与"下一次 Stop 怎么解释"判断的字段）仍然只在
    send 确认成功后才落盘，且要用 `delivery_id` 核验这次投递还没被 hook
    抢先消费——`_handle_review_stop` 的 `do_claim` 处理任何 Stop 时都会
    清掉 `review_control_delivery`，如果我们发现它已经不是自己那份
    delivery_id，说明 hook 已经先一步做出了判断（可能已经把 state 从
    held 推进到 idle 了），这时候不能再用 `success_only_fields`（比如
    resume 的 `state="working"`）把 hook 已经推进的结果覆盖回去。
    """
    prior = store.read_status(task_id)
    prior_values = {k: prior.get(k) for k in pre_send_fields}
    delivery_id = uuid.uuid4().hex
    store.update_status(task_id, review_control_delivery=delivery_id, **pre_send_fields)
    proc = launcher.send_keys(str(window_id), text)
    if proc.returncode != 0:
        def rollback(status: dict) -> None:
            if status.get("review_control_delivery") != delivery_id:
                return  # 已经被后来者/hook 动过，不要把不再是"我们的"状态回滚回去
            for key, value in prior_values.items():
                if value is None:
                    status.pop(key, None)
                else:
                    status[key] = value
            status.pop("review_control_delivery", None)

        store.modify_status(task_id, rollback)
        store.append_event(
            task_id,
            f"{kind} 控制消息投递失败（returncode={proc.returncode}）{failure_note}",
        )
        return False

    def finalize(status: dict) -> None:
        if status.get("review_control_delivery") != delivery_id:
            return  # hook 已经先消费掉了，success_only_fields 不再适用
        status.pop("review_control_delivery", None)
        status.update(success_only_fields or {})

    store.modify_status(task_id, finalize)
    return True


def _coordinator_id(task: dict) -> str:
    return store.pipeline_id_of(task)


def _coordinator_status(task: dict) -> dict:
    """流水线协调字段（fix_count/hold_requested/pipeline_phase/…）统一记在
    pipeline_id 对应的根任务 status 上，跨 task 更新前先定位这唯一
    coordinator（设计稿"先确定唯一 coordinator，通过一个 flock 内的原子
    append/update helper"——store.update_status 已经是这样一个 helper）。
    """
    return store.read_status(_coordinator_id(task))


def _update_coordinator(task: dict, **fields) -> dict:
    return store.update_status(_coordinator_id(task), **fields)


def _current_build(task: dict) -> tuple[dict, dict] | None:
    """这条流水线里，状态为 held 且 role=build 的那一个任务。

    S7.1 阻断三：以前到处用 `review_task.get("parent_id")` 猜"当前 build
    是谁"——这只在 review 是这一轮第一个审稿任务时恰好正确；pending 的
    release 分支新建 review#2 后，review#2.parent_id 是 review#1，不是
    build，`_review_done`/`_review_fix` 沿它找会摸到错的任务。改成按
    pipeline_id + role=build + state=held 直接找，不依赖链上是第几步。

    S5 起同一 pipeline 同一时刻理论上只应该有一个 build 处于 held（阻断四
    的同 pipeline 互斥要到 S7.1③ 才真正强制），这里按 list_tasks() 的
    run_at 升序取第一个匹配，找不到返回 None（build 窗口已经不在了/被
    release 关掉了）。
    """
    pid = _coordinator_id(task)
    for item in store.list_tasks():
        t = item["task"]
        if store.pipeline_id_of(t) != pid or store.role_of(t) != "build":
            continue
        if item["status"].get("state") == "held":
            return t, item["status"]
    return None


def _hold_blocks(task: dict, config: dict, now: datetime, *, reason: str) -> list[str] | None:
    """流水线协调状态若 hold_requested：这一班转 held 并停在这里，不再往下
    走（不起审稿、不返工、不合并）；否则返回 None 交回调用方继续正常流程。
    "我来看"不按 Esc、不打断正在进行的工具调用——这里只在自然的决策点
    （起审稿前/返工前/合并前）拦截，不会打断任何工具调用。
    """
    coordinator = _coordinator_status(task)
    if not coordinator.get("hold_requested"):
        return None
    task_id = task["id"]
    store.update_status(
        task_id, state="held", held_since=to_iso(now), held_reason=reason,
        last_event_at=to_iso(now),
    )
    _update_coordinator(task, pipeline_phase="held")
    store.append_event(task_id, f"我来看：{reason}，停在这里等工头")
    return [f"{task_id} 我来看 → held（{reason}）"]


def _previous_review_text(task: dict, round_: int) -> str:
    """这条流水线上一轮（round_ - 1）审稿意见的原文；round_ <= 1 或找不到
    就返回空串（模板里 {previous_review} 天然是可选前缀）。"""
    if round_ <= 1:
        return ""
    pid = _coordinator_id(task)
    for item in store.list_tasks():
        other = item["task"]
        if store.pipeline_id_of(other) != pid:
            continue
        if store.role_of(other) != "review" or store.round_of(other) != round_ - 1:
            continue
        review_file = (item["status"] or {}).get("review_file")
        if review_file and Path(review_file).is_file():
            return Path(review_file).read_text(encoding="utf-8", errors="replace")
    return ""


def _apply_review_no_quota_policy(review_task: dict, config: dict, now: datetime) -> None:
    """审稿方额度不足、还没起跑就被预检拦下（_try_launch 的额度关卡）：按
    review.on_no_quota 处理挂着的 build 父班——release 直接关它的窗口（工作
    树上的改动已经打过存档点，不需要会话继续开着烧保活）；hold 什么都不
    做，build 继续 held、按其 runner 的保活间隔戳。只在第一次因这个原因
    处理父班时动手，避免每个 tick 都重复关同一个窗口。

    S7.1 阻断三：改用 `_current_build` 按 pipeline+role+held 找真正的
    build（而不是 `review_task.get("parent_id")`）——pending 的 release
    分支新建的 review#2 的 parent_id 是 review#1，不是 build，旧写法在这种
    场景下会摸到 review#1（state 早就不是 held）直接空转，on_no_quota=
    release 的关窗策略悄悄失效。
    """
    review_cfg = store.review_config(review_task, config)
    if (review_cfg.get("on_no_quota") or "release") != "release":
        return
    current = _current_build(review_task)
    if current is None:
        return
    parent_task, parent_status = current
    if parent_status.get("review_no_quota_released"):
        return
    parent_id = parent_task["id"]
    window_id = parent_status.get("window_id")
    if window_id:
        launcher.close_windows([window_id], config)
    store.update_status(parent_id, review_no_quota_released=True)
    store.append_event(
        parent_id,
        "审稿方额度不足（on_no_quota=release）：施工窗口已关闭，"
        "等审稿额度刷新后另起审稿",
    )


def _start_review_round(task: dict, config: dict, now: datetime) -> list[str]:
    """build 班收工（NEXT: done）且这条流水线开着审稿：起同 round 的审稿
    班，build 班转 held 保活等结果。"""
    task_id = task["id"]
    blocked = _hold_blocks(task, config, now, reason="收工后本该起审稿，但工头要来看")
    if blocked is not None:
        return blocked
    round_ = store.round_of(task)
    status = store.read_status(task_id)
    wt = status.get("worktree_path")
    base_ref = status.get("base_ref")
    if not wt or not base_ref:
        reason = "审稿流水线要求工作树元数据（worktree_path/base_ref），但这一班缺失"
        store.update_status(
            task_id, state="needs_attention", error=reason, last_event_at=to_iso(now)
        )
        store.append_event(task_id, reason)
        launcher.open_notice_window(task, "(需要人工)", [reason], config)
        return [f"{task_id} 审稿流水线元数据缺失 → needs_attention"]

    handover_text = _read_handover(_handover_file(task, status))
    prompt = store.render_review_prompt(
        config, task,
        workdir=str(wt),
        base_ref=str(base_ref),
        diff_command=f"git -C {wt} diff {base_ref}..HEAD",
        build_handover=handover_text,
        previous_review=_previous_review_text(task, round_),
        round_=round_,
    )
    review_id = store.create_cross_role_successor(
        task, config, role="review", round_=round_,
        prompt_final=prompt, parent_next_state="held",
    )
    store.update_status(
        task_id, held_since=to_iso(now),
        held_reason=f"等待第 {round_} 轮审稿", last_event_at=to_iso(now),
    )
    _update_coordinator(task, pipeline_phase="reviewing")
    store.append_event(task_id, f"收工，进入第 {round_} 轮审稿 → {review_id}")
    return [f"{task_id} 起第 {round_} 轮审稿 → {review_id}"]


def _check_review_idle(
    task: dict, status: dict, config: dict, now: datetime
) -> list[str] | None:
    """review 角色的 idle 分流：读 hook 落好的 review_verdict，按
    done/fix/pending 三态分流；返回 None 交回调用方按普通流程走（verdict
    还没落盘，或这一轮已经处理过）。"""
    task_id = task["id"]
    round_ = store.round_of(task)
    verdict = status.get("review_verdict")
    if verdict is None:
        return None  # hook 还没落 verdict，等下一 tick

    if verdict == "fix":
        coordinator = _coordinator_status(task)
        review_cfg = store.review_config(task, config)
        max_rounds = int(review_cfg.get("max_rounds") or 5)
        fix_count = int(coordinator.get("fix_count") or 0)
        if fix_count >= max_rounds and not coordinator.get("round_limit_override"):
            if status.get("round_limit_noted_round") != round_:
                reason = f"返工轮数已到线（{fix_count}/{max_rounds}），继续需要工头确认"
                store.update_status(
                    task_id, state="needs_attention", error=reason,
                    round_limit_noted_round=round_, last_event_at=to_iso(now),
                )
                _update_coordinator(task, pipeline_phase="round_limit")
                store.append_event(task_id, f"{reason} → needs_attention")
                launcher.open_notice_window(
                    task, "(需要人工)",
                    [reason, "网页上点“继续”可以再放一轮，不会自动无限返工"],
                    config,
                )
                return [f"{task_id} 返工轮数到线 → needs_attention"]
            return []  # 已经告过警，安静等工头点"继续"

    # F5：pending 也要受"我来看"拦——之前只拦 done/fix，pending 会绕过
    # hold_requested 直接往下走 release/hold 分支，工头"我来看"落空。
    blocked = _hold_blocks(
        task, config, now,
        reason="审稿已给出结果（{}），但工头要来看".format(verdict),
    )
    if blocked is not None:
        return blocked

    if status.get("review_routed_round") == round_:
        return []  # 这一轮已经处理过（幂等）
    store.update_status(task_id, review_routed_round=round_)

    if verdict == "done":
        return _review_done(task, config, now)
    if verdict == "pending":
        return _review_pending(task, config, now)
    actions, _ok = _review_fix(task, config, now)  # fix，或 hook 已经归一过的非法值
    return actions


def _review_done(review_task: dict, config: dict, now: datetime) -> list[str]:
    """审稿通过：叫停仍 held 着的 build 会话（若还活着），复用
    `_finalize_done` 走 merge_policy 分流（manual → awaiting_merge；
    auto → 唯一 merge helper）。

    S7.1 阻断三：改用 `_current_build` 找真正 held 的 build（而不是
    `review_task.get("parent_id")`）——review 是 pending release 出来的
    第 2、3…个审稿任务时，parent_id 指向上一个 review，不是 build，旧写法
    会摸错任务、build 永久 held 泄漏窗口/保活。

    S7.1 阻断六：叫停 build 的 send-keys 若失败，不能假装它已经停了——build
    标 needs_attention（而不是 chained），让人工确认那扇仍可能在跑的窗口。

    S7.2 阻断七：停工失败时以前 build 虽然标了 needs_attention，但函数末尾
    仍无条件调用 `_finalize_done`——auto 策略会继续 merge/清树，跟那扇
    "可能还在跑"的施工窗口打架（它要是真还在写文件，工作树在合并/清理时
    被改动就是数据损坏）。改成停工失败时 review 自己也转
    needs_attention、**不调用** `_finalize_done`，直接 return，把收尾
    整个暂停下来等人工确认。"停工成功"的定义就是"send-keys 返回码为
    0"——不引入等 SessionEnd/窗口消失确认的新机制（那是更大的架构改动，
    这次范围内不做，明确写在这里：这是选择，不是遗漏）。只有确认成功
    （或者本来就没有活着的 build 需要停）才继续走 `_finalize_done`。
    """
    task_id = review_task["id"]
    current = _current_build(review_task)
    if current is not None:
        parent_task, parent_status = current
        parent_id = parent_task["id"]
        window_id = parent_status.get("window_id")
        stop_failed = False
        window_alive = bool(window_id) and launcher.window_alive(str(window_id), config)
        if window_alive:
            text = config.get("review_stop_build_text") or DEFAULT_REVIEW_STOP_BUILD_TEXT
            # F1：这句"请停下"打进 build 会话后，跟保活探针一样会触发它的
            # UserPromptSubmit/Stop——必须走 build_control_kind="stop" 让
            # hook.py 认出这是控制 turn，state 保持 send 之前的值（held），
            # 由这里 success_only_fields 一次性落成 chained，不能被 build
            # 回一句话之后 hook 自己算出的 idle 覆盖掉（Fable 审查 N1，9/1）。
            sent = send_review_control(
                parent_id, str(window_id), text, kind="stop",
                pre_send_fields={"build_control_kind": "stop"},
                success_only_fields={
                    "state": "chained", "successor_id": task_id,
                    "last_event_at": to_iso(now),
                },
                failure_note="，未能让施工班停下",
            )
            if not sent:
                # S7.1 阻断六：send-keys 失败不能假装 build 已经停了——它可能
                # 还在跑，这时候把它标 chained 会让人以为可以放心合并/删树。
                stop_failed = True
            else:
                store.append_event(parent_id, "审稿已通过（NEXT: done），已敲停施工班")
        if stop_failed:
            build_reason = "审稿已通过，但没能让仍在跑的施工窗口停下（send-keys 失败），请手动确认/关闭"
            store.update_status(
                parent_id, state="needs_attention", error=build_reason, last_event_at=to_iso(now)
            )
            store.append_event(parent_id, build_reason)
            launcher.open_notice_window(parent_task, "(需要人工)", [build_reason], config)
            # S7.2 阻断七：不能继续 finalize——那扇窗口可能还在写代码，
            # auto 策略的自动合并/清树会跟它打架。review 自己也转
            # needs_attention，整条流水线的收工暂停在这里等人工确认。
            review_reason = "审稿已通过，但没能让仍在跑的施工窗口停下，暂缓自动收尾直到人工确认"
            store.update_status(
                task_id, state="needs_attention", error=review_reason, last_event_at=to_iso(now)
            )
            _update_coordinator(review_task, pipeline_phase="stop_failed")
            store.append_event(task_id, review_reason)
            launcher.open_notice_window(review_task, "(需要人工)", [review_reason], config)
            return [f"{task_id} 审稿通过但停工未确认成功 → 暂缓收尾，需人工确认"]
        if not window_alive:
            # 窗口本来就不在了，没有 send-keys 这一步、也就没有
            # success_only_fields 帮忙落 chained，原样直接写。
            store.update_status(
                parent_id, state="chained", successor_id=task_id, last_event_at=to_iso(now)
            )
    _update_coordinator(review_task, pipeline_phase="done")
    store.append_event(task_id, "审稿通过（NEXT: done）")
    return _finalize_done(review_task, config, now)


def _review_hold_resume_eta(review_task: dict, now: datetime) -> str:
    """review 角色 hold（额度到线）自动恢复的时间估计。

    S7.1 阻断二/三：以前 `_review_pending` 的 hold 分支只转 held、什么都
    不落，没有任何机制能把它带回来（`_check_running` 通用的
    quota_paused_until 恢复只认 idle/waiting_wakeup，held 状态永远等不到）。

    优先复用 hook.py `_quota_check` 撞五小时线时已经落盘的
    `quota_paused_until`（那是按真实 API 回报的刷新时间算的，最准）；
    没有（多半是撞的周线，没有精确到分钟的刷新时间）就现查一次这一班
    runner 的缓存用量，按周线 resets 估一个时间；都查不到就退回一个固定
    的兜底间隔——不确定但好过永远卡住，`_check_running` 的恢复分支到点
    发现还是没刷新时，review 大概率还会再给一次 pending，届时会重新走这里
    算一次新的估计（自我修正，不是死锁在一个错误估计上）。
    """
    status = store.read_status(review_task["id"])
    existing = status.get("quota_paused_until")
    if existing:
        return existing
    runner = store.effective_runner(review_task)
    usage = (quota.load_quota_file().get(runner) or {}).get("usage")
    resets_in = None
    if isinstance(usage, dict):
        resets_in = quota.resets_in_minutes(usage.get("week_all_resets"))
    if resets_in is None:
        resets_in = 60  # 查不到就按 1 小时后重试一次
    return to_iso(now + timedelta(minutes=resets_in))


def _review_pending(review_task: dict, config: dict, now: datetime) -> list[str]:
    """审稿额度到线、意见没写完（NEXT: pending，不计轮数）：按
    review.on_no_quota release/hold 处理这一轮——release 另起同轮审稿等
    额度刷新；hold 转 held 保活等刷新。不把半截意见当 fix 让 build 盲改。"""
    task_id = review_task["id"]
    round_ = store.round_of(review_task)
    review_cfg = store.review_config(review_task, config)
    on_no_quota = review_cfg.get("on_no_quota") or "release"
    store.append_event(task_id, f"审稿第 {round_} 轮 pending（{on_no_quota}）")
    if on_no_quota == "hold":
        # S7.1 阻断二/三：额外落 quota_paused_until，配合
        # _check_running 新增的 review-hold 恢复分支，不再永久卡住。
        # F2（Fable 审查 A2，9/1）：pending 不是终态，这一轮之后还会再来一份
        # 真正的 done/fix——路由幂等标记 review_routed_round 在这里就清掉
        # （state 已是 held，_check_review_idle 只在 idle 时进入，不会重复
        # 路由这份 pending）。不能等到恢复分支的 success_only_fields 再清：
        # 那一步在 Stop 抢先消费掉 delivery 时不会落盘，"我来看→继续"把它
        # 拨回 idle 的路径也不经过恢复分支，两种情况下新 verdict 都会被
        # 顶部的幂等判断当"本轮已处理"吞掉，review 永远 idle、build 永远 held。
        store.update_status(
            task_id, state="held", held_since=to_iso(now),
            held_reason="审稿额度到线，等刷新后继续", last_event_at=to_iso(now),
            quota_paused_until=_review_hold_resume_eta(review_task, now),
            quota_resume_sent=False, review_routed_round=None,
        )
        return [f"{task_id} 审稿 pending → held"]

    status = store.read_status(task_id)
    # S7.5 阻断：review 继承同一棵工作树，元数据缺失时 fail-closed 到
    # needs_attention，不静默退回主项目目录（同 _start_review_round 的口径）。
    wt = status.get("worktree_path")
    if not wt:
        reason = "审稿流水线要求工作树元数据（worktree_path），但这一班缺失"
        store.update_status(
            task_id, state="needs_attention", error=reason, last_event_at=to_iso(now)
        )
        store.append_event(task_id, reason)
        launcher.open_notice_window(review_task, "(需要人工)", [reason], config)
        return [f"{task_id} 审稿流水线元数据缺失 → needs_attention"]
    base_ref = status.get("base_ref") or ""
    # S7.1 阻断三：pending 之前这一轮如果已经写了半截意见（review_file 已
    # 落盘），传给续班看，不能让已经审过的内容白白丢掉。
    partial_review = ""
    partial_path = status.get("review_file")
    if partial_path and Path(partial_path).is_file():
        partial_text = Path(partial_path).read_text(encoding="utf-8", errors="replace")
        if partial_text.strip():
            partial_review = "（上一次这轮审到一半，接着看）\n\n" + partial_text
    prompt = store.render_review_prompt(
        config, review_task, workdir=str(wt), base_ref=str(base_ref),
        diff_command=f"git -C {wt} diff {base_ref}..HEAD",
        build_handover=None, previous_review=partial_review, round_=round_,
    )
    new_review_id = store.create_cross_role_successor(
        review_task, config, role="review", round_=round_,
        prompt_final=prompt, parent_next_state="chained",
    )
    store.append_event(task_id, f"按 on_no_quota=release 另起第 {round_} 轮审稿 {new_review_id}")
    return [f"{task_id} 审稿 pending → 另起 {new_review_id}"]


def _reconcile_stale_fix_intent(task: dict, config: dict, now: datetime) -> list[str] | None:
    """崩溃恢复：coordinator 上如果还留着非空 `pending_fix_intent`，说明上
    一次 `_review_fix` 的"原地唤醒 held build"分支在 send-keys 成功之后、
    五步正式提交（领 shift/写 build task.json/写 build status/写 review
    status/写 coordinator）走完之前，进程被打断了。

    S7.2 阻断二：消息可能已经真的送达 build 会话——不能假装没发生过去
    重新走一遍正常流程，也不能自动重发（会造成双份返工提示，同一条意见
    在窗口里出现两次）。一律 fail-closed 到 needs_attention，把 intent
    原样留在 coordinator 上当审计证据，人工核对 build 实际进度（看它的
    tmux 面板/round 是否已经推进）后手动清理再继续。

    协调者自查补充：build 与 review 是同一条 pipeline 里两个独立的任务，
    都会各自调用到这个函数（只要它们各自处于活跃状态、被 `_check_running`
    巡检到）。"要不要开新的提醒窗口"（全局只应该做一次，用
    `pending_fix_intent_noted` 控制）与"要不要把**这一个**任务自己标成
    needs_attention"（应该对每一个被巡检到、受影响的任务都做，天然幂等、
    不产生新副作用）是两件不同的事——早期版本把两者绑在一起，导致同一
    tick/后续 tick 里第二个被处理到的任务（先到的那个已经把
    `pending_fix_intent_noted` 置位）直接安静跳过、自己的 state 从头到尾
    没被设过 needs_attention，操作者盯着这一个任务的卡片完全看不出异常。
    改成分开判断：状态字段每次都按需补齐（已经是 needs_attention 且原因
    一致就不重复写），提醒窗口仍然只开一次。
    """
    coordinator = _coordinator_status(task)
    intent = coordinator.get("pending_fix_intent")
    if not intent:
        return None
    task_id = task["id"]
    reason = (
        f"上一次返工投递在写盘中途被打断（{intent!r}），消息可能已经送达施工"
        "会话，不自动重发——请人工核对 build 实际进度（tmux 面板/round 是否"
        "已推进），确认后手动清掉这份 pending_fix_intent 再继续"
    )
    already_flagged = (
        task_status := store.read_status(task_id)
    ).get("state") == "needs_attention" and task_status.get("error") == reason
    if not already_flagged:
        store.update_status(
            task_id, state="needs_attention", error=reason, last_event_at=to_iso(now)
        )
        store.append_event(task_id, reason)
    if not coordinator.get("pending_fix_intent_noted"):
        launcher.open_notice_window(task, "(需要人工)", [reason], config)
        _update_coordinator(task, pending_fix_intent_noted=True)
    return [f"{task_id} 上一次返工投递中断，未收口 → needs_attention（不自动重发）"]


def _review_fix(review_task: dict, config: dict, now: datetime) -> tuple[list[str], bool]:
    """审稿 NEXT: fix：记一次 fix_count，起下一轮 build 返工——若合格 held
    build 会话仍活着，直接 send-keys 完整意见继续（不新开窗口）；否则造
    新的 build 班。两条路径都留下不覆盖的 round/checkpoint 审计。

    S7.1 阻断六：返回值从 `list[str]` 扩成 `(actions, ok)`——`ok=False`
    专指"原地捎话失败、这一轮返工没能真正投递出去"（此时这一班转
    needs_attention，没有产生下一轮 build）；`ok=True` 覆盖"捎话成功"与
    "新起了一个 build 班"两种正常完成路径。`_api_pipeline_fix_now` 靠这个
    判断该回 200 还是 409，不能再假设调用完就是成功。

    S7.1 阻断一：原地唤醒 held build 这条路径改成两阶段提交——send-keys
    之前只落一个可丢弃的 `pending_fix_intent` 标记，不动 fix_count/build
    的 round/shift/checkpoint 字段；send-keys 成功后才一次性提交这些正式
    字段。失败时只清掉意图标记，双方状态与调用前完全一致，可以安全重试
    （旧写法先改 fix_count/build.round 再 send-keys，失败后 round=2、
    fix_count=1 但 build 其余字段停在第 1 轮，成了没法安全重试的半吊子
    状态）。

    S7.1 阻断一：build 被复用时也要从 `store.next_pipeline_shift` 领一个
    新的全局单调 shift（不再只改 round）——否则 `chain_state()` 的
    max-shift 扫描永远扫不到被复用的这一班，会长期停留在旧 review 的
    状态上。

    S7.1 阻断一：不再把 `review.successor_id` 设成 build_id——build 早在
    `_start_review_round` 起审稿时就把 successor_id 指向了这个 review，
    反过来再让 review.successor_id 指回 build 会形成一个两步环。原地唤醒
    改记 `reactivated_task_id`（review 记：我唤醒了哪个 build），build 侧
    对应记 `reactivated_from_review_id`，两个字段都只读、不参与任何
    successor 链遍历，不会成环。

    S7.1 阻断三：改用 `_current_build` 找真正 held 的 build（不再用
    `review_task.get("parent_id")`）。
    """
    task_id = review_task["id"]
    round_ = store.round_of(review_task)
    next_round = round_ + 1
    coordinator = _coordinator_status(review_task)
    fix_count = int(coordinator.get("fix_count") or 0) + 1

    review_status = store.read_status(task_id)
    review_file = review_status.get("review_file")
    review_text = ""
    if review_file and Path(review_file).is_file():
        review_text = Path(review_file).read_text(encoding="utf-8", errors="replace")

    current = _current_build(review_task)
    if current is not None:
        parent_task, parent_status = current
        parent_id = parent_task["id"]
        window_id = parent_status.get("window_id")
        if window_id and launcher.window_alive(str(window_id), config):
            # 阶段一：只落一个可丢弃的意图标记，不动任何正式状态字段。
            _update_coordinator(
                review_task,
                pending_fix_intent={
                    "build_id": parent_id, "review_id": task_id, "next_round": next_round,
                },
            )
            # S7.5 阻断：workdir 用 build 自己登记的工作树（parent_status），
            # 不是 config 主目录——返工班永远在工作树里干活。
            fix_text = store.render_review_fix_prompt(
                config, parent_task,
                workdir=str(parent_status.get("worktree_path") or ""),
                round_=next_round, review_text=review_text,
            )
            proc = launcher.send_keys(str(window_id), fix_text)
            if proc.returncode != 0:
                # 失败：只清意图标记，round/fix_count/checkpoint 全部保持
                # 调用前原样，build 仍 held，下一次可以安全重试。
                _update_coordinator(review_task, pending_fix_intent=None)
                reason = "审稿退回，但把返工意见敲进施工窗口失败（send-keys 失败）"
                store.update_status(
                    task_id, state="needs_attention", error=reason, last_event_at=to_iso(now)
                )
                store.append_event(task_id, reason)
                launcher.open_notice_window(review_task, "(需要人工)", [reason], config)
                return [f"{task_id} 返工投递失败 → needs_attention"], False
            # 阶段二：成功，一次性提交所有正式字段。
            # 同一个 build task id 复用进下一轮：checkpoint 相关字段要归零才能
            # 让 _checkpoint_shift/_check_idle_chain 重新跑一遍；归零前先把上一
            # 轮的存档点归档进历史，不能覆盖丢失审计线索（不许复用 task id 后
            # 悄悄盖掉上一轮的 checkpoint_sha）。
            history = list(parent_status.get("checkpoint_history") or [])
            if parent_status.get("checkpoint_sha"):
                history.append({"round": round_, "sha": parent_status["checkpoint_sha"]})
            new_shift = store.next_pipeline_shift(review_task)
            parent_task["round"] = next_round
            parent_task["shift"] = new_shift
            store.atomic_write_json(store.task_dir(parent_id) / "task.json", parent_task)
            store.update_status(
                parent_id, state="working", round=next_round, shift=new_shift,
                last_event_at=to_iso(now), chain_checked=False, checkpoint_done=False,
                checkpoint_sha=None, checkpoint_history=history,
                reactivated_from_review_id=task_id,
                # S7.2 阻断三：显式清掉上一轮的运行期收尾标记——update_status
                # 是合并语义，不传等于不清除。handover_path 一旦被上一轮的
                # 提醒逻辑写过就会一直覆盖 _handover_file() 按 shift 计算的
                # 默认路径；不清掉的话，新一轮如果只发生一次普通 Stop、还没
                # 来得及写新交接文件，调度器会重新读到上一轮那份写着
                # NEXT:done 的旧交接，把中间停顿误判成这一轮已经收工。
                handover_path=None, context_warned_at=None, quota_warned_at=None,
                context_warn_count=0, quota_warn_count=0, mode_warned=False,
                other_model_warned=[],
            )
            store.update_status(
                task_id, state="chained", reactivated_task_id=parent_id,
            )
            _update_coordinator(
                review_task, fix_count=fix_count, pipeline_phase="build",
                round_limit_override=False, pending_fix_intent=None,
            )
            store.append_event(
                parent_id, f"审稿退回（第 {round_} 轮），已捎话继续第 {next_round} 轮返工（同一会话）"
            )
            store.append_event(task_id, f"审稿退回，已捎话给仍 held 着的施工班第 {next_round} 轮")
            return [f"{task_id} 审稿 fix → 捎话继续（第 {next_round} 轮）"], True

    # S7.5 阻断：held build 已不在（窗口没了），新起返工班——workdir 用
    # review 自己继承的工作树（review 创建时从 build 拷贝而来，整条流水线
    # 只有一棵树），不是 config 主目录。
    prompt = store.render_review_fix_prompt(
        config, review_task,
        workdir=str(review_status.get("worktree_path") or ""),
        round_=next_round, review_text=review_text,
    )
    build_id = store.create_cross_role_successor(
        review_task, config, role="build", round_=next_round,
        prompt_final=prompt, parent_next_state="chained",
    )
    _update_coordinator(
        review_task, fix_count=fix_count, pipeline_phase="build",
        round_limit_override=False,
    )
    store.append_event(task_id, f"审稿退回 → 新起第 {next_round} 轮返工班 {build_id}")
    return [f"{task_id} 审稿 fix → 新起返工班 {build_id}"], True


# ---------- 额度刷新（零开销原则） ----------


def _maybe_refresh_quota(
    config: dict, now: datetime, actions: list[str], force: bool = False,
    runners: set[str] | None = None,
) -> None:
    """按需刷新——只刷调用方指定的那几家 runner，每家各自独立判断新鲜度、
    独立落盘（一家刷新失败/过期不影响另一家的好数据，见 quota.write_quota_runner）。
    """
    sch = config.get("scheduler") or {}
    refresh_after = timedelta(minutes=sch.get("quota_refresh_minutes", 30))
    for runner in runners or set():
        if not force:
            slice_ = quota.load_quota_file().get(runner) or {}
            fetched_at = slice_.get("fetched_at")
            if fetched_at:
                try:
                    if now - parse_iso(fetched_at) < refresh_after:
                        continue  # 还新鲜，不刷
                except ValueError:
                    pass  # 坏时间戳当过期处理
        _, err = _fetch_and_record_usage(runner, config, now)
        actions.append(f"已刷新 {runner} 额度" if not err else f"刷新 {runner} 额度失败：{err}")


# ---------- 启动对账（S5：孤儿工作树只提示，绝不自动删） ----------


def reconcile_worktrees(config: dict) -> list[dict]:
    """服务启动时（长期循环前一次；--once 也跑一次）对账所有项目的工作树：
    孤儿树写 orphan_worktrees.json（无孤儿也原子写 []），引用丢失的任务标
    needs_attention。返回孤儿列表给调用方留日志。"""
    result = worktree.reconcile_all(config)
    return result["orphans"]


# ---------- 常驻主循环 ----------


def _setup_logging() -> logging.Logger:
    """给 nightshift 根 logger 挂 scheduler.log（2 MB × 3 轮转）+ stderr 各一份。

    scheduler 与 http 两个子 logger 靠继承共用这套 handler，自身不配置——
    网页访问日志（nightshift.http）与调度日志（nightshift.scheduler）落同一个文件。
    """
    root = logging.getLogger("nightshift")
    if not root.handlers:
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        store.ensure_dirs()
        file_handler = logging.handlers.RotatingFileHandler(
            store.home() / "scheduler.log",
            maxBytes=2_000_000, backupCount=3, encoding="utf-8", delay=True,
        )
        file_handler.setFormatter(fmt)
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        root.addHandler(stream_handler)
    return logger


def run_forever(config: dict, max_ticks: int | None = None) -> None:
    """每 interval_seconds 一轮 tick，永不主动退出；单轮异常只记日志不许死。

    启动后第一轮 tick 就是崩溃恢复（launching 过宽限期的重试、working 而窗口
    没了的标 exited）。max_ticks 仅供测试截断循环。
    """
    logger = _setup_logging()
    interval = (config.get("scheduler") or {}).get("interval_seconds", 30)
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        try:
            # 每轮现读 config：网页改的预热时刻/间隔/模板要立刻生效（8/28 工头加的 06:02
            # 预热直到服务重启才被看见）；读坏了沿用上一份
            try:
                config = store.load_config()
            except Exception:
                logger.warning("config.json 读不了，沿用上一份")
            actions = tick(config, datetime.now(timezone.utc))
            if actions:
                logger.info("tick：%s", "；".join(actions))
        except Exception:
            logger.exception("tick 出错，下一轮继续")
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        time.sleep(interval)
