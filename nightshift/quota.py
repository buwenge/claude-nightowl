"""`claude -p "/usage"` 的调用与解析、额度门槛判定。

实测（设计稿 F1）无头 `/usage` 输出三行额度：
    Current session: 13% used · resets Aug 27, 6:40pm (UTC)
    Current week (all models): 19% used · resets Sep 2, 12pm (UTC)
    Current week (Fable): 35% used · resets Sep 2, 12pm (UTC)
其中除 all models 外的每一行是该模型的单独周线，预检必须一并认。
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

from .store import ensure_dirs, home

__all__ = [
    "resets_in_minutes",
    "UsageParseError",
    "UsageUnavailable",
    "check_guards",
    "fetch_usage",
    "parse_usage",
]


class UsageParseError(Exception):
    """/usage 输出里 session 与 week 两行都没认出来（fail-closed）。"""

    def __init__(self, raw: str):
        super().__init__("认不出 /usage 输出里的 session/week 额度行，原文：\n" + raw)
        self.raw = raw


class UsageUnavailable(Exception):
    """`claude -p /usage` 本身没跑成（非零退出 / 超时 / 找不到可执行文件）。"""


_RE_SESSION = re.compile(r"Current session:\s*(\d+)%\s*used")
_RE_WEEK_ALL = re.compile(r"Current week \(all models\):\s*(\d+)%\s*used")
_RE_WEEK_MODEL = re.compile(r"Current week \(([^)]+)\):\s*(\d+)%\s*used")
_RE_RESETS = re.compile(r"resets\s+(.+?)\s*$")


def _resets_of(line: str, match: re.Match) -> str | None:
    """取该行额度数字后面跟着的 `resets …` 文本（若有）。"""
    m = _RE_RESETS.search(line, match.end())
    return m.group(1) if m else None


def parse_usage(text: str) -> dict:
    """把 /usage 的输出解析成结构化额度；两行主额度都缺则抛 UsageParseError。"""
    result: dict = {
        "session_pct": None,
        "session_resets": None,
        "week_all_pct": None,
        "week_all_resets": None,
        "per_model": {},
        "per_model_resets": {},
        "raw": text,
    }
    for line in text.splitlines():
        m = _RE_SESSION.search(line)
        if m:
            result["session_pct"] = int(m.group(1))
            result["session_resets"] = _resets_of(line, m)
            continue
        m = _RE_WEEK_ALL.search(line)
        if m:
            result["week_all_pct"] = int(m.group(1))
            result["week_all_resets"] = _resets_of(line, m)
            continue
        m = _RE_WEEK_MODEL.search(line)
        if m:
            name = m.group(1)
            result["per_model"][name] = int(m.group(2))
            resets = _resets_of(line, m)
            if resets:
                result["per_model_resets"][name] = resets
    if result["session_pct"] is None and result["week_all_pct"] is None:
        raise UsageParseError(text)
    return result


def fetch_usage(config: dict, timeout: int = 120) -> dict:
    """跑一次无头 /usage 并解析。非零退出或超时抛 UsageUnavailable。

    环境变量 NIGHTSHIFT_FAKE_USAGE_FILE：设了就读该文件当作 /usage 的输出，
    完全不起子进程——serve --once 的集成测试用（monkeypatch 管不到子进程）。
    环境里要去掉 CLAUDECODE（在 Claude Code 会话里嵌套调用会报错）。
    """
    fake_path = os.environ.get("NIGHTSHIFT_FAKE_USAGE_FILE")
    if fake_path:
        with open(fake_path, encoding="utf-8") as f:
            return parse_usage(f.read())
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    cmd = [
        config["claude_bin"],
        "-p",
        "/usage",
        "--model",
        config["probe_model"],
        "--tools",
        "",
    ]
    # cwd=home()；目录还不存在时 subprocess 抛的 FileNotFoundError 带的是
    # cwd 路径，会被误报成"找不到 claude"——先把数据目录骨架建出来。
    ensure_dirs()
    try:
        proc = subprocess.run(
            cmd,
            cwd=home(),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        tail = exc.stderr
        if isinstance(tail, bytes):
            tail = tail.decode("utf-8", "replace")
        raise UsageUnavailable(f"/usage 超时（{timeout}s）{(tail or '')[-500:]}") from exc
    except FileNotFoundError as exc:
        if exc.filename != cmd[0]:
            raise  # 不是可执行文件不在（比如 cwd 建不出来），原样上抛
        raise UsageUnavailable(f"找不到 claude 可执行文件：{cmd[0]}") from exc
    if proc.returncode != 0:
        raise UsageUnavailable(f"/usage 退出码 {proc.returncode}：{proc.stderr[-500:]}")
    return parse_usage(proc.stdout)


_RE_RESETS_AT = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{1,2})(?::(\d{2}))?(am|pm)\s*\((UTC)\)")


def resets_in_minutes(resets_text: str | None, now: datetime | None = None) -> int | None:
    """把 /usage 的 `Aug 27, 6:40pm (UTC)` 换算成"距现在几分钟刷新"（向上取整，最小 0）。

    认不出来（格式变了 / 不是 UTC）返回 None，调用方按未知处理。年份按当前年，
    若算出来在一天前以上，视为跨年取下一年。
    """
    if not resets_text:
        return None
    m = _RE_RESETS_AT.search(resets_text)
    if not m:
        return None
    now = now or datetime.now(timezone.utc)
    mon, day, hour, minute, ampm = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), m.group(5)
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    try:
        when = datetime.strptime(f"{now.year} {mon} {day} {hour}:{minute}", "%Y %b %d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if when < now - timedelta(days=1):
        when = when.replace(year=now.year + 1)
    return max(0, math.ceil((when - now).total_seconds() / 60))


def check_guards(usage: dict, model: str, config: dict, guards: dict) -> tuple[bool, str]:
    """额度门槛判定：五小时线、七日 all models 线、任务模型自己的单模型周线。

    全过返回 (True, "")；任一超线返回 (False, 中文原因)。
    """
    session_max = guards["session_pct_max"]
    week_max = guards["weekly_pct_max"]
    session_pct = usage["session_pct"]
    week_all_pct = usage["week_all_pct"]
    if session_pct is not None and session_pct > session_max:
        return False, f"五小时额度 {session_pct}% 超线 {session_max}%"
    if week_all_pct is not None and week_all_pct > week_max:
        return False, f"七日额度 {week_all_pct}% 超线 {week_max}%"
    label = config.get("models", {}).get(model, {}).get("usage_label")
    model_max = guards.get("model_weekly_pct_max", week_max)
    if label and label in usage.get("per_model", {}):
        pct = usage["per_model"][label]
        if pct > model_max:
            return False, f"模型 {label} 周额度 {pct}% 超线 {model_max}%"
    return True, ""


if __name__ == "__main__":
    from .store import load_config

    print(json.dumps(fetch_usage(load_config()), ensure_ascii=False, indent=2))
