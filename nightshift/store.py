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
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .context import context_limit_for

__all__ = [
    "ConfigMissing",
    "ENDED_STATES",
    "STATES",
    "WORKTREE_INSTRUCTION",
    "append_event",
    "atomic_write_json",
    "atomic_write_text",
    "build_prompt",
    "chain_state",
    "create_successor",
    "create_task",
    "ensure_dirs",
    "home",
    "list_tasks",
    "load_config",
    "load_task",
    "modify_status",
    "new_task_id",
    "read_status",
    "render",
    "task_dir",
    "update_status",
    "utc_now_iso",
    "validate_task",
    "worktree_enabled",
]


class ConfigMissing(Exception):
    """数据目录里没有 config.json。"""


# 任务状态全集（设计稿 §3 状态机）
STATES = (
    "scheduled",
    "postponed",
    "launching",
    "working",
    "waiting_background",
    "waiting_wakeup",
    "idle",
    "chained",
    "exited",
    "finished",
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
# review.merge_policy 只认这两个值（S7 扩写同一个 review 对象，不做二次迁移）
_MERGE_POLICIES = ("manual", "auto")

# trigger.type == "after" 且 when == "ended" 时，前置链最新一班落在这些状态
# 就算"已结束"（调度器与网页共用这一个定义）
ENDED_STATES = (
    "finished",
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
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
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
    """
    for key in _REQUIRED_FIELDS:
        if key == "run_at":
            continue  # after 任务允许缺，最后统一判
        if not task.get(key):
            raise ValueError(f"缺少必填字段：{key}")
    if task["project"] not in config["projects"]:
        raise ValueError(f"project 不在 config.projects 里：{task['project']}")
    if task["effort"] not in config["efforts"]:
        raise ValueError(f"effort 不在 config.efforts 里：{task['effort']}")

    for key in ("guards", "chain"):
        value = task.get(key)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{key} 必须是对象")
    auto = (task.get("guards") or {}).get("auto_interrupt_minutes")
    if auto is not None and (
        isinstance(auto, bool) or not isinstance(auto, int) or auto <= 0
    ):
        raise ValueError("guards.auto_interrupt_minutes 必须是正整数")

    # S5：worktree 只有显式 true/false 两种；review 占住形状但 enabled 必须 false
    if task.get("worktree") is not None and not isinstance(task.get("worktree"), bool):
        raise ValueError("worktree 必须是布尔值")
    review = task.get("review")
    if review is not None:
        if not isinstance(review, dict):
            raise ValueError("review 必须是对象")
        unknown = sorted(set(review) - {"enabled", "merge_policy"})
        if unknown:
            raise ValueError(f"review 只认 enabled / merge_policy，多出：{'、'.join(unknown)}")
        enabled = review.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("review.enabled 必须是布尔值")
        if enabled:
            raise ValueError("联动审稿要到 S7 才开放，现在只能 enabled=false")
        policy = review.get("merge_policy", "manual")
        if policy not in _MERGE_POLICIES:
            raise ValueError("review.merge_policy 只认 manual / auto")

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
            if not pre_id or task_id == str(pre_id) or not (
                task_dir(str(pre_id)) / "task.json"
            ).is_file():
                raise ValueError(f"trigger.task 必须是已存在的任务 id：{pre_id}")
            if trigger.get("when") not in ("finished", "ended"):
                raise ValueError("trigger.when 只认 finished / ended")
        else:
            raise ValueError("trigger.type 只认 time / after")

    run_at = task.get("run_at")
    if run_at:
        if not (isinstance(run_at, str) and run_at.endswith("Z")):
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
    if data.get("worktree") is None:
        data["worktree"] = True  # 新任务缺省建树
    validate_task(data, config)  # 先原样校验：review 形状不对在这里报人话错误
    review = data.get("review")
    if not isinstance(review, dict):
        data["review"] = {"enabled": False, "merge_policy": "manual"}
    else:
        data["review"] = {
            "enabled": bool(review.get("enabled", False)),
            "merge_policy": review.get("merge_policy") or "manual",
        }
    if not data.get("run_at"):
        data["run_at"] = utc_now_iso()  # after 任务：只当排序用

    task_id = new_task_id()
    data["id"] = task_id
    data["created_at"] = utc_now_iso()
    data.setdefault("shift", 1)
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


# create_successor 里交接缺席时的兜底文案（与开工令一致）
NO_HANDOVER_TEXT = (
    "上一班没留交接。先看 git log / git status / 项目里的验收单或 reports "
    "目录判断进度，再接着做。"
)


def create_successor(parent_task: dict, handover_text: str | None, config: dict) -> str:
    """换班：按父任务造后继任务并落盘，返回后继任务 id。

    - 复制 title/project/model/effort/task_text/guards/chain/retry_max；
    - S5：显式复制 worktree 与 review，并让后继班沿用父班的
      worktree_path / branch / base_ref——一条换班链从头到尾只有一棵树、
      一个分支、一个基准提交；
    - shift = 父 shift + 1，parent_id / root_id（根任务的 root_id 是它自己）；
    - run_at = 现在（后继下一轮 tick 就能走预检）；
    - prompt_final = render(config.chain_template, task=…, shift=…, handover=…)，
      没交接就用 NO_HANDOVER_TEXT 兜底；
    - 父任务状态改 chained 并记 successor_id（本班结束，后继进 scheduled）。
    """
    parent_id = parent_task["id"]
    shift = int(parent_task.get("shift") or 1) + 1
    handover = handover_text if handover_text else NO_HANDOVER_TEXT
    task: dict = {
        "title": parent_task["title"],
        "project": parent_task["project"],
        "model": parent_task["model"],
        "effort": parent_task["effort"],
        "run_at": utc_now_iso(),
        "task_text": parent_task["task_text"],
        # 续班模板可用的占位符 = 首班模板那四个 + shift/handover，别让第 2 班少掉开场叮嘱
        "prompt_final": render(
            config["chain_template"],
            task=parent_task["task_text"],
            shift=shift,
            handover=handover,
            title=parent_task["title"],
            project_path=config["projects"][parent_task["project"]],
            context_limit=context_limit_for(parent_task["model"], config),
            worktree_instruction=(
                WORKTREE_INSTRUCTION if worktree_enabled(parent_task) else ""
            ),
        ),
        "shift": shift,
        "parent_id": parent_id,
        "root_id": parent_task.get("root_id") or parent_id,
        # S5：显式复制——旧式父任务（缺 worktree 字段）的后继必须保持 false，
        # 不能吃 create_task 的新任务缺省 true
        "worktree": worktree_enabled(parent_task),
        "review": dict(parent_task.get("review") or {}),
    }
    if parent_task.get("retry_max") is not None:
        task["retry_max"] = parent_task["retry_max"]
    if parent_task.get("guards"):
        task["guards"] = parent_task["guards"]
    if parent_task.get("chain"):
        task["chain"] = parent_task["chain"]
    successor_id = create_task(task, config)
    # 沿用父班的工作树三件元数据（有才写；父班还没建树就没有）
    parent_status = read_status(parent_id)
    meta = {
        key: parent_status[key]
        for key in ("worktree_path", "branch", "base_ref")
        if parent_status.get(key)
    }
    if meta:
        update_status(successor_id, **meta)
    update_status(parent_id, state="chained", successor_id=successor_id)
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
    """只对已知占位符做字面替换。

    不用 str.format——任务正文里可能有花括号；没认出的 {xxx} 与 {{ 原样保留。
    """
    out = template
    for key, value in vars.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def build_prompt(
    config: dict, title: str, project: str, model: str, task_text: str,
    worktree: bool = False,
) -> str:
    """按 config.prompt_template 渲染最终提示词。

    网页 /api/preview 与 CLI cmd_add 共用这一套占位符
    （{task} {title} {project_path} {context_limit} {worktree_instruction}），
    保证所见即所发。工作树任务把 {worktree_instruction} 渲染成运行时安全前言；
    老式任务渲染成空串。
    """
    return render(
        config["prompt_template"],
        task=task_text,
        title=title,
        project_path=config["projects"][project],
        context_limit=context_limit_for(model, config),
        worktree_instruction=WORKTREE_INSTRUCTION if worktree else "",
    )
