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
    """现在该跑哪些预热时刻：开关开着、本地时间已过该时刻、今天这个时刻还没做过。"""
    cfg = config.get("warmup") or {}
    if not cfg.get("enabled"):
        return []
    tz = timezone(timedelta(hours=int(config.get("display_tz_offset_hours", 0))))
    local_now = now.astimezone(tz)
    today = local_now.strftime("%Y-%m-%d")
    state = read_state() if state is None else state
    done_today = set((state.get("done") or {}).get(today) or [])
    if state.get("last_date") == today and not state.get("done"):
        done_today.add(state.get("time") or "")  # 旧格式兼容
    out = []
    for t in times_of(config):
        hour, minute = (int(x) for x in t.split(":"))
        if (local_now.hour, local_now.minute) >= (hour, minute) and t not in done_today:
            out.append(t)
    return out


def run_warmup(config: dict, now: datetime, timeout: int = 120, slot: str | None = None) -> dict:
    """发一句话给 probe_model（默认 haiku），把结果写进 warmup.json 并返回。slot 是本次对应的时刻。"""
    tz = timezone(timedelta(hours=int(config.get("display_tz_offset_hours", 0))))
    local_now = now.astimezone(tz)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    model = (config.get("warmup") or {}).get("model") or config["probe_model"]
    cmd = [config["claude_bin"], "-p", WARMUP_PROMPT, "--model", model, "--tools", ""]
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
    try:
        proc = subprocess.run(cmd, cwd=store.home(), env=env, capture_output=True, text=True, timeout=timeout)
        result["ok"] = proc.returncode == 0
        result["reply"] = (proc.stdout or "").strip()[:80]
        if proc.returncode != 0:
            result["error"] = (proc.stderr or "")[-300:]
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        result["ok"] = False
        result["error"] = str(exc)[:300]
    store.atomic_write_json(state_path(), result)
    return result
