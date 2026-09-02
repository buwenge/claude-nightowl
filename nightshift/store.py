"""数据目录、config、任务与状态的读写（原子写 + flock）。

约定：
- 所有写盘一律"同目录临时文件 + os.replace"原子替换；
- status.json 的读-改-写只允许走 modify_status()（update_status 是它的
  字段合并特例），靠 tasks/<id>/.lock 上的 fcntl.flock 串行化（hook 进程
  与调度器会并发写同一份状态）。计数器一类的"读旧值算新值"必须整个在
  锁内完成，锁外先 read 再 update 会丢增量。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .context import context_limit_for

__all__ = [
    "CODEX_BACKGROUND_INSTRUCTION",
    "ConfigInvalid",
    "ConfigMissing",
    "ENDED_STATES",
    "ON_NO_QUOTA_VALUES",
    "REVIEW_KEYS",
    "RUNNERS",
    "STATES",
    "WORKTREE_INSTRUCTION",
    "append_event",
    "atomic_write_json",
    "atomic_write_text",
    "build_prompt",
    "chain_state",
    "create_cross_role_successor",
    "create_same_role_successor",
    "create_successor",
    "create_task",
    "effective_effort",
    "effective_model",
    "effective_runner",
    "ensure_dirs",
    "home",
    "list_tasks",
    "load_config",
    "load_task",
    "modify_status",
    "new_task_id",
    "next_pipeline_shift",
    "pipeline_id_of",
    "read_status",
    "render",
    "render_review_fix_prompt",
    "render_review_prompt",
    "review_config",
    "role_of",
    "round_of",
    "runner_config",
    "runner_label",
    "task_dir",
    "update_status",
    "utc_now_iso",
    "validate_task",
    "worktree_enabled",
]

# S6：两家工人。运行时永远只认这两个字面量，config.runners 只提供各自的
# bin/models/efforts/保活参数，不扩展这个集合。
RUNNERS = ("claude", "codex")


class ConfigMissing(Exception):
    """数据目录里没有 config.json。"""


class ConfigInvalid(Exception):
    """config.json（或 task.review）里的审稿配置值类型/取值不对——S7.1
    非阻断尾巴：以前 config.review.max_rounds/on_no_quota/merge_policy 没有
    统一校验，坏值会一路传到 scheduler 里 int(...)/字符串比较才炸，报错
    完全看不出是哪个 pipeline 哪份配置的问题。review_config() 在合并出
    最终视图时就地校验、就地报出人话原因。"""


# 任务状态全集（设计稿 §3 状态机 + S5② 工作树收尾）
# awaiting_merge 不是 active（不拦起跑）；merged / discarded 是终态
STATES = (
    "scheduled",
    "postponed",
    "launching",
    "working",
    "waiting_background",
    "waiting_wakeup",
    "idle",
    "held",
    "chained",
    "exited",
    "finished",
    "awaiting_merge",
    "merged",
    "discarded",
    "failed",
    "cancelled",
    "needs_attention",
    "chain_exhausted",
)

# create_task 必填字段（run_at 对 after 触发的任务可缺，见 create_task）
_REQUIRED_FIELDS = (
    "title",
    "project",
    "model",
    "effort",
    "run_at",
    "task_text",
    "prompt_final",
)

# S5：工作树任务提示词里的运行时安全前言（经 {worktree_instruction} 占位符
# 渲染进模板；launcher 还会在 prompt.txt 缺它时补一层，保证不可遗漏）
WORKTREE_INSTRUCTION = (
    "只在当前工作树里施工，不要切回主签出目录；不要 git commit，"
    "调度器会在每班收工后替你打存档点。完成或换班时照常写交接，"
    "末行写 NEXT: done 或 NEXT: continue。"
)
# S6.1 A1：Codex 任务提示词里的运行时安全前言（F12）——真机靶测证实这条协议
# 只靠手工 prompt 才走通，nightshift 自己创建的任务原本完全没有告诉 Codex
# 要走这条路；跟 WORKTREE_INSTRUCTION 同一个模式，launcher._prompt_text()
# 保证运行时兜底追加一次，不依赖生产 config.prompt_template 人工同步。
CODEX_BACKGROUND_INSTRUCTION = (
    "长任务统一用 `python3 -m nightshift.background_runner start -- <程序> <参数...>` "
    "起：这个 wrapper 自己会留在前台，靠 Codex 的后台终端/yield 机制让这次工具调用先"
    "返回。拿到 running 的 session/process id 后不要再 poll 它，等 nightshift 调度器"
    "主动把结果敲给你再继续。禁止裸用 `&`/`nohup`/`setsid`，禁止自己 fork 脱离——"
    "那样起的后台进程在这次工具调用判定完成时会被沙箱一并回收，永远不会跑完。"
)
# review.merge_policy 只认这两个值
_MERGE_POLICIES = ("manual", "auto")
# S7：review 对象只认这七个键；旧任务/S5 占位对象（只有 enabled/merge_policy）
# 是这个集合的子集，天然兼容。
REVIEW_KEYS = frozenset({
    "enabled", "runner", "model", "effort", "max_rounds",
    "on_no_quota", "merge_policy", "criteria_text",
})
# review.on_no_quota 只认这两个值
ON_NO_QUOTA_VALUES = ("release", "hold")
# config.review 兜底默认值（task.review 没显式给的键从这里取）
_REVIEW_CONFIG_DEFAULTS = {
    "max_rounds": 5,
    "on_no_quota": "release",
    "merge_policy": "manual",
    "criteria_text": "",
}

# run_at 只认 `YYYY-MM-DDTHH:MM:SSZ`（utc_now_iso 写的）或带 1–6 位小数秒的
# 同形状（网页 Date.toISOString() 写的 .000Z）。9/1 审查：以前只查"Z 结尾且
# 去掉 Z 能 fromisoformat"，`…T18:00:00+08:00Z` 能过校验但 scheduler.parse_iso
# 直接 ValueError；空格分隔/紧凑格式能解析但 list_tasks 按字符串排序会错位
# （A 组 N9）。前端/CLI 正常路径发的两种形状都在这个正则里。
_RE_RUN_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
# guards 里的数值线：百分比 0–100，tokens/比例为正数；bool 不算数字
_GUARD_PCT_KEYS = ("session_pct_max", "weekly_pct_max", "model_weekly_pct_max")
_GUARD_TOKEN_KEYS = ("context_warn_tokens", "context_limit_tokens")
# 任务 id 的形状（与 server 路由正则一致）：trigger.task 也只认这个形状，不让
# "../tasks/<id>" 这类能凑出 task.json 的相对路径混进 task.json
_RE_TASK_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
# 总review二 G11：表外模型名（新建页「自定义…」填的）只查形状，不查是否在
# config 模型表里——真实模型 id 都是这个形状（如 claude-fable-5-1、
# gpt-5.6-luna），打错字/凭空编一个会在起跑时 claude/codex 报错才知道，
# 不受单模型周线保护（前端已在自定义输入框旁写了这句提醒）。
_RE_MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,63}$")

# trigger.type == "after" 且 when == "ended" 时，前置链最新一班落在这些状态
# 就算"已结束"（调度器与网页共用这一个定义）
ENDED_STATES = (
    "finished",
    "awaiting_merge",
    "merged",
    "discarded",
    "exited",
    "failed",
    "cancelled",
    "chain_exhausted",
    "needs_attention",
)


def home() -> Path:
    """数据目录根：环境变量 NIGHTSHIFT_HOME，默认 ~/.nightshift。"""
    return Path(os.environ.get("NIGHTSHIFT_HOME") or (Path.home() / ".nightshift"))


def ensure_dirs() -> None:
    """确保数据目录骨架存在。"""
    home().mkdir(parents=True, exist_ok=True)
    (home() / "tasks").mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（秒级，Z 结尾）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config() -> dict:
    """读数据目录里的 config.json；不做默认值合并，缺键让它炸出来。"""
    path = home() / "config.json"
    if not path.is_file():
        raise ConfigMissing(
            f"配置文件不存在：{path}。"
            f"请把仓库里的 config.example.json 复制为该路径，"
            f"改成自己的 projects/models 等配置再用。"
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def atomic_write_text(path: str | os.PathLike, text: str, mode: int | None = None) -> None:
    """同目录写临时文件再 os.replace，保证读方要么看到旧整份要么看到新整份。

    给了 mode（如 0o600）就用 os.open(tmp, O_WRONLY|O_CREAT|O_EXCL, mode) 建
    临时文件，落盘即收紧权限，不留"先按 umask 落地再 chmod"的旁观窗口。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # S7.1 阻断二：单用 pid 命名在同进程内并发调用（多线程/hook 竞态测试）
    # 会撞同一个临时文件名，加 uuid nonce 保证每次调用独占自己的临时文件。
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        if mode is not None:
            f = os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "w", encoding="utf-8")
        else:
            f = open(tmp, "w", encoding="utf-8")
        with f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_json(path: str | os.PathLike, obj, mode: int | None = None) -> None:
    """原子写 JSON 文件（UTF-8、缩进两格）。mode 语义同 atomic_write_text。"""
    atomic_write_text(
        path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n", mode=mode
    )


