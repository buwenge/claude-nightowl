"""调度循环：到点预检起跑、崩溃恢复、保活戳、推迟/失败窗口、常驻主循环。

时间约定：一律 UTC；所有"现在"都从参数 now（aware，UTC）进来，便于测试注入。
ISO 字符串与 datetime 互转用 parse_iso / to_iso（与 store.utc_now_iso 同格式）。

预检顺序（设计稿 §5.1，按本仓库开工令定为：信任 → 同目录 → 额度 → 起跑）。
崩溃恢复四条（设计稿 §3）：PID 复用三条件、宽限期、先落盘再碰 tmux、重试上限。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import launcher, quota, store

__all__ = ["parse_iso", "to_iso", "tick", "run_forever"]

# retry_max 的兜底值（task.json 里没写时按 3）
DEFAULT_RETRY_MAX = 3
# 视为"活跃"的状态：额度刷新看它们，同目录不并跑也拦它们
ACTIVE_STATES = ("launching", "working", "waiting_background")
# config.scheduler 缺 keepalive_text 时的兜底文案（与 config.example.json 一致）
DEFAULT_KEEPALIVE_TEXT = (
    "来自nightshift：保活探针——背景任务还在跑吗？简短回一句即可。"
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
    """
    if now.tzinfo is None:
        raise ValueError("now 必须是带时区的 aware datetime（UTC）")
    now = now.astimezone(timezone.utc)
    store.ensure_dirs()
    actions: list[str] = []
    items = store.list_tasks()
    for item in items:
        task, status = item["task"], item["status"]
        state = status.get("state")
        if state == "scheduled":
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
                actions.extend(_try_launch(task, status, config, now))
        elif state == "launching":
            actions.extend(_check_launching(task, status, config, now))
        elif state in ("working", "waiting_background", "idle"):
            actions.extend(_check_running(task, status, config, now))
        elif state == "exited":
            # S3 换班：exited 也评估一次（写完交接后会话被关/崩了的情形）
            actions.extend(_check_exited_chain(task, status, config, now))
        # 其余状态（chained/finished/…）不动

    # 每轮末尾：有活跃任务且 quota.json 缺失/过期才刷 /usage（零开销原则）
    active_ids = [
        item["task"]["id"]
        for item in items
        if store.read_status(item["task"]["id"]).get("state") in ACTIVE_STATES
    ]
    if active_ids:
        _maybe_refresh_quota(config, now, actions)
    return actions


# ---------- 起跑前预检（设计稿 §5.1） ----------


