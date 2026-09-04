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
from datetime import datetime, timezone

from .store import atomic_write_json, ensure_dirs, home, runner_config

__all__ = [
    "resets_in_minutes",
    "AppServerTimeout",
    "UsageParseError",
    "UsageUnavailable",
    "check_guards",
    "fetch_usage_claude",
    "fetch_usage_codex",
    "load_quota_file",
    "normalize_codex_ratelimits",
    "parse_usage",
    "usage_from_shared_rate_limits",
    "write_quota_runner",
]

# 一年期令牌（`claude setup-token`，放在 CLAUDE_CODE_OAUTH_TOKEN）只能发模型请求：
# 官方文档明写它"can only make model requests"，`claude -p /usage` 在它底下什么都不打印。
# 但每个 CC 会话每次回复都自带水位（状态栏脚本收到 rate_limits，stream-json 收到 rate_limit_event），
# 本机的状态栏脚本把它们合并写进一个共享文件（默认 ~/.claude/rate_limits.json，格式见
# usage_from_shared_rate_limits 的 docstring）。环境里有令牌时改读这个文件——零额外请求，
# 谁在花额度谁就在刷新它；夜班工人窗口自己也是 CC 会话，开工后读数自然跟着走。
DEFAULT_RATE_LIMITS_FILE = "~/.claude/rate_limits.json"
# 共享文件里单模型专属周线的原始键 → usage_label。目前只有 Fable 有专属周线，跑 Fable 的 CC
# 子进程在 rate_limit_event 里给的键叫 seven_day_overage_included（9/4 实测，数值与 /usage 的
# "Current week (Fable)" 一致）；可用 runners.claude.scoped_window_labels 覆盖
DEFAULT_SCOPED_WINDOW_LABELS = {"seven_day_overage_included": "Fable"}


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
    """把 /usage 的输出解析成结构化额度。

    抛 UsageParseError（fail-closed）的两种情形：
    - session 与 all models 两行主额度都没认出来；
    - 认出了单模型周线（`Current week (Fable)` 之类）却没认出 all models 那
      一行——多半是这一行括号里的措辞变了，被 _RE_WEEK_MODEL 当成一个"模型"
      收走；这时 week_all_pct 静默 None 会让七日线守卫无声失效、只剩五小时
      线在拦，宁可显式失败让预检推迟并把原文写进原因。
    只有 session 一行（没有任何 Current week 行）仍按"周线未知"放过。
    """
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
    if result["week_all_pct"] is None and result["per_model"]:
        raise UsageParseError(text)
    return result


def usage_from_shared_rate_limits(doc: dict, labels: dict | None = None, now: datetime | None = None) -> dict:
    """共享水位文件 → 与 parse_usage 同形的额度 dict。
    文件格式（/root/.claude/statusline.py 与小予 usage_quota.py 共同维护）：
        {"updated_at": epoch, "source": "statusline", "model": "...",
         "windows": {"five_hour": {"utilization": 9.0, "resets_at": "<ISO UTC>", "at": epoch},
                     "seven_day": {...}, "seven_day_overage_included": {...}}}
    utilization 是百分数；刷新时刻已经过去的窗口（读数之后没人再花过）按 0% 且刷新时刻未知处理。
    five_hour/seven_day 一个都没有就抛 UsageParseError（fail-closed，与 /usage 解析同样的口径）。"""
    labels = DEFAULT_SCOPED_WINDOW_LABELS if labels is None else labels
    now = now or datetime.now(timezone.utc)
    windows = (doc or {}).get("windows") or {}
    result: dict = {
        "session_pct": None,
        "session_resets": None,
        "week_all_pct": None,
        "week_all_resets": None,
        "per_model": {},
        "per_model_resets": {},
        "raw": "",
    }
    raw_lines = []

    def read(key):
        w = windows.get(key)
        if not isinstance(w, dict) or w.get("utilization") is None:
            return None, None
        pct = _pct_to_int(w.get("utilization"))
        resets = w.get("resets_at")
        try:
            reset_at = datetime.fromisoformat(str(resets).replace("Z", "+00:00")) if resets else None
        except ValueError:
            reset_at = None
        if reset_at is not None and reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        age_min = None
        if w.get("at"):
            age_min = int((now.timestamp() - float(w["at"])) / 60)
        if reset_at is not None and reset_at <= now:
            raw_lines.append(f"{key}: {pct}% (已过刷新时刻，按 0% 算；读数 {age_min} 分钟前)")
            return 0, None
        raw_lines.append(f"{key}: {pct}% resets {resets} (读数 {age_min} 分钟前)")
        return pct, resets

    result["session_pct"], result["session_resets"] = read("five_hour")
    result["week_all_pct"], result["week_all_resets"] = read("seven_day")
    for key in windows:
        if key in ("five_hour", "seven_day"):
            continue
        label = labels.get(key)
        if not label:
            continue
        pct, resets = read(key)
        if pct is None:
            continue
        result["per_model"][label] = pct
        if resets:
            result["per_model_resets"][label] = resets
    result["raw"] = "\n".join(raw_lines)
    if result["session_pct"] is None and result["week_all_pct"] is None:
        raise UsageParseError(result["raw"] or "(共享水位文件里没有 five_hour/seven_day)")
    return result


