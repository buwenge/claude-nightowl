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
    "STATES",
    "append_event",
    "atomic_write_json",
    "atomic_write_text",
    "build_prompt",
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
    "idle",
    "chained",
    "exited",
    "finished",
    "failed",
    "cancelled",
    "needs_attention",
    "chain_exhausted",
)

# create_task 必填字段
_REQUIRED_FIELDS = (
    "title",
    "project",
    "model",
    "effort",
    "run_at",
    "task_text",
    "prompt_final",
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


def create_task(task: dict, config: dict) -> str:
    """校验并落盘一个新任务（task.json + 初始 status.json），返回任务 id。"""
    for key in _REQUIRED_FIELDS:
        if not task.get(key):
            raise ValueError(f"缺少必填字段：{key}")
    if task["project"] not in config["projects"]:
        raise ValueError(f"project 不在 config.projects 里：{task['project']}")
    if task["effort"] not in config["efforts"]:
        raise ValueError(f"effort 不在 config.efforts 里：{task['effort']}")
    run_at = task["run_at"]
    if not (isinstance(run_at, str) and run_at.endswith("Z")):
        raise ValueError("run_at 必须是 Z 结尾的 ISO UTC 时间，如 2026-08-27T18:00:00Z")
    try:
        datetime.fromisoformat(run_at[:-1])
    except ValueError:
        raise ValueError(f"run_at 不是合法的 ISO 时间：{run_at}") from None

    task_id = new_task_id()
    data = dict(task)
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


def build_prompt(config: dict, title: str, project: str, model: str, task_text: str) -> str:
    """按 config.prompt_template 渲染最终提示词。

    网页 /api/preview 与 CLI cmd_add 共用这一套占位符
    （{task} {title} {project_path} {context_limit}），保证所见即所发。
    """
    return render(
        config["prompt_template"],
        task=task_text,
        title=title,
        project_path=config["projects"][project],
        context_limit=context_limit_for(model, config),
    )
