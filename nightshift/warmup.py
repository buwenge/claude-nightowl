"""预热五小时窗口：到点给 haiku 发一句话，让订阅的 5 小时窗口从这一刻开始算。

窗口是从账号当天第一条消息起算的；你 10 点开工窗口 15 点刷新，
7 点先由调度器发一句"好"，窗口就 12 点刷新——白捡几小时（借鉴 Sleep Well）。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from . import store

__all__ = ["due", "run_warmup", "state_path", "read_state", "times_of"]

WARMUP_PROMPT = "只回复一个字：好"


def state_path():
    return store.home() / "warmup.json"


def read_state() -> dict:
    p = state_path()
    if not p.is_file():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def times_of(config: dict) -> list[str]:
    """配置里的预热时刻列表（"HH:MM"），兼容旧的单个 time_local；坏格式丢掉。"""
    cfg = config.get("warmup") or {}
    raw = cfg.get("times")
    if not raw:
        raw = [cfg.get("time_local")] if cfg.get("time_local") else []
    out = []
    for t in raw:
        t = str(t or "").strip()
        try:
            hour, minute = (int(x) for x in t.split(":"))
        except ValueError:
            continue
        if 0 <= hour < 24 and 0 <= minute < 60:
            out.append(f"{hour:02d}:{minute:02d}")
    return sorted(set(out))


def due(config: dict, now: datetime, state: dict | None = None) -> list[str]:
    """现在该跑哪些预热时刻：开关开着、本地时间已过该时刻、今天这个时刻还没做过。

    总review二 G14：服务重启时若当天错过了不止一个时刻，逐个真发一句
    haiku 没有意义——窗口是从最近一次发消息起算的，只有最晚那个过点时刻
    真正决定接下来的窗口。这里只把它放进返回值，更早的几个直接标 done
    落盘（跟正常"发过了"同一个记账口径），调用方（scheduler.tick）不会
    看到、也就不会真的为它们各发一句。
    """
    cfg = config.get("warmup") or {}
    if not cfg.get("enabled"):
        return []
    tz = timezone(timedelta(hours=int(config.get("display_tz_offset_hours", 0))))
    local_now = now.astimezone(tz)
    today = local_now.strftime("%Y-%m-%d")
    # 总review二 G15（D④-3）：旧格式（last_date/time 两键，没有 done 字典）
    # 的兼容分支删掉了——生产 warmup.json 自 8/28 起就有 done 字典。
    state = read_state() if state is None else state
    done_today = set((state.get("done") or {}).get(today) or [])
    overdue = []
    for t in times_of(config):
        hour, minute = (int(x) for x in t.split(":"))
        if (local_now.hour, local_now.minute) >= (hour, minute) and t not in done_today:
            overdue.append(t)
    if len(overdue) <= 1:
        return overdue
    skip, latest = overdue[:-1], overdue[-1]
    new_state = dict(state)
    new_state["last_date"] = today
    new_state["done"] = {today: sorted(done_today | set(skip))}
    store.atomic_write_json(state_path(), new_state)
    return [latest]


def run_warmup(config: dict, now: datetime, timeout: int = 120, slot: str | None = None) -> dict:
    """发一句话给 probe_model（默认 haiku），把结果写进 warmup.json 并返回。slot 是本次对应的时刻。

    S6.1 二次返修 B3：预热只针对 Claude 的五小时窗口（Codex 没有这个概念），
    bin/probe_model 必须取 `store.runner_config(config)["claude"]` 这份权威
    视图，不能再看顶层 `config["claude_bin"]`/`config["probe_model"]`——
    `runners.claude` 一旦声明，顶层这两个键就只是过期快照。`warmup.model`
    的显式覆盖语义不变，优先级最高。
    """
    tz = timezone(timedelta(hours=int(config.get("display_tz_offset_hours", 0))))
    local_now = now.astimezone(tz)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    claude_rc = store.runner_config(config).get("claude") or {}
    model = (config.get("warmup") or {}).get("model") or claude_rc.get("probe_model")
    cmd = [claude_rc.get("bin", "claude"), "-p", WARMUP_PROMPT, "--model", model, "--tools", ""]
    store.ensure_dirs()
    today = local_now.strftime("%Y-%m-%d")
    prev = read_state()
    done = {today: list((prev.get("done") or {}).get(today) or [])}  # 只留今天的
    if slot and slot not in done[today]:
        done[today].append(slot)
    result: dict = {
        "last_date": today,
        "last_run_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": model,
        "slot": slot,
        "done": done,
    }
    if not model:
        # None 塞进 argv 会让 subprocess 抛 TypeError，tick 末尾掀翻、状态不落盘、
        # 下一轮再来一遍；配置缺失就记成一次失败的预热（该时刻当天不再试）。
        result["ok"] = False
        result["error"] = "runners.claude.probe_model（或 warmup.model）没配，预热没发"
    else:
        try:
            proc = subprocess.run(cmd, cwd=store.home(), env=env, capture_output=True, text=True, timeout=timeout)
            result["ok"] = proc.returncode == 0
            result["reply"] = (proc.stdout or "").strip()[:80]
            if proc.returncode != 0:
                result["error"] = (proc.stderr or "")[-300:]
        except (subprocess.TimeoutExpired, OSError) as exc:  # 找不到/没执行权限都算
            result["ok"] = False
            result["error"] = str(exc)[:300]
    store.atomic_write_json(state_path(), result)
    return result
