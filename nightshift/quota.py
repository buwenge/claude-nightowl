"""`claude -p "/usage"` 的调用与解析、额度门槛判定。

实测（设计稿 F1）无头 `/usage` 输出三行额度：
    Current session: 13% used · resets Aug 27, 6:40pm (UTC)
    Current week (all models): 19% used · resets Sep 2, 12pm (UTC)
    Current week (Fable): 35% used · resets Sep 2, 12pm (UTC)
其中除 all models 外的每一行是该模型的单独周线，预检必须一并认。
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import select
import subprocess
import time
from datetime import datetime, timedelta, timezone

from .store import atomic_write_json, ensure_dirs, home, runner_config

__all__ = [
    "resets_in_minutes",
    "AppServerTimeout",
    "UsageParseError",
    "UsageUnavailable",
    "check_guards",
    "fetch_usage",
    "fetch_usage_claude",
    "fetch_usage_codex",
    "load_quota_file",
    "normalize_codex_ratelimits",
    "parse_usage",
    "write_quota_runner",
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


def fetch_usage_claude(config: dict, timeout: int = 120) -> dict:
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
    # S6.1 B3：统一从 runner_config 取 bin/probe_model，不再单独读顶层
    # config["claude_bin"]/config["probe_model"]——两处配置一旦不同会出现
    # "校验按新表、实际查额度按旧表"的分裂；兼容视图从顶层键合成，旧
    # config 行为不变。
    rc = runner_config(config).get("claude") or {}
    cmd = [
        rc.get("bin", "claude"),
        "-p",
        "/usage",
        "--model",
        rc.get("probe_model"),
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


# 向后兼容别名：__main__.py 的 `nightshift quota` 子命令与一期测试仍按老名字
# 调用，语义原样不变（就是查 Claude 的额度）。S6 起新代码一律显式写
# fetch_usage_claude / fetch_usage_codex，不再用这个没有 runner 语义的名字。
fetch_usage = fetch_usage_claude


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


class AppServerTimeout(UsageUnavailable):
    """codex app-server 在给定超时内没有回应/退出（连不上、卡死、协议不对）。"""


def _epoch_to_iso(value) -> str | None:
    """rateLimits 的 resetsAt 是秒级 epoch 整数；不是数字就认不出，返回 None。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_codex_ratelimits(result: dict) -> dict:
    """把 `account/rateLimits/read` 的原始响应归一成与 Claude 同一形状的 usage dict。

    S6 靶测记录：`primary` 是五小时窗（windowDurationMins=300），`secondary`
    是周窗（=10080）——按 windowDurationMins 识别，不按位置盲猜；`rateLimits`
    与 `rateLimitsByLimitId[limitId]` 是同一份数据的两种呈现，优先按
    limitId 精确取，取不到才退回顶层 `rateLimits`。字段缺失一律 null，
    不造百分比、不 fail-open。
    """
    rl_flat = result.get("rateLimits") or {}
    by_id = result.get("rateLimitsByLimitId") or {}
    limit_id = rl_flat.get("limitId")
    rl = by_id.get(limit_id) if limit_id and limit_id in by_id else rl_flat

    windows: dict = {}
    session = week = None
    for key in ("primary", "secondary"):
        window = rl.get(key)
        if not isinstance(window, dict):
            continue
        mins = window.get("windowDurationMins")
        pct = window.get("usedPercent")
        entry = {
            "used_pct": pct if isinstance(pct, int) else None,
            "window_minutes": mins,
            "resets_at": _epoch_to_iso(window.get("resetsAt")),
        }
        windows[key] = entry
        if mins == 300:
            session = entry
        elif mins == 10080:
            week = entry

    credits = result.get("rateLimitResetCredits") or {}
    return {
        "session_pct": session["used_pct"] if session else None,
        "session_resets": session["resets_at"] if session else None,
        "week_all_pct": week["used_pct"] if week else None,
        "week_all_resets": week["resets_at"] if week else None,
        "per_model": {},       # Codex 没有单模型周线这个概念
        "per_model_resets": {},
        "rate_limit_reached_type": rl.get("rateLimitReachedType"),
        "reset_credits_available": credits.get("availableCount"),
        "windows": windows,
    }


def _read_jsonl_response(proc: subprocess.Popen, want_id: int, deadline: float) -> dict:
    """从 app-server 的 stdout 按行读 JSON，直到拿到 id 匹配的那条或超时/EOF。"""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise AppServerTimeout("codex app-server 超时")
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            raise AppServerTimeout("codex app-server 提前退出（EOF）")
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("id") == want_id:
            return obj


