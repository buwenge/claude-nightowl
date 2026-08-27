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

__all__ = ["due", "run_warmup", "state_path", "read_state"]

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


def due(config: dict, now: datetime, state: dict | None = None) -> bool:
    """今天该预热了吗：开关开着、本地时间已过设定时刻、今天还没做过。"""
    cfg = config.get("warmup") or {}
    if not cfg.get("enabled"):
        return False
    hhmm = cfg.get("time_local") or ""
    try:
        hour, minute = (int(x) for x in hhmm.split(":"))
    except ValueError:
        return False
    tz = timezone(timedelta(hours=int(config.get("display_tz_offset_hours", 0))))
    local_now = now.astimezone(tz)
    if (local_now.hour, local_now.minute) < (hour, minute):
        return False
    state = read_state() if state is None else state
    return state.get("last_date") != local_now.strftime("%Y-%m-%d")


def run_warmup(config: dict, now: datetime, timeout: int = 120) -> dict:
    """发一句话给 probe_model（默认 haiku），把结果写进 warmup.json 并返回。"""
    tz = timezone(timedelta(hours=int(config.get("display_tz_offset_hours", 0))))
    local_now = now.astimezone(tz)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    model = (config.get("warmup") or {}).get("model") or config["probe_model"]
    cmd = [config["claude_bin"], "-p", WARMUP_PROMPT, "--model", model, "--tools", ""]
    store.ensure_dirs()
    result: dict = {
        "last_date": local_now.strftime("%Y-%m-%d"),
        "last_run_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": model,
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