def _try_launch(task: dict, status: dict, config: dict, now: datetime) -> list[str]:
    task_id = task["id"]
    project_path = config["projects"][task["project"]]

    # a. 目录信任：没点过信任，交互式 claude 会卡在信任问答——等人也没用，直接判失败
    if not launcher.is_trusted(project_path):
        return _fail_now(
            task, config, now,
            f"目录未信任，请先手动在该目录开一次 claude：{project_path}",
        )

    # b. 同目录不并跑：开第二个窗口两边抢文件系统，纯坏事
    for other in store.list_tasks():
        if other["task"]["id"] == task_id:
            continue
        if (
            other["task"]["project"] == task["project"]
            and other["status"].get("state") in ACTIVE_STATES
        ):
            reason = f"同目录任务 {other['task']['id']} 还在跑"
            return _postpone(task, status, config, now, reason, notify=False)

    # c. 额度：查不到一律不放行（fail-closed）
    try:
        usage = quota.fetch_usage(config)
    except (quota.UsageUnavailable, quota.UsageParseError) as exc:
        return _postpone(task, status, config, now, f"额度查不到（fail-closed）：{exc}")
    # 顺手把新鲜额度落盘（quota.json 同一格式），网页/hook 都吃它
    store.atomic_write_json(
        store.home() / "quota.json", {"usage": usage, "fetched_at": to_iso(now)}
    )
    ok, reason = quota.check_guards(
        usage, task["model"], config, task.get("guards") or {}
    )
    if not ok:
        return _postpone(task, status, config, now, reason)

    # d. 全过 → 起跑（launcher.launch 自己会先落盘 launching 再碰 tmux）
    store.update_status(
        task_id,
        quota_at_launch={
            "session_pct": usage.get("session_pct"),
            "week_all_pct": usage.get("week_all_pct"),
            "per_model": usage.get("per_model") or {},
            "fetched_at": to_iso(now),
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
    give_up_at = run_at + timedelta(hours=max_postpone_hours)
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


# ---------- 运行期巡检：working / waiting_background / idle（设计稿 §5.2） ----------


def _check_running(
    task: dict, status: dict, config: dict, now: datetime
) -> list[str]:
    task_id = task["id"]

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
    if not alive:
        # 窗口没了又没等到 SessionEnd → 按退场处理
        store.update_status(
            task_id, state="exited", exit_reason="window_gone",
            last_event_at=to_iso(now),
        )
        store.append_event(task_id, "窗口不在了且没等到 SessionEnd → exited(window_gone)")
        return [f"{task_id} 窗口消失 → exited(window_gone)"]

    # R2：auto 被 CC 静默回落（如 haiku 只吃 default），无人值守会整晚卡在
    # 权限问答。开窗提醒一次就够——不改 state、不杀窗口，人来处理。
    mode = status.get("permission_mode")
    if (
        mode
        and mode not in ("auto", "bypassPermissions")
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

    # S3 换班：idle 收尾后按交接文件接下一班（每次评估先落 chain_checked
    # 防重复；评估失败的代价是这班不再自动续，好过重复开出双份后继）
    if status.get("state") == "idle" and not status.get("chain_checked"):
        return _check_idle_chain(task, status, config, now)

    if status.get("state") != "waiting_background":
        return []  # idle 永远不戳（8/27 事故的反面，设计稿 §5.2）

    guards = task.get("guards") or {}
    if not guards.get("keepalive", True):
        return []

    sch = config.get("scheduler") or {}
    idle_needed = timedelta(minutes=sch.get("keepalive_idle_minutes", 50))
    stamps = [
        parse_iso(status[key])
        for key in ("last_event_at", "last_keepalive_at")
        if status.get(key)
    ]
    if not stamps or now - max(stamps) < idle_needed:
        return []

    text = sch.get("keepalive_text") or DEFAULT_KEEPALIVE_TEXT
    launcher.send_keys(str(window_id), text)
    store.update_status(
        task_id,
        last_keepalive_at=to_iso(now),
        keepalive_count=int(status.get("keepalive_count") or 0) + 1,
    )
    store.append_event(task_id, "保活戳：waiting_background 静默超时，已 send-keys 探针")
    return [f"{task_id} 保活戳"]


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


def _check_idle_chain(
    task: dict, status: dict, config: dict, now: datetime
) -> list[str]:
    """idle 的换班评估（设计稿 §4.4 第 3 条）。

    先落 chain_checked=True 防重复评估；再看交接文件：
    - 有交接按末行 NEXT: continue/done 判（没写 NEXT 按 continue）；
    - 没交接但这班被提醒过 → 按 chain.on_no_handover（continue/stop）；
    - 没交接也从未被提醒 → 正常干完，finished。
    """
    store.update_status(task["id"], chain_checked=True)
    path = _handover_file(task, status)
    text = _read_handover(path)
    if text is not None:
        return _handover_verdict(task, text, config, now)

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
        return _chain_continue(task, config, now, handover_text=None)

    store.update_status(task["id"], state="finished", last_event_at=to_iso(now))
    store.append_event(task["id"], "idle、没留交接、也没被提醒过 → finished（正常干完）")
    return [f"{task['id']} 正常干完 → finished"]


def _handover_verdict(
    task: dict, text: str, config: dict, now: datetime
) -> list[str]:
    """有交接时的判定（idle 与 exited 共用）：末行 NEXT: done → finished；
    NEXT: continue（或没写 NEXT，按 continue）→ 续班。"""
    last = _last_nonempty_line(text)
    if _RE_NEXT_DONE.match(last):
        store.update_status(task["id"], state="finished", last_event_at=to_iso(now))
        store.append_event(task["id"], "交接末行 NEXT: done → finished")
        return [f"{task['id']} 交接 NEXT: done → finished"]
    note = ""
    if not _RE_NEXT_CONTINUE.match(last):
        note = "（交接末行没写 NEXT，按 continue）"
    return _chain_continue(task, config, now, handover_text=text, note=note)


def _chain_continue(
    task: dict, config: dict, now: datetime,
    handover_text: str | None, note: str = "",
) -> list[str]:
    """续班：班次没到上限就造后继任务（父任务转 chained）；到上限标
    chain_exhausted 并开提醒窗。后继下一 tick 走完整预检（额度不够就推迟）。"""
    task_id = task["id"]
    shift = int(task.get("shift") or 1)
    chain = task.get("chain") or {}
    max_windows = int(chain.get("max_windows") or 3)
    if shift >= max_windows:
        store.update_status(
            task_id, state="chain_exhausted", last_event_at=to_iso(now)
        )
        store.append_event(
            task_id, f"已连开 {shift} 班（上限 {max_windows}）→ chain_exhausted"
        )
        launcher.open_notice_window(
            task,
            "(班次用尽)",
            [
                f"已连开 {shift} 班（chain.max_windows={max_windows}），不再自动续班",
                "任务可能没做完；要继续可调大上限后在网页重建任务",
            ],
            config,
        )
        return [f"{task_id} 第 {shift} 班结束：班次用尽"]
    successor_id = store.create_successor(task, handover_text, config)
    store.append_event(task_id, f"续班 → {successor_id}（第 {shift + 1} 班）{note}")
    return [f"{task_id} 续班 → {successor_id}（第 {shift + 1} 班）{note}"]


def _check_exited_chain(
    task: dict, status: dict, config: dict, now: datetime
) -> list[str]:
    """exited 也评估一次换班（会话被关/崩了但交接已写完的情形）：
    只认交接文件——有交接按 NEXT 判，没交接不动。"""
    if status.get("chain_checked"):
        return []
    store.update_status(task["id"], chain_checked=True)
    text = _read_handover(_handover_file(task, status))
    if text is None:
        return []
    return _handover_verdict(task, text, config, now)


# ---------- 额度刷新（零开销原则） ----------


def _maybe_refresh_quota(
    config: dict, now: datetime, actions: list[str]
) -> None:
    sch = config.get("scheduler") or {}
    refresh_after = timedelta(minutes=sch.get("quota_refresh_minutes", 30))
    qpath = store.home() / "quota.json"
    if qpath.is_file():
        try:
            with open(qpath, encoding="utf-8") as f:
                data = json.load(f)
            if now - parse_iso(data["fetched_at"]) < refresh_after:
                return  # 还新鲜，不刷
        except (ValueError, KeyError, OSError, TypeError):
            pass  # 缺文件键/坏 JSON 当过期处理
    try:
        usage = quota.fetch_usage(config)
        payload: dict = {"usage": usage, "fetched_at": to_iso(now)}
    except (quota.UsageUnavailable, quota.UsageParseError) as exc:
        payload = {"error": str(exc), "fetched_at": to_iso(now)}
    store.atomic_write_json(qpath, payload)
    actions.append("已刷新 quota.json")


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
    return logging.getLogger("nightshift.scheduler")


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
            actions = tick(config, datetime.now(timezone.utc))
            if actions:
                logger.info("tick：%s", "；".join(actions))
        except Exception:
            logger.exception("tick 出错，下一轮继续")
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        time.sleep(interval)