def fetch_usage_from_shared_file(rc: dict) -> dict:
    """一年期令牌路径：读共享水位文件。文件没有/读不出来抛 UsageUnavailable（原因写明让谁去开个 CC 会话）。"""
    path = os.path.expanduser(rc.get("rate_limits_file") or DEFAULT_RATE_LIMITS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError as exc:
        raise UsageUnavailable(
            f"共享水位文件 {path} 还没有：一年期令牌下 /usage 是空的，额度只能从 CC 会话的状态栏读数拿——"
            "随便开一个 claude 窗口让它回一句就有了"
        ) from exc
    except (OSError, ValueError) as exc:
        raise UsageUnavailable(f"共享水位文件 {path} 读不出来：{exc}") from exc
    labels = rc.get("scoped_window_labels")
    return usage_from_shared_rate_limits(doc, labels if isinstance(labels, dict) else None)


def fetch_usage_claude(config: dict, timeout: int = 120) -> dict:
    """查一次额度并解析。非零退出或超时抛 UsageUnavailable。

    三条路，按优先级：
    - 环境变量 NIGHTSHIFT_FAKE_USAGE_FILE：设了就读该文件当作 /usage 的输出，
      完全不起子进程——serve --once 的集成测试用（monkeypatch 管不到子进程）；
    - 环境里有 CLAUDE_CODE_OAUTH_TOKEN（一年期令牌）：/usage 在它底下是空的，改读状态栏
      脚本维护的共享水位文件（见 DEFAULT_RATE_LIMITS_FILE 处说明）；
    - 否则跑一次无头 `claude -p /usage` 解析。环境里要去掉 CLAUDECODE（在 Claude Code
      会话里嵌套调用会报错）。
    """
    fake_path = os.environ.get("NIGHTSHIFT_FAKE_USAGE_FILE")
    if fake_path:
        with open(fake_path, encoding="utf-8") as f:
            return parse_usage(f.read())
    rc = runner_config(config).get("claude") or {}
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        return fetch_usage_from_shared_file(rc)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    # S6.1 B3：统一从 runner_config 取 bin/probe_model（上面已取 rc），不再单独读顶层
    # config["claude_bin"]/config["probe_model"]——两处配置一旦不同会出现
    # "校验按新表、实际查额度按旧表"的分裂；兼容视图从顶层键合成，旧
    # config 行为不变。
    probe_model = rc.get("probe_model")
    if not probe_model:
        # None 塞进 argv 会让 subprocess 抛 TypeError——那不是调用方接得住的
        # UsageUnavailable，会把整轮 tick 掀翻；配置缺失就按"查不到"处理。
        raise UsageUnavailable("runners.claude.probe_model（或顶层 probe_model）没配，查不了额度")
    cmd = [
        rc.get("bin", "claude"),
        "-p",
        "/usage",
        "--model",
        probe_model,
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
    except OSError as exc:
        # 总review二 G15（D④-4）：以前这里还会判 FileNotFoundError 是不是
        # 指向 cmd[0]（不是就原样上抛，怀疑是 cwd 建不出来）——上面
        # ensure_dirs() 已经把 home()（cwd）建出来了，"cwd 不存在"这种
        # FileNotFoundError 到不了这里，分支已死，删了行为不变。
        # 找不到 / 没执行权限 / 其余起不了进程的错，都算"查不到"
        raise UsageUnavailable(f"起不了 claude（{cmd[0]}）：{exc}") from exc
    if proc.returncode != 0:
        raise UsageUnavailable(f"/usage 退出码 {proc.returncode}：{proc.stderr[-500:]}")
    return parse_usage(proc.stdout)


# 审查 D（9/2）：删掉了 `fetch_usage = fetch_usage_claude` 这个兼容别名——
# test_warmup 曾 monkeypatch 这个别名，而 scheduler 调的是 fetch_usage_claude，
# 补丁落空导致每跑一次测试就真起一次 `claude -p /usage`。两个名字指同一个
# 函数就是这种坑，只留带 runner 语义的那个。


_RE_RESETS_AT = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{1,2})(?::(\d{2}))?(am|pm)\s*\((UTC)\)")


def resets_in_minutes(resets_text: str | None, now: datetime | None = None) -> int | None:
    """把刷新时刻换算成"距现在几分钟刷新"（向上取整，最小 0）。

    两种输入形状都认：Codex 分片的 `_epoch_to_iso` 产出的 ISO 字符串
    （`2026-09-02T08:53:11Z`）优先按 ISO 解析；解析不出来再退回 /usage 的
    `Aug 27, 6:40pm (UTC)` 这种正则格式（S6 之前一直用的那条路）。

    都认不出来（格式变了 / 不是 UTC）返回 None，调用方按未知处理。/usage 不给
    年份：resets 只会落在"过去一天内 ~ 未来八天内"，在去年/今年/明年三个候选里
    取离现在最近的那个——跨年那几分钟缓存里还是 12 月 31 日，按"当前年"硬解析
    会算成明年 12 月 31 日（52 万分钟，hook 会排出上万个闹钟）。
    """
    if not resets_text:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        iso_when = datetime.fromisoformat(resets_text.replace("Z", "+00:00"))
    except ValueError:
        iso_when = None
    if iso_when is not None:
        if iso_when.tzinfo is None:
            iso_when = iso_when.replace(tzinfo=timezone.utc)
        return max(0, math.ceil((iso_when - now).total_seconds() / 60))
    m = _RE_RESETS_AT.search(resets_text)
    if not m:
        return None
    mon, day, hour, minute, ampm = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), m.group(5)
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(
                datetime.strptime(f"{year} {mon} {day} {hour}:{minute}", "%Y %b %d %H:%M")
                .replace(tzinfo=timezone.utc)
            )
        except ValueError:
            continue  # 比如非闰年的 2 月 29 日
    if not candidates:
        return None
    when = min(candidates, key=lambda d: abs((d - now).total_seconds()))
    return max(0, math.ceil((when - now).total_seconds() / 60))


