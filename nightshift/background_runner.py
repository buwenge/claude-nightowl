"""后台进程登记簿（F12）：`python3 -m nightshift.background_runner start -- <程序> <参数...>`。

Codex 不许裸用 `&`/`nohup`/`setsid` 起长任务后就地结束 turn——那样谁都不知道
它有没有跑完。**也不许在沙箱内用双 fork/setsid 试图脱离这次工具调用**：真机
实证（Fathom + Sol 独立复现，见
`/root/CC/moving/reports/夜班-S6-F12沙箱清理机制答复.md`）证实每次
sandboxed exec 都在独立 PID namespace 里（`codex-linux-sandbox` 是 PID 1），
工具调用被判定完成后这个 namespace 会被回收，fork/setsid/挪 cgroup 都换不了
进程所在的 namespace，逃不掉。也不许让沙箱外的常驻进程按请求文件执行任意
argv 来绕——那等于让 workspace-write 沙箱里的模型指挥宿主机代为执行任意命令，
是自制的沙箱逃逸（confused deputy），比"进程被杀"更糟。

正确做法是不逃：wrapper 自己留在沙箱里前台直接持有并等待目标进程，只把
`{background_id, result_path}` 尽早打到 stdout 并 flush；Codex 自己的
unified-exec 有"yield"机制，能在命令还没跑完时就把这次工具调用的 session id
先还给模型，模型据此结束这一轮 turn，但沙箱内的进程其实还在跑——这是 Codex
官方支持的行为，不是我们自己想办法绕的。scheduler 发现"已完成但未通知"后对
原 Codex 窗口 send-keys 主动唤醒——见 scheduler.py 的
`_reconcile_codex_background`；notify（codex_notify.py）只管 turn 完成记账，
绝不冒充这条链路。

沙箱内记录的 pid 只在那一次 PID namespace 里有意义，**不能由后来的 exec 或
宿主 scheduler 按 `/proc/<pid>` 操作**——换一次工具调用就是全新的 namespace，
旧 pid 数字可能被回收给完全无关的进程。因此 `sandbox_pid` 只做诊断展示；
"停止"改成请求-响应：`stop` 只原子写 `stop_requested_at`，仍在跑、仍持有
`Popen` 对象的原 wrapper 自己在等待循环里发现请求，终止自己直接持有的子进程。

登记簿是每任务一份 JSON：`<task_dir>/background/registry.json`（总review
F12：从 `<task_dir>/background.json` 挪进 `background/` 子目录——监理
9/2 实测坐实 Codex 的 workspace-write 沙箱对 task_dir 本身是只读的
（`codex sandbox -c 'sandbox_mode="workspace-write"' -- touch
~/.nightshift/x` 报 Read-only file system），只有 `<task_dir>/background`
这个子目录会被 launcher 起跑前预建、且 Codex 命令行额外放开写权限，
登记簿必须落在这个目录下才写得进去；F12 在生产从未真正工作过），
`background/.lock` 文件锁串行化读改写（wrapper 与 CLI 主进程会并发写）。
命令全文只存安全摘要（可能含秘密），输出尾部限长且不落原始密钥类内容的
假设由调用方负责（这层只截断，不脱敏）。
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from . import store

__all__ = [
    "background_dir",
    "load_registry",
    "main",
    "modify_registry",
    "registry_path",
]

_ARGV_SUMMARY_MAX = 200
_OUTPUT_TAIL_MAX = 4000
_HEARTBEAT_INTERVAL_SECONDS = 1.0
_STOP_GRACE_SECONDS = 5


def registry_path(task_id: str) -> Path:
    # 总review F12：从 <task_dir>/background.json 挪进 background/ 子目录
    # ——Codex 沙箱对 task_dir 本身只读，只有这个子目录会被预建并放开写权限。
    return background_dir(task_id) / "registry.json"


def background_dir(task_id: str) -> Path:
    return store.task_dir(task_id) / "background"


def load_registry(task_id: str) -> dict:
    """读整份登记簿；没有/坏 JSON/不是对象都返回空字典，不炸。"""
    path = registry_path(task_id)
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def modify_registry(task_id: str, mutator) -> dict:
    """锁内读-改-写整份登记簿（同任务多个后台项并发落盘不许互相覆盖）。

    总review F12：mkdir 的是 background_dir(task_id)（`<task_dir>/
    background`），不是 task_dir 本身——Codex 沙箱内 task_dir 只读，
    这个子目录才是被预建、放开写权限的那个（见 launcher.launch）。锁
    文件同样挪进这个子目录：`background/.lock`。
    """
    d = background_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = load_registry(task_id)
            mutator(data)
            store.atomic_write_json(registry_path(task_id), data)
            return data
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_tail(path: Path, limit: int) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def _run_foreground(task_id: str, background_id: str, argv: list[str], output_path: Path) -> None:
    """wrapper 本体：不 fork、不 setsid、不脱离——直接在当前（沙箱内）进程
    Popen 目标命令并前台等待，周期写心跳，发现 stop_requested_at 就终止自己
    持有的子进程。这个函数只在 `cmd_start` 打完 stdout 之后调用，异常也不能
    让它裸崩——崩了就永远没人把这一项标成完成，任务会卡死在 waiting_background。
    """
    exit_code: int | None = None
    stopped = False
    proc: subprocess.Popen | None = None
    try:
        with open(output_path, "wb") as out:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            )

            def set_sandbox_pid(data: dict) -> None:
                rec = data.get(background_id)
                if rec is not None:
                    rec["sandbox_pid"] = proc.pid
                    rec["heartbeat_at"] = store.utc_now_iso()

            modify_registry(task_id, set_sandbox_pid)

            while True:
                try:
                    exit_code = proc.wait(timeout=_HEARTBEAT_INTERVAL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass

                registry = load_registry(task_id)
                rec = registry.get(background_id) or {}
                if rec.get("stop_requested_at") and not stopped:
                    stopped = True
                    proc.terminate()
                    try:
                        exit_code = proc.wait(timeout=_STOP_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        exit_code = proc.wait()
                    break

                def touch_heartbeat(data: dict) -> None:
                    rec2 = data.get(background_id)
                    if rec2 is not None:
                        rec2["heartbeat_at"] = store.utc_now_iso()

                modify_registry(task_id, touch_heartbeat)
    except Exception as exc:  # wrapper 自己出岔子：也要留痕，不能悄悄消失
        exit_code = -1
        try:
            with open(output_path, "ab") as out:
                out.write(f"\n[nightshift background_runner 监工异常：{exc!r}]\n".encode())
        except OSError:
            pass

    tail = _read_tail(output_path, _OUTPUT_TAIL_MAX)
    finished_at = store.utc_now_iso()
    final_state = "stopped" if stopped else "finished"

    def mark_finished(data: dict) -> None:
        rec = data.get(background_id)
        if rec is None:
            return
        rec["state"] = final_state
        rec["exit_code"] = exit_code
        rec["finished_at"] = finished_at
        rec["output_tail"] = tail
        rec["heartbeat_at"] = finished_at

    modify_registry(task_id, mark_finished)


def _task_id_from_env() -> str | None:
    return os.environ.get("NIGHTOWL_TASK_ID") or None


def cmd_start(argv: list[str]) -> int:
    task_id = _task_id_from_env()
    if not task_id:
        print("NIGHTOWL_TASK_ID 未设置，这个命令只能在 nightshift 起的会话里用", file=sys.stderr)
        return 2
    if "--" not in argv:
        print("用法：background_runner start -- <程序> <参数...>", file=sys.stderr)
        return 2
    idx = argv.index("--")
    command = argv[idx + 1:]
    if not command:
        print("-- 后面要跟真正的命令", file=sys.stderr)
        return 2

    background_id = f"bg-{secrets.token_hex(4)}"
    d = background_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    output_path = d / f"{background_id}.log"
    started_at = store.utc_now_iso()
    argv_summary = " ".join(command)
    if len(argv_summary) > _ARGV_SUMMARY_MAX:
        argv_summary = argv_summary[:_ARGV_SUMMARY_MAX] + "…"

    # S6.1 A4：登记时把当时的 thread_id 也记下来。窗口 id 理论上可能被换掉
    # 的任务复用（同一个 @N 编号先后属于不同任务/session），scheduler 通知
    # 前要拿它跟 status.json 当前的 thread_id 对答案，不能只信 window_alive——
    # 那只能证明"这个窗口号还在"，证明不了"这个窗口现在跑的还是当初那个会话"。
    thread_id_at_start = store.read_status(task_id).get("thread_id")

    def register(data: dict) -> None:
        data[background_id] = {
            "task_id": task_id,
            "background_id": background_id,
            "sandbox_pid": None,  # 仅诊断：只在起跑这次 PID namespace 里有意义，不可跨 exec kill()
            "thread_id_at_start": thread_id_at_start,
            "argv_summary": argv_summary,
            "started_at": started_at,
            "heartbeat_at": started_at,
            "stop_requested_at": None,
            "result_path": str(output_path),
            "state": "running",
            "exit_code": None,
            "finished_at": None,
            "output_tail": None,
            "notification_state": "pending",
            "notified_at": None,
        }

    modify_registry(task_id, register)
    # 尽早把 id 打到 stdout 并 flush：Codex unified-exec 的 yield 机制据此能在
    # 目标命令还没跑完时就把这次工具调用的 session id 先还给模型，模型可以
    # 结束这一轮 turn；这个函数本身接下来会在沙箱内前台一直等到命令跑完。
    print(json.dumps({"background_id": background_id, "result_path": str(output_path)}, ensure_ascii=False))
    sys.stdout.flush()
    _run_foreground(task_id, background_id, command, output_path)
    return 0


def cmd_list(argv: list[str]) -> int:
    task_id = _task_id_from_env()
    if not task_id:
        print("NIGHTOWL_TASK_ID 未设置，这个命令只能在 nightshift 起的会话里用", file=sys.stderr)
        return 2
    print(json.dumps(list(load_registry(task_id).values()), ensure_ascii=False, indent=2))
    return 0


def cmd_stop(argv: list[str]) -> int:
    task_id = _task_id_from_env()
    if not task_id:
        print("NIGHTOWL_TASK_ID 未设置，这个命令只能在 nightshift 起的会话里用", file=sys.stderr)
        return 2
    if not argv:
        print("用法：background_runner stop <background_id>", file=sys.stderr)
        return 2
    background_id = argv[0]
    registry = load_registry(task_id)
    rec = registry.get(background_id)
    if rec is None:
        print(f"没有这个 background_id：{background_id}", file=sys.stderr)
        return 1
    if rec.get("state") != "running":
        print("这一项已经不在跑了")
        return 0

    # 不按 pid 发信号（那是上一次 PID namespace 里的编号，这次 exec 里对不上
    # 号）。只原子写停止请求，仍活着、仍持有 Popen 对象的原 wrapper 自己在
    # 等待循环里发现并终止它直接持有的子进程。
    def request_stop(data: dict) -> None:
        r = data.get(background_id)
        if r is not None and r.get("state") == "running":
            r["stop_requested_at"] = store.utc_now_iso()

    modify_registry(task_id, request_stop)
    print(f"已请求停止：{background_id}（由仍在运行的原 wrapper 处理，不是立即生效）")
    return 0


_COMMANDS = {"start": cmd_start, "list": cmd_list, "stop": cmd_stop}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in _COMMANDS:
        print(f"用法：background_runner <{'|'.join(_COMMANDS)}> ...", file=sys.stderr)
        return 2
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