def new_task_id() -> str:
    """YYYYMMDD-HHMMSS-<4 位十六进制随机>（UTC 时间戳）。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def task_dir(task_id: str) -> Path:
    """某个任务的目录。"""
    return home() / "tasks" / task_id


def worktree_enabled(task: dict) -> bool:
    """该任务是否走工作树路径：只有显式 true 才算；S5 上线前落盘的旧任务
    没有 worktree 字段，必须按 false 解释（一期路径），绝不偷偷迁移。"""
    return task.get("worktree") is True


def runner_label(runner: str) -> str:
    """runner 字面量的人话标签，供错误文案/前端展示使用。"""
    return "Codex" if runner == "codex" else "Claude Code"


# ---------- S7：流水线 / 角色 / 有效工人（唯一权威源） ----------


def pipeline_id_of(task: dict) -> str:
    """这一班所属的流水线 id：pipeline_id 缺失（S7 之前落盘的旧任务）按
    root_id 兼容，再缺就按自身 id——只读兼容，不加载即回写。"""
    return task.get("pipeline_id") or task.get("root_id") or task["id"]


def role_of(task: dict) -> str:
    """这一班的角色：build | review。旧任务缺 role 按 build 解释。"""
    return task.get("role") or "build"


def round_of(task: dict) -> int:
    """这一班所属的返工轮次。旧任务缺 round 按 1 解释。"""
    return int(task.get("round") or 1)


def effective_runner(task: dict) -> str:
    """这一班真正要用的工人（S7 唯一权威源，launcher/额度预检/保活/状态
    展示一律走这里，不许各自直接读 task["runner"]）：
    build 角色取顶层 runner（旧任务缺省 claude，一期语义不变）；
    review 角色取 task.review.runner。"""
    if role_of(task) == "review":
        return (task.get("review") or {}).get("runner") or "claude"
    return task.get("runner") or "claude"


def effective_model(task: dict) -> str:
    """同 effective_runner，取模型；review 角色缺失时退顶层 model 只是防炸，
    正常流水线里 review.enabled=true 必有 review.model（validate_task 保证）。"""
    if role_of(task) == "review":
        return (task.get("review") or {}).get("model") or task.get("model") or ""
    return task.get("model") or ""


def effective_effort(task: dict) -> str:
    """同 effective_runner，取档位。"""
    if role_of(task) == "review":
        return (task.get("review") or {}).get("effort") or task.get("effort") or ""
    return task.get("effort") or ""


def review_config(task: dict, config: dict) -> dict:
    """review 配置的合并视图：task.review 里显式给的键优先，缺的从
    config.review 同名键兜底，config 也没有就用代码内置默认值
    （_REVIEW_CONFIG_DEFAULTS）。返回值总是含全部七个键，供 pipeline 逻辑
    统一读取，不必每处都做三层 or 判断。"""
    task_review = task.get("review") or {}
    cfg_review = config.get("review") or {}

    def pick(key: str):
        if key in task_review and task_review[key] is not None:
            return task_review[key]
        if key in cfg_review and cfg_review[key] is not None:
            return cfg_review[key]
        return _REVIEW_CONFIG_DEFAULTS.get(key)

    max_rounds = pick("max_rounds")
    on_no_quota = pick("on_no_quota")
    merge_policy = pick("merge_policy")
    # S7.1 非阻断尾巴：坏值就地报出人话原因，不要让它一路传到 scheduler
    # 里某个 int(...)/字符串比较才炸——那时完全看不出是哪份配置的问题。
    # S7.2 兼容尾巴 1：改用跟 `validate_task`（本文件里同名字段的任务级
    # 校验）完全一致的严格口径——`int(max_rounds)` 会悄悄接受
    # `True`（bool 是 int 子类）、`1.5`（截断成 1）、`"5"`（数字字符串）
    # 这类不该通过的值；config.review 是直接从 config.json 反序列化，没有
    # 经过 create_task 时的 validate_task 把关，必须在这里同样严格。
    if (
        isinstance(max_rounds, bool)
        or not isinstance(max_rounds, int)
        or max_rounds <= 0
    ):
        raise ConfigInvalid(f"review.max_rounds 必须是正整数，读到 {max_rounds!r}")
    max_rounds_int = max_rounds
    if on_no_quota not in ON_NO_QUOTA_VALUES:
        raise ConfigInvalid(
            f"review.on_no_quota 只认 {'/'.join(ON_NO_QUOTA_VALUES)}，读到 {on_no_quota!r}"
        )
    if merge_policy not in _MERGE_POLICIES:
        raise ConfigInvalid(
            f"review.merge_policy 只认 {'/'.join(_MERGE_POLICIES)}，读到 {merge_policy!r}"
        )

    return {
        "enabled": bool(task_review.get("enabled", False)),
        "runner": task_review.get("runner"),
        "model": task_review.get("model"),
        "effort": task_review.get("effort"),
        "max_rounds": max_rounds_int,
        "on_no_quota": on_no_quota,
        "merge_policy": merge_policy,
        "criteria_text": pick("criteria_text"),
    }


def runner_config(config: dict) -> dict:
    """统一 runner 配置视图：{"claude": {...}, "codex": {...}}（codex 键
    不一定存在——旧配置或没配 codex 时就是没有）。

    - config.runners 存在且含 "claude" 键：直接原样返回（S6 起的新配置）；
    - 否则（S6 上线前的旧 config，或 runners 只写了 codex 没写 claude）：
      从顶层 claude_bin/probe_model/models/efforts/scheduler.keepalive_*
      合成一份只含 claude 的兼容视图，旧配置/旧任务因此原样能跑；
      若 config.runners 里另外声明了 codex（哪怕没声明 claude），一并纳入。
    """
    runners = config.get("runners")
    sch = config.get("scheduler") or {}
    compat = {
        "claude": {
            "bin": config.get("claude_bin", "claude"),
            "probe_model": config.get("probe_model"),
            "models": config.get("models") or {},
            "efforts": config.get("efforts") or [],
            "keepalive_idle_minutes": sch.get("keepalive_idle_minutes", 50),
            "keepalive_text": sch.get("keepalive_text"),
        }
    }
    if isinstance(runners, dict):
        if "claude" in runners:
            return runners
        compat.update(runners)
    return compat


def _looks_like_model_name(value) -> bool:
    """G11：表外模型名的形状校验——不在配置的模型表里，但长得像一个真实
    模型 id（小写字母/数字/点/短横线）就放行，交给起跑时的 claude/codex
    自己去认。"""
    return isinstance(value, str) and bool(_RE_MODEL_NAME.match(value))


def validate_task(task: dict, config: dict, *, task_id: str | None = None) -> str:
    """校验一个完整任务 dict；不合法抛 ValueError，通过返回归一后的触发类型。

    create_task 与网页编辑（PUT /api/tasks/<id>）共用这一套，规则只此一份：
    - 必填：title / project / model / effort / task_text / prompt_final；
    - project、effort 必须在 config 里；run_at 必须是 Z 结尾 ISO UTC，
      但 trigger.type == "after" 时允许缺（create_task 会补创建时刻，只当
      排序用，起不起由前置链状态决定）；
    - trigger：None 按 {"type": "time"} 看待；after 要求 task 是已存在的
      任务 id（task.json 读得到，task_id 给出时还不许指向自己）、
      when 只认 finished / ended；
    - guards / chain（S4.1）：存在时必须是 JSON 对象；guards 里的
      auto_interrupt_minutes 若存在且非 null，必须是正整数（bool 不算
      整数）——防止网页 PUT 把 task.json 写坏、把调度器的 int(...) 炸出来
      或负数让它立刻中止。
    - worktree / review（S5）：worktree 存在必须是布尔值；review 存在必须是
      对象且只认 enabled / merge_policy 两个键，enabled 只能 false
      （联动审稿要到 S7 才开放），merge_policy 只认 manual / auto。
    - runner（S6）：缺省 claude；只认 RUNNERS 里的字面量；该 runner 必须在
      config.runners（或旧配置合成的兼容视图）里配置了，否则报人话错误；
      model / effort 严格限定在这个 runner 自己的 models/efforts 里——
      不能拿 Claude 的模型表校验 Codex 任务，反之亦然。models 字典为空
      （旧配置常见）时跳过模型名单校验，只保留原有的 effort 校验。
    """
    for key in _REQUIRED_FIELDS:
        if key == "run_at":
            continue  # after 任务允许缺，最后统一判
        if not task.get(key):
            raise ValueError(f"缺少必填字段：{key}")
    if task["project"] not in config["projects"]:
        raise ValueError(f"project 不在 config.projects 里：{task['project']}")

    runner = task.get("runner") or "claude"
    if runner not in RUNNERS:
        raise ValueError(f"runner 只认 {'/'.join(RUNNERS)}：{runner}")
    rc = runner_config(config).get(runner)
    if not rc:
        raise ValueError(f"{runner_label(runner)} 还没在 config.runners 里配置")
    label = runner_label(runner)
    if task["effort"] not in (rc.get("efforts") or []):
        raise ValueError(f"{label} 不支持这个档位：{task['effort']}")
    models = rc.get("models") or {}
    if models and task["model"] not in models and not _looks_like_model_name(task["model"]):
        raise ValueError(f"{label} 不支持这个模型：{task['model']}")

    for key in ("guards", "chain"):
        value = task.get(key)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{key} 必须是对象")
    guards = task.get("guards") or {}
    auto = guards.get("auto_interrupt_minutes")
    if auto is not None and (
        isinstance(auto, bool) or not isinstance(auto, int) or auto <= 0
    ):
        raise ValueError("guards.auto_interrupt_minutes 必须是正整数")
    # 9/1 审查：其余数值线以前不查类型/范围，"80"（字符串）、True、150 都能落盘，
    # 到 quota.check_guards 的 `int > str` / hook.warn_threshold 的 int("abc") 才炸
    # ——预检或 hook 里炸掉的是整条 tick/整个会话，不是这一条任务。
    for key in _GUARD_PCT_KEYS:
        value = guards.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= 100
        ):
            raise ValueError(f"guards.{key} 必须是 0–100 的数字")
    for key in _GUARD_TOKEN_KEYS:
        value = guards.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
        ):
            raise ValueError(f"guards.{key} 必须是正数")
    ratio = guards.get("context_warn_ratio")
    if ratio is not None and (
        isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < ratio <= 1
    ):
        raise ValueError("guards.context_warn_ratio 必须是 0–1 之间的数字")
    if guards.get("keepalive") is not None and not isinstance(guards["keepalive"], bool):
        raise ValueError("guards.keepalive 必须是布尔值")
    if guards.get("context_warn_text") is not None and not isinstance(guards["context_warn_text"], str):
        raise ValueError("guards.context_warn_text 必须是字符串")
    chain = task.get("chain") or {}
    max_windows = chain.get("max_windows")
    if max_windows is not None and (
        isinstance(max_windows, bool) or not isinstance(max_windows, int) or max_windows <= 0
    ):
        raise ValueError("chain.max_windows 必须是正整数")
    if chain.get("on_no_handover") is not None and chain["on_no_handover"] not in ("continue", "stop"):
        raise ValueError("chain.on_no_handover 只认 continue / stop")

    # S5：worktree 只有显式 true/false 两种
    if task.get("worktree") is not None and not isinstance(task.get("worktree"), bool):
        raise ValueError("worktree 必须是布尔值")
    # S8：keepalive 是任务自己的长期显式覆盖（跟 guards.keepalive 那个全局
    # 兜底开关并存，见 scheduler._maybe_keepalive），只认 enabled 一个键。
    keepalive = task.get("keepalive")
    if keepalive is not None:
        if not isinstance(keepalive, dict):
            raise ValueError("keepalive 必须是对象")
        unknown = sorted(set(keepalive) - {"enabled"})
        if unknown:
            raise ValueError(f"keepalive 只认 enabled，多出：{'、'.join(unknown)}")
        if "enabled" in keepalive and not isinstance(keepalive["enabled"], bool):
            raise ValueError("keepalive.enabled 必须是布尔值")
    # S7：review 只认 REVIEW_KEYS 七个键；enabled=true 要求 worktree=true，
    # 且审稿方 runner/model/effort 必须真实存在于 config.runners（或旧配置
    # 合成的兼容视图）；enabled=false 继续接受 S5 的最小占位对象。
    review = task.get("review")
    if review is not None:
        if not isinstance(review, dict):
            raise ValueError("review 必须是对象")
        unknown = sorted(set(review) - REVIEW_KEYS)
        if unknown:
            raise ValueError(
                f"review 只认 {'、'.join(sorted(REVIEW_KEYS))}，多出：{'、'.join(unknown)}"
            )
        enabled = review.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("review.enabled 必须是布尔值")
        policy = review.get("merge_policy", "manual")
        if policy not in _MERGE_POLICIES:
            raise ValueError("review.merge_policy 只认 manual / auto")
        if "max_rounds" in review and review["max_rounds"] is not None:
            max_rounds = review["max_rounds"]
            if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds <= 0:
                raise ValueError("review.max_rounds 必须是正整数")
        if "on_no_quota" in review and review["on_no_quota"] is not None:
            if review["on_no_quota"] not in ON_NO_QUOTA_VALUES:
                raise ValueError("review.on_no_quota 只认 release / hold")
        if "criteria_text" in review and review["criteria_text"] is not None:
            if not isinstance(review["criteria_text"], str):
                raise ValueError("review.criteria_text 必须是字符串")
        if enabled:
            if task.get("worktree") is not True:
                raise ValueError("联动审稿（review.enabled=true）要求 worktree=true")
            r_runner = review.get("runner")
            if r_runner not in RUNNERS:
                raise ValueError(f"review.runner 只认 {'/'.join(RUNNERS)}：{r_runner}")
            r_rc = runner_config(config).get(r_runner)
            if not r_rc:
                raise ValueError(f"审稿方 {runner_label(r_runner)} 还没在 config.runners 里配置")
            r_label = runner_label(r_runner)
            if review.get("effort") not in (r_rc.get("efforts") or []):
                raise ValueError(f"审稿方 {r_label} 不支持这个档位：{review.get('effort')}")
            r_models = r_rc.get("models") or {}
            if (
                r_models and review.get("model") not in r_models
                and not _looks_like_model_name(review.get("model"))
            ):
                raise ValueError(f"审稿方 {r_label} 不支持这个模型：{review.get('model')}")

    trigger = task.get("trigger")
    if trigger is None:
        ttype = "time"
    elif not isinstance(trigger, dict):
        raise ValueError("trigger 必须是对象")
    else:
        ttype = trigger.get("type")
        if ttype == "time":
            pass
        elif ttype == "after":
            pre_id = trigger.get("task")
            if (
                not isinstance(pre_id, str) or not _RE_TASK_ID.match(pre_id)
                or task_id == pre_id or not (task_dir(pre_id) / "task.json").is_file()
            ):
                raise ValueError(f"trigger.task 必须是已存在的任务 id：{pre_id}")
            if trigger.get("when") not in ("finished", "ended"):
                raise ValueError("trigger.when 只认 finished / ended")
        else:
            raise ValueError("trigger.type 只认 time / after")

    run_at = task.get("run_at")
    if run_at:
        if not (isinstance(run_at, str) and _RE_RUN_AT.match(run_at)):
            raise ValueError("run_at 必须是 Z 结尾的 ISO UTC 时间，如 2026-08-27T18:00:00Z")
        try:
            datetime.fromisoformat(run_at[:-1])
        except ValueError:
            raise ValueError(f"run_at 不是合法的 ISO 时间：{run_at}") from None
    elif ttype != "after":
        raise ValueError("缺少必填字段：run_at")
    return ttype


def create_task(task: dict, config: dict) -> str:
    """校验并落盘一个新任务（task.json + 初始 status.json），返回任务 id。

    trigger 缺省补 {"type": "time"}；type == "after" 时 run_at 可以不给
    （补成创建时刻，只当排序用）。S5 起新任务缺省 worktree=true（建树施工），
    review 占住 {"enabled": false, "merge_policy": "manual"} 的形状供 S7 扩写；
    显式 worktree=false 走一期旧路径。
    """
    data = dict(task)
    data["trigger"] = data.get("trigger") or {"type": "time"}
    data["runner"] = data.get("runner") or "claude"  # S6：新任务显式落盘缺省值
    if data.get("worktree") is None:
        data["worktree"] = True  # 新任务缺省建树
    validate_task(data, config)  # 先原样校验：review 形状不对在这里报人话错误
    review = data.get("review")
    if not isinstance(review, dict):
        data["review"] = {"enabled": False, "merge_policy": "manual"}
    elif not review.get("enabled"):
        # 未开审稿：保持 S5 的最小占位形状，不给旧式任务凭空堆七个键
        data["review"] = {
            "enabled": False,
            "merge_policy": review.get("merge_policy") or "manual",
        }
    else:
        # S7：开审稿的新任务落盘时把七个键的兜底值坐实，后续读取不必每次
        # 都经 review_config() 三层 or；review_config() 对老任务仍然兼容。
        merged = review_config(data, config)
        merged.update({
            "enabled": True,
            "runner": review["runner"],
            "model": review["model"],
            "effort": review["effort"],
        })
        data["review"] = merged
    # S8：新任务缺省 keepalive.enabled=true（长期开关，落盘归一成只含
    # enabled 一个键的最小形状）；旧任务/未显式给的编辑路径不受影响
    # （_api_update_task 只在 data 里带了 keepalive 键时才会走到这里）。
    keepalive = data.get("keepalive")
    if not isinstance(keepalive, dict) or not isinstance(keepalive.get("enabled"), bool):
        data["keepalive"] = {"enabled": True}
    else:
        data["keepalive"] = {"enabled": keepalive["enabled"]}
    if not data.get("run_at"):
        data["run_at"] = utc_now_iso()  # after 任务：只当排序用

    task_id = new_task_id()
    data["id"] = task_id
    data["created_at"] = utc_now_iso()
    data.setdefault("shift", 1)
    data.setdefault("role", "build")
    data.setdefault("round", 1)
    data.setdefault("role_shift", 1)
    data.setdefault("pipeline_id", task_id)
    guards = dict(config.get("guards") or {})
    guards.update(data.pop("guards", None) or {})
    data["guards"] = guards
    chain = dict(config.get("chain") or {})
    chain.update(data.pop("chain", None) or {})
    data["chain"] = chain

    atomic_write_json(task_dir(task_id) / "task.json", data)
    update_status(
        task_id,
        state="scheduled",
        retries=0,
        turns=0,
        tool_calls=0,
        subagents_running=0,
        background_tasks=[],
        context_tokens=None,
    )
    return task_id


def load_task(task_id: str) -> dict:
    """读 task.json。"""
    with open(task_dir(task_id) / "task.json", encoding="utf-8") as f:
        return json.load(f)


def chain_state(task_id: str) -> str:
    """前置链判定：该任务所在链（root_id 相同的所有任务，root_id 缺省即自身）
    最新一班（shift 最大）的 status.state。after 触发的调度用它当"到点"。

    任务不存在时抛 OSError（FileNotFoundError），调用方自己区分处理。
    """
    task = load_task(task_id)
    root = task.get("root_id") or task["id"]
    latest: dict | None = None
    for item in list_tasks():
        other = item["task"]
        if (other.get("root_id") or other["id"]) != root:
            continue
        if latest is None or int(other.get("shift") or 1) > int(latest.get("shift") or 1):
            latest = other
    if latest is None:  # 到不了：自己总在列表里
        return ""
    return read_status(latest["id"]).get("state") or ""


def next_pipeline_shift(task: dict) -> int:
    """从 pipeline coordinator（pipeline_id_of 对应的任务）原子领取下一个
    全局单调、唯一的 shift 号。

    S7.1 阻断一：以前各处本地算 `parent.shift + 1`——`_review_fix` 原地
    复用 held build 时不走这条路，只改 round 不改 shift，导致新一轮返工
    跟上一轮 review 撞了同一个 shift，`chain_state()` 的"扫最大 shift"
    猜错、成环。任何"这条流水线产生了新的当前班"的操作（造后继任务、或
    原地复用 held 任务开始新一轮）都必须从这里领号，不能再各自本地加一。
    这一步在 coordinator 的 status.json 锁内完成（modify_status），并发
    领号也不会撞号。

    S7.2 阻断一：第一次领号（coordinator 还没有 `pipeline_shift_seq`）不能
    固定从 1 开始——S7 上线前（S1-S6 时代）就可能已经有 scheduled/postponed/
    idle/chained 的旧任务落在这条 root_id/pipeline_id 链上，shift 早就推进
    到了某个更大的值。固定从 1+1=2 起会比这条链已有的最大 shift 还小，
    `chain_state()` 的 max-shift 扫描会继续认那个旧任务是"最新班"，新造的
    后继反而被判定成旧的。第一次领号时先在同一把锁内扫一遍这条 pipeline
    现存所有任务的最大 shift 当 bootstrap 起点，之后就是纯 `+1`，不用每次
    都扫（`list_tasks()`/`read_status()` 只读文件不加锁，在这把锁内调用不会
    死锁）。
    """
    coordinator_id = pipeline_id_of(task)
    pid = pipeline_id_of(task)

    def bump(status: dict) -> None:
        if "pipeline_shift_seq" not in status:
            existing_max = 1
            for item in list_tasks():
                if pipeline_id_of(item["task"]) == pid:
                    existing_max = max(existing_max, int(item["task"].get("shift") or 1))
            status["pipeline_shift_seq"] = existing_max
        status["pipeline_shift_seq"] = int(status["pipeline_shift_seq"]) + 1

    status = modify_status(coordinator_id, bump)
    return status["pipeline_shift_seq"]


# create_successor 里交接缺席时的兜底文案（与开工令一致）
NO_HANDOVER_TEXT = (
    "上一班没留交接。先看 git log / git status / 项目里的验收单或 reports "
    "目录判断进度，再接着做。"
)


def _copy_common_fields(parent_task: dict) -> dict:
    """换班/角色轮转共用的字段复制：title/project/runner/model/effort/
    task_text/worktree/review/root_id/pipeline_id/重试与守卫设置——build 与
    review 的后继都从这份基础上再补角色专属字段（shift/role/round/…）。

    S5 起：显式复制 worktree 与 review——旧式父任务（缺 worktree 字段）的
    后继必须保持 false，不能吃 create_task 的新任务缺省 true。runner/model/
    effort 复制的是这条流水线的**建造配方**（顶层字段），不随后继班的角色
    变化；review 角色要用的是 review 子对象，由 effective_* 系列取，不在
    这里改写顶层字段。
    """
    task: dict = {
        "title": parent_task["title"],
        "project": parent_task["project"],
        "runner": parent_task.get("runner") or "claude",
        "model": parent_task["model"],
        "effort": parent_task["effort"],
        "task_text": parent_task["task_text"],
        "root_id": parent_task.get("root_id") or parent_task["id"],
        "pipeline_id": pipeline_id_of(parent_task),
        "worktree": worktree_enabled(parent_task),
        "review": dict(parent_task.get("review") or {}),
    }
    if parent_task.get("retry_max") is not None:
        task["retry_max"] = parent_task["retry_max"]
    if parent_task.get("guards"):
        task["guards"] = parent_task["guards"]
    if parent_task.get("chain"):
        task["chain"] = parent_task["chain"]
    return task


def _copy_worktree_meta(parent_id: str, successor_id: str) -> None:
    """沿用父班的工作树三件元数据（有才写；父班还没建树就没有）——一条
    流水线从头到尾只有一棵树、一个分支、一个基准提交，任何后继（同角色
    续班或角色轮转）都要沿用，不许另建。"""
    parent_status = read_status(parent_id)
    meta = {
        key: parent_status[key]
        for key in ("worktree_path", "branch", "base_ref")
        if parent_status.get(key)
    }
    if meta:
        update_status(successor_id, **meta)


def create_same_role_successor(
    parent_task: dict, handover_text: str | None, config: dict,
) -> str:
    """同角色续班：上下文/额度到线 → 下一班接着干（Codex 同角色允许
    `codex resume`）。role/round 不变，shift 与 role_shift 各自 +1，
    pipeline_id 沿用；父任务状态改 chained 并记 successor_id（本班结束，
    后继进 scheduled）。

    - run_at = 现在（后继下一轮 tick 就能走预检）；
    - prompt_final = render(config.chain_template, task=…, shift=…, handover=…)，
      没交接就用 NO_HANDOVER_TEXT 兜底——续班模板占位符与首班共用四个
      （task/title/project_path/context_limit）再加 shift/handover。
    """
    parent_id = parent_task["id"]
    shift = next_pipeline_shift(parent_task)
    handover = handover_text if handover_text else NO_HANDOVER_TEXT
    task = _copy_common_fields(parent_task)
    task.update({
        "run_at": utc_now_iso(),
        "shift": shift,
        "role": role_of(parent_task),
        "round": round_of(parent_task),
        "role_shift": int(parent_task.get("role_shift") or 1) + 1,
        "parent_id": parent_id,
        "prompt_final": render(
            config["chain_template"],
            task=parent_task["task_text"],
            shift=shift,
            handover=handover,
            title=parent_task["title"],
            project_path=config["projects"][parent_task["project"]],
            context_limit=_context_limit_text(
                parent_task["model"], config, parent_task.get("runner") or "claude"
            ),
            worktree_instruction=(
                WORKTREE_INSTRUCTION if worktree_enabled(parent_task) else ""
            ),
        ),
    })
    successor_id = create_task(task, config)
    _copy_worktree_meta(parent_id, successor_id)
    update_status(parent_id, state="chained", successor_id=successor_id)
    return successor_id


def create_successor(parent_task: dict, handover_text: str | None, config: dict) -> str:
    """向后兼容别名：S1–S6 的换班入口，等价于 create_same_role_successor
    （S7 之前没有角色轮转，续班永远同角色）。S7 起的新调用点请直接用
    create_same_role_successor / create_cross_role_successor，语义更明确。
    """
    return create_same_role_successor(parent_task, handover_text, config)


def create_cross_role_successor(
    parent_task: dict, config: dict, *, role: str, round_: int,
    prompt_final: str, parent_next_state: str,
) -> str:
    """角色轮转的后继（build 收工 → 起同轮 review；review NEXT:fix → 下一轮
    build 返工）：新角色/新轮次，role_shift 重新从 1 起（只有同角色续班才在
    原有 role_shift 上 +1），shift 沿用全局单调序号，pipeline_id 沿用。

    跨角色永远开新会话（不传、也不查任何 resume 相关信息——resume 只在
    launcher 按"是否同角色续班"的判断里生效，这里造的后继天然不同角色）。

    prompt_final 由调用方渲染好整段传入（review_template / review_fix_template
    与 chain_template 占位符不同，不能共用 create_same_role_successor 那套
    渲染逻辑）。parent_next_state 必须由调用方显式给出——父班收工起审稿时
    该转 held（还在等审稿结果，不是"结束"），只有真正的角色轮转终点
    （如返工上限内的正常交接）才可能是 chained；本 helper 不揣测、不默认。
    """
    parent_id = parent_task["id"]
    shift = next_pipeline_shift(parent_task)
    task = _copy_common_fields(parent_task)
    task.update({
        "run_at": utc_now_iso(),
        "shift": shift,
        "role": role,
        "round": round_,
        "role_shift": 1,
        "parent_id": parent_id,
        "prompt_final": prompt_final,
    })
    successor_id = create_task(task, config)
    _copy_worktree_meta(parent_id, successor_id)
    update_status(parent_id, state=parent_next_state, successor_id=successor_id)
    return successor_id


def read_status(task_id: str) -> dict:
    """读 status.json；还没有就返回空 dict（读不加锁，写方原子替换）。"""
    path = task_dir(task_id) / "status.json"
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def list_tasks() -> list[dict]:
    """所有任务，按 run_at 升序；每项 {"task": ..., "status": ...}。"""
    out: list[dict] = []
    tasks_root = home() / "tasks"
    if tasks_root.is_dir():
        for entry in sorted(tasks_root.iterdir()):
            task_file = entry / "task.json"
            if not (entry.is_dir() and task_file.is_file()):
                continue
            with open(task_file, encoding="utf-8") as f:
                task = json.load(f)
            out.append({"task": task, "status": read_status(task["id"])})
    out.sort(key=lambda item: item["task"]["run_at"])
    return out


def modify_status(task_id: str, mutator) -> dict:
    """status.json 锁内读-改-写：读旧值 → mutator 原地改（返回值忽略）→
    盖 updated_at → 原子写 → 返回新 status。

    计数器增量（turns / tool_calls / subagents_running）必须整个走这里；
    锁外先 read_status 再 update_status 会被并发的 hook 进程吃掉增量。
    """
    d = task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            status = read_status(task_id)
            mutator(status)
            status["updated_at"] = utc_now_iso()
            atomic_write_json(d / "status.json", status)
            return status
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def update_status(task_id: str, **fields) -> dict:
    """status.json 的字段合并入口：锁内合并字段，自动盖 updated_at。"""
    return modify_status(task_id, lambda status: status.update(fields))


def append_event(task_id: str, text: str) -> None:
    """events.log 追加一行 `<UTC ISO>\\t<text>`，多进程并发也整行落盘。"""
    d = task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    line = f"{utc_now_iso()}\t{text}\n"
    with open(d / "events.log", "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def render(template: str, **vars) -> str:
    """只对已知占位符做字面替换，一遍扫描完成。

    不用 str.format——任务正文里可能有花括号；没认出的 {xxx} 与 {{ 原样保留。
    总review二 G8：以前是逐个 key 顺序 `str.replace`——先替换进去的值
    （比如 task_text/审稿意见，都是用户或模型写的自由文本）如果自己也含着
    另一个占位符的字面写法（如 `{title}`），会被后面轮到的那次替换二次
    展开，纯属误伤。现在按占位符名一次性正则替换，被替换进来的内容不会
    再被当成模板扫描第二遍。
    """
    if not vars:
        return template
    pattern = re.compile(
        "{(" + "|".join(re.escape(key) for key in vars) + ")}"
    )
    return pattern.sub(lambda m: str(vars[m.group(1)]), template)


def _context_limit_text(model: str, config: dict, runner: str) -> str:
    """{context_limit} 占位符的人话版本：S6.1 B3——Codex 模型查不到稳定水位
    时如实说"暂无稳定水位来源"，不能把 None 写进模板变成字面的 "None"，
    更不能悄悄套 Claude 的 default_context_limit 冒充一个假数字。"""
    limit = context_limit_for(model, config, runner=runner)
    return str(limit) if limit is not None else "暂无稳定水位来源"


def build_prompt(
    config: dict, title: str, project: str, model: str, task_text: str,
    worktree: bool = False, runner: str = "claude",
) -> str:
    """按 config.prompt_template 渲染最终提示词。

    网页 /api/preview 与 CLI cmd_add 共用这一套占位符
    （{task} {title} {project_path} {context_limit} {worktree_instruction}），
    保证所见即所发。工作树任务把 {worktree_instruction} 渲染成运行时安全前言；
    老式任务渲染成空串。{context_limit} 按 runner 对应的模型表查（S6.1 B3）。
    """
    return render(
        config["prompt_template"],
        task=task_text,
        title=title,
        project_path=config["projects"][project],
        context_limit=_context_limit_text(model, config, runner),
        worktree_instruction=WORKTREE_INSTRUCTION if worktree else "",
    )


# ---------- S7：审稿 / 返工提示词渲染 ----------

# S7.1 非阻断尾巴：config["review_template"]/config["review_fix_template"]
# 以前是直接索引——旧生产 config（S7 上线前部署的）缺这两个键时实测
# KeyError('review_template')。开工令要求代码自带 fallback，不能只靠部署
# 人工补键；内容原样抄自 config.example.json 的同名字段，不重新措辞。
DEFAULT_REVIEW_TEMPLATE = (
    "你在无人值守的定时会话里做代码审查，项目工作树 {project_path}"
    "（分支基准 {base_ref}）。任务：{title}\n\n{task}\n\n只读审查，不许改代码、"
    "不许 git commit。看改动请跑：{diff_command}\n\n施工班交接：\n{build_handover}"
    "\n\n{previous_review}这是第 {round} 轮审稿。通过标准：\n{criteria}\n\n"
    "{stop_build_hint}审完把完整意见写成本次最终回复的正文（不要写文件、不要用"
    " shell 重定向），最后一个非空行严格写成三选一：`NEXT: done`（通过）、"
    "`NEXT: fix`（退回，正文说清改什么/为什么/是小改还是重写）或 `NEXT: pending`"
    "（额度到线意见没写完，不计本轮）。"
)
DEFAULT_REVIEW_FIX_TEMPLATE = (
    "你在无人值守的定时会话里工作，项目目录 {project_path}。任务：{title}\n\n"
    "{task}\n\n这是第 {round} 轮返工，审稿意见如下：\n{review}\n\n"
    "{worktree_instruction}先核对审稿意见里说的问题，逐条确认改完再收尾。没有人"
    "在场：遇到问题按合理判断继续，不要停下来等确认。完成或换班时照常写交接，"
    "末行写 NEXT: done 或 NEXT: continue。"
)


def render_review_prompt(
    config: dict, task: dict, *, workdir: str, base_ref: str, diff_command: str,
    build_handover: str | None, previous_review: str, round_: int,
    stop_build_hint: str = "",
) -> str:
    """渲染审稿班的提示词（config.review_template）。

    只给固定参数数组语义的 git diff 命令（{diff_command}），不把整份 diff
    塞进提示词——审稿班自己在只读工具面里跑这条命令看改动。

    S7.5 阻断：`{project_path}` 必须是调用方传入的**施工班工作树绝对路径**
    （`status["worktree_path"]`），不能在这里从 `config["projects"][task["project"]]`
    推导——那是主签出目录，审稿会话的 cwd/信任根其实是工作树，指错目录会让
    审稿人读到未修的旧代码、永远判 fix（真机 smoke 抓到的死循环）。
    """
    review = review_config(task, config)
    criteria = review.get("criteria_text") or config.get("review_criteria_text") or ""
    return render(
        config.get("review_template") or DEFAULT_REVIEW_TEMPLATE,
        task=task["task_text"],
        title=task["title"],
        project_path=workdir,
        base_ref=base_ref,
        diff_command=diff_command,
        build_handover=build_handover if build_handover else NO_HANDOVER_TEXT,
        previous_review=previous_review or "",
        round=round_,
        criteria=criteria,
        stop_build_hint=stop_build_hint,
    )


def render_review_fix_prompt(
    config: dict, task: dict, *, workdir: str, round_: int, review_text: str,
) -> str:
    """渲染返工班的提示词（config.review_fix_template）：原任务书 + 第几轮
    返工 + 完整审稿意见 + 工作树安全前言（返工班永远是工作树任务）。

    S7.5 阻断：`workdir` 同 `render_review_prompt`——调用方传施工班自己的
    工作树路径，不在这里推导主目录。
    """
    return render(
        config.get("review_fix_template") or DEFAULT_REVIEW_FIX_TEMPLATE,
        task=task["task_text"],
        title=task["title"],
        project_path=workdir,
        round=round_,
        review=review_text,
        context_limit=_context_limit_text(
            task.get("model", ""), config, task.get("runner") or "claude"
        ),
        worktree_instruction=WORKTREE_INSTRUCTION,
    )