class AppServerTimeout(UsageUnavailable):
    """codex app-server 在给定超时内没有回应/退出（连不上、卡死、协议不对）。"""


def _pct_to_int(value) -> int | None:
    """usedPercent 取整。codex 核心协议里 used_percent 是 f64（本机 rollout
    里落成 `47.0`），app-server 转出来可能是 `12` 也可能是 `12.0`——两种都认，
    浮点向上取整（宁可早拦半个点）；bool/字符串/NaN/缺失 → None，不造数。
    只认 int 会把 12.0 丢成 None，守卫从此全放行（fail-open）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return int(math.ceil(value))


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
            "used_pct": _pct_to_int(pct),
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


class _LineReader:
    """按行读 app-server 的 stdout：select 盯裸 fd + os.read 进自己的缓冲区。

    审查 D11：不能用 TextIOWrapper.readline()——服务端把一条通知和真正的响应
    放在同一次 write 里（或两行一起到达）时，第二行留在 Python 的读缓冲里，
    fd 层面不再"可读"，select 会一直等到 deadline 才报超时（15 s 后
    fail-closed 推迟，Codex 任务整夜起不来）。
    """

    def __init__(self, fd: int):
        self.fd = fd
        self.buf = bytearray()
        self.eof = False

    def readline(self, deadline: float) -> bytes | None:
        """返回一行（不含换行）；EOF 且缓冲空返回 None；到 deadline 抛 AppServerTimeout。"""
        while True:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                line = bytes(self.buf[:nl])
                del self.buf[: nl + 1]
                return line
            if self.eof:
                if self.buf:
                    line = bytes(self.buf)
                    self.buf.clear()
                    return line
                return None
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AppServerTimeout("codex app-server 超时")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self.fd, 65536)
            if not chunk:
                self.eof = True
            else:
                self.buf += chunk


def _read_jsonl_response(reader: _LineReader, want_id: int, deadline: float) -> dict:
    """从 app-server 的 stdout 按行读 JSON，直到拿到 id 匹配的那条或超时/EOF。"""
    while True:
        line = reader.readline(deadline)
        if line is None:
            raise AppServerTimeout("codex app-server 提前退出（EOF）")
        line = line.decode("utf-8", "replace").strip()
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
    reader = _LineReader(proc.stdout.fileno())  # 只从这里读 stdout，不碰 proc.stdout 的文本缓冲
    try:
        proc.stdin.write(json.dumps({
            "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "nightshift", "version": "1"}},
        }) + "\n")
        proc.stdin.flush()
        _read_jsonl_response(reader, 1, deadline)
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        proc.stdin.write(json.dumps({
            "id": 2, "method": "account/rateLimits/read", "params": {},
        }) + "\n")
        proc.stdin.flush()
        resp = _read_jsonl_response(reader, 2, deadline)
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

    文件缺失/坏 JSON/不是对象都返回两家皆空的空壳，不炸。

    总review二 G15（D④-2）：一期旧形状 `{"usage": ..., "fetched_at": ...}`
    的兼容分支删掉了——生产 quota.json 自 S6 起就是双分片形状，已核对确认。
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
    # 分片不是对象（手改/写坏）按空壳，消费方一律 slice_.get(...)，不能炸
    return {
        runner: (data.get(runner) if isinstance(data.get(runner), dict) else {})
        for runner in ("claude", "codex")
    }


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

    全过返回 (True, "")；任一到线（含等号，总review F7）返回 (False, 中文原因)。

    S6.1 B3：`usage_label` 查找必须按 `runner` 对应的模型表，不能只看顶层
    `config.models`（那只是 Claude 的兼容视图）——Codex 任务传自己的
    `runner="codex"` 就不会被 Claude 那张表误判。
    """
    cfg_guards = config.get("guards") or {}

    def line(key: str, fallback=None):
        # 任务 guards 里缺这条线或为 null → 回退 config.guards（与 create_task
        # 的合并语义一致：网页编辑把某条线清空就是"回到默认"，server 只做
        # task.update 不回填）。以前直接 guards["session_pct_max"] 会 KeyError，
        # _try_launch 抛出去让整轮 tick 中止，排在后面的任务全部不处理。
        value = guards.get(key)
        if value is None:
            value = cfg_guards.get(key)
        return fallback if value is None else value

    session_max = line("session_pct_max")
    week_max = line("weekly_pct_max")
    model_max = line("model_weekly_pct_max", week_max)
    for name, value in (
        ("session_pct_max", session_max),
        ("weekly_pct_max", week_max),
        ("model_weekly_pct_max", model_max),
    ):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return False, f"guards.{name} 不是数字：{value!r}（fail-closed）"
    # 某条线两处都没配 = 运维明确不设这条线，跳过（与 hook._quota_check 一致）
    session_pct = usage.get("session_pct")
    week_all_pct = usage.get("week_all_pct")
    if session_max is not None and session_pct is not None and session_pct >= session_max:
        return False, f"五小时额度 {session_pct}% 到线 {session_max}%"
    if week_max is not None and week_all_pct is not None and week_all_pct >= week_max:
        return False, f"七日额度 {week_all_pct}% 到线 {week_max}%"
    rc = runner_config(config).get(runner) or {}
    label = rc.get("models", {}).get(model, {}).get("usage_label")
    if label and model_max is not None and label in (usage.get("per_model") or {}):
        pct = usage["per_model"][label]
        if pct >= model_max:
            return False, f"模型 {label} 周额度 {pct}% 到线 {model_max}%"
    return True, ""


if __name__ == "__main__":
    from .store import load_config

    print(json.dumps(fetch_usage_claude(load_config()), ensure_ascii=False, indent=2))