def fetch_usage_codex(config: dict, timeout: float = 15.0) -> dict:
    """起一次短命 `codex app-server --stdio`，握手后取一次
    `account/rateLimits/read`，归一成统一形状。全程标准库，不联网测试用
    fake app-server 脚本（NIGHTSHIFT_CODEX_BIN 覆盖，与 launcher 共用同一个
    环境变量——同一个 codex 可执行文件，只是这里传的子命令不同）。

    每次都要设超时、关 stdin、回收子进程；错误只留脱敏尾部，不带原始
    payload（可能含账号/额度重置券这类不该进日志的内容）。
    """
    bin_path = os.environ.get("NIGHTSHIFT_CODEX_BIN") or (
        runner_config(config).get("codex") or {}
    ).get("bin", "codex")
    ensure_dirs()
    try:
        proc = subprocess.Popen(
            [bin_path, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=home(),
        )
    except FileNotFoundError as exc:
        raise UsageUnavailable(f"找不到 codex 可执行文件：{bin_path}") from exc

    deadline = time.time() + timeout
    try:
        proc.stdin.write(json.dumps({
            "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "nightshift", "version": "1"}},
        }) + "\n")
        proc.stdin.flush()
        _read_jsonl_response(proc, 1, deadline)
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        proc.stdin.write(json.dumps({
            "id": 2, "method": "account/rateLimits/read", "params": {},
        }) + "\n")
        proc.stdin.flush()
        resp = _read_jsonl_response(proc, 2, deadline)
    except AppServerTimeout:
        raise
    except OSError as exc:
        raise UsageUnavailable(f"codex app-server 通信失败：{exc}") from exc
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

    if not isinstance(resp.get("result"), dict):
        tail = ""
        try:
            tail = (proc.stderr.read() or "")[-500:]
        except Exception:
            pass
        raise UsageUnavailable(f"codex app-server 没有返回 rateLimits：{tail}")
    return normalize_codex_ratelimits(resp["result"])


# ---------- quota.json：双 runner 归一读写（S6） ----------


def load_quota_file() -> dict:
    """读 quota.json，统一成 {"claude": {...}, "codex": {...}} 形状（各自
    "usage"/"fetched_at"/"error" 三键）。

    兼容一期旧形状 `{"usage": ..., "fetched_at": ...}`（按 claude 解释）；
    读取时不改盘——下次哪家成功刷新了，才会把整份文件换成新形状。
    文件缺失/坏 JSON/不是对象都返回两家皆空的空壳，不炸。
    """
    path = home() / "quota.json"
    empty = {"claude": {}, "codex": {}}
    if not path.is_file():
        return empty
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    if "claude" not in data and "codex" not in data:
        return {"claude": data, "codex": {}}  # 一期旧形状：整份就是 claude 那份
    return {"claude": data.get("claude") or {}, "codex": data.get("codex") or {}}


def write_quota_runner(runner: str, payload: dict) -> dict:
    """只更新一家（claude/codex）的分片，另一家原样保留——一家刷新失败
    不能覆盖另一家最后一次的好数据。返回写盘后的整份内容。

    S6.1 A6：scheduler 主线程与网页手动刷新（ThreadingHTTPServer 的请求
    线程）会并发调用这个函数，读旧值→改一家→原子替换这整段必须在同一把
    `.quota.lock` 的 flock 里完成，否则两个线程交错读到同一份旧值、各自
    只改自己那一家再各自写回，后写的会把先写的那一家覆盖丢掉（lost
    update）——只改 atomic_write_json 的临时文件名消不掉这个问题，那只是
    消掉两个线程抢同一个临时文件名的异常，读改写本身仍然没有互斥。
    """
    ensure_dirs()
    with open(home() / ".quota.lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = load_quota_file()
            data[runner] = payload
            atomic_write_json(home() / "quota.json", data)
            return data
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def check_guards(
    usage: dict, model: str, config: dict, guards: dict, runner: str = "claude",
) -> tuple[bool, str]:
    """额度门槛判定：五小时线、七日 all models 线、任务模型自己的单模型周线。

    全过返回 (True, "")；任一超线返回 (False, 中文原因)。

    S6.1 B3：`usage_label` 查找必须按 `runner` 对应的模型表，不能只看顶层
    `config.models`（那只是 Claude 的兼容视图）——Codex 任务传自己的
    `runner="codex"` 就不会被 Claude 那张表误判。
    """
    session_max = guards["session_pct_max"]
    week_max = guards["weekly_pct_max"]
    session_pct = usage["session_pct"]
    week_all_pct = usage["week_all_pct"]
    if session_pct is not None and session_pct > session_max:
        return False, f"五小时额度 {session_pct}% 超线 {session_max}%"
    if week_all_pct is not None and week_all_pct > week_max:
        return False, f"七日额度 {week_all_pct}% 超线 {week_max}%"
    rc = runner_config(config).get(runner) or {}
    label = rc.get("models", {}).get(model, {}).get("usage_label")
    model_max = guards.get("model_weekly_pct_max", week_max)
    if label and label in usage.get("per_model", {}):
        pct = usage["per_model"][label]
        if pct > model_max:
            return False, f"模型 {label} 周额度 {pct}% 超线 {model_max}%"
    return True, ""


if __name__ == "__main__":
    from .store import load_config

    print(json.dumps(fetch_usage(load_config()), ensure_ascii=False, indent=2))
