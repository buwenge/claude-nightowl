"""background_runner.py 的测试：登记簿读写、前台 wrapper 心跳/完成/停止请求。

S6④ 改法（Sol 沙箱清理机制答复后的定案，见
`/root/CC/moving/reports/夜班-S6-F12沙箱清理机制答复.md`）：wrapper 不再
双 fork/setsid 脱离，而是自己留在（模拟的）沙箱内前台直接持有并等待目标
进程；`start` 尽早把 `{background_id, result_path}` 打到 stdout 并 flush，
随后这个 OS 进程本身继续阻塞直到目标命令跑完——这里测的就是"尽早吐 id"
而不是"CLI 立刻退出"（后者已经不是新设计的行为）。

真机靶测（真 Codex、验证跨 Stop/turn 边界仍存活）另见
reports/夜班-S6-靶测记录.md；这里全部离线、全部用 0.1~3 秒的假进程，
不联网、不花钱。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nightshift import background_runner as bgr
from nightshift import store

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    return tmp_path


def make_task(task_id: str = "20260830-000000-aaaa") -> str:
    d = store.task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    store.atomic_write_json(d / "task.json", {"id": task_id, "title": "x"})
    return task_id


def _env(task_id: str | None) -> dict:
    env = dict(os.environ)
    env["NIGHTSHIFT_HOME"] = os.environ["NIGHTSHIFT_HOME"]
    env["PYTHONPATH"] = str(REPO_ROOT)
    if task_id is not None:
        env["NIGHTOWL_TASK_ID"] = task_id
    else:
        env.pop("NIGHTOWL_TASK_ID", None)
    return env


def run_cli(argv: list[str], task_id: str | None, timeout: float = 10.0):
    """跑一次 CLI 直到它自己退出（新设计里 start 会阻塞到目标命令跑完）。"""
    return subprocess.run(
        [sys.executable, "-m", "nightshift.background_runner", *argv],
        capture_output=True, text=True, env=_env(task_id), timeout=timeout,
    )


def start_bg(argv: list[str], task_id: str, timeout: float = 10.0):
    """跑 `start`，只读第一行 stdout 就返回（不等目标命令跑完）——用来验证
    "尽早吐 background_id、随后仍在前台跑"这条核心行为。调用方负责后续
    `proc.wait(timeout=...)` 等这个 wrapper 进程自己退出。
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "nightshift.background_runner", "start", "--", *argv],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(task_id),
    )
    line = proc.stdout.readline()
    out = json.loads(line)
    return proc, out["background_id"], out["result_path"]


def wait_until_state(task_id: str, background_id: str, states=("finished",), timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = bgr.load_registry(task_id).get(background_id)
        if rec and rec.get("state") in states:
            return rec
        time.sleep(0.05)
    raise AssertionError(f"{timeout}s 没等到 {background_id} 变成 {states}")


# ---------- 登记簿读写 ----------


def test_load_registry_missing_and_bad_json(tmp_path):
    task_id = make_task()
    assert bgr.load_registry(task_id) == {}
    bgr.registry_path(task_id).write_text("不是json", encoding="utf-8")
    assert bgr.load_registry(task_id) == {}


def test_modify_registry_roundtrip():
    task_id = make_task()

    def add(data):
        data["bg-1"] = {"state": "running"}

    bgr.modify_registry(task_id, add)
    assert bgr.load_registry(task_id) == {"bg-1": {"state": "running"}}


# ---------- CLI 参数校验 ----------


def test_cmd_start_requires_task_id_env():
    proc = run_cli(["start", "--", "echo", "hi"], task_id=None)
    assert proc.returncode == 2
    assert "NIGHTOWL_TASK_ID" in proc.stderr


def test_cmd_start_requires_double_dash_and_command():
    task_id = make_task()
    proc = run_cli(["start", "echo", "hi"], task_id=task_id)
    assert proc.returncode == 2
    proc = run_cli(["start", "--"], task_id=task_id)
    assert proc.returncode == 2


# ---------- 核心行为：尽早吐 id、前台持有到完成 ----------


def test_start_prints_id_before_target_finishes_then_stays_foreground(tmp_path):
    task_id = make_task()
    t0 = time.time()
    proc, background_id, result_path = start_bg(["sleep", "1.5"], task_id)
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"应该在目标命令跑完前就吐出 id，实际等了 {elapsed:.2f}s"
    assert background_id.startswith("bg-")

    # 此时目标应该还在跑：state 仍是 running，wrapper 这个 OS 进程也还没退出
    rec = bgr.load_registry(task_id)[background_id]
    assert rec["state"] == "running"
    assert proc.poll() is None, "wrapper 不该在目标命令跑完前就自己退出"

    # wrapper 前台阻塞直到目标跑完才退出（这正是新设计要的：不脱离、不 fork）
    ret = proc.wait(timeout=10)
    assert ret == 0
    finished = wait_until_state(task_id, background_id)
    assert finished["exit_code"] == 0
    assert finished["result_path"] == result_path
    assert finished["finished_at"] is not None
    assert finished["sandbox_pid"] is not None


def test_cmd_start_success_writes_output_and_argv_summary(tmp_path):
    task_id = make_task()
    marker = tmp_path / "canary.txt"
    proc = run_cli(
        ["start", "--", "bash", "-c", f"sleep 0.1; echo ok > {marker}; echo done"],
        task_id=task_id,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    background_id = out["background_id"]

    finished = bgr.load_registry(task_id)[background_id]
    assert finished["state"] == "finished"  # run_cli 已经等到 wrapper 自己退出
    assert finished["exit_code"] == 0
    assert "done" in finished["output_tail"]
    assert finished["argv_summary"].startswith("bash")
    assert finished["notification_state"] == "pending"
    assert marker.is_file() and marker.read_text().strip() == "ok"


def test_cmd_start_nonzero_exit_recorded():
    task_id = make_task()
    proc = run_cli(["start", "--", "bash", "-c", "exit 7"], task_id=task_id)
    background_id = json.loads(proc.stdout)["background_id"]
    finished = bgr.load_registry(task_id)[background_id]
    assert finished["exit_code"] == 7


def test_cmd_start_monitor_exception_still_records_finished(tmp_path):
    """目标命令是个不存在的可执行文件：Popen 会抛异常，wrapper 自己也不能
    崩掉不留痕——必须原子落一个 finished（exit_code=-1）方便 scheduler 别永
    远卡着。"""
    task_id = make_task()
    proc = run_cli(["start", "--", "/nonexistent/nope-binary-xyz"], task_id=task_id)
    background_id = json.loads(proc.stdout)["background_id"]
    finished = bgr.load_registry(task_id)[background_id]
    assert finished["state"] == "finished"
    assert finished["exit_code"] == -1
    assert "监工异常" in finished["output_tail"]


def test_heartbeat_updates_while_running(tmp_path):
    task_id = make_task()
    proc, background_id, _ = start_bg(["sleep", "2.5"], task_id)
    time.sleep(0.3)
    hb1 = bgr.load_registry(task_id)[background_id]["heartbeat_at"]
    assert hb1 is not None
    time.sleep(1.3)
    hb2 = bgr.load_registry(task_id)[background_id]["heartbeat_at"]
    assert hb2 != hb1, "心跳应该在等待循环里持续刷新，不是起跑写一次就不动了"
    proc.wait(timeout=10)


# ---------- list ----------


def test_cmd_list_shows_registered_items():
    task_id = make_task()
    proc = run_cli(["start", "--", "sleep", "0.1"], task_id=task_id)
    background_id = json.loads(proc.stdout)["background_id"]
    proc = run_cli(["list"], task_id=task_id)
    assert proc.returncode == 0
    items = json.loads(proc.stdout)
    assert any(item["background_id"] == background_id for item in items)


# ---------- stop：请求-响应，不跨 exec 按 pid 发信号 ----------


def test_cmd_stop_request_is_consumed_by_live_wrapper_not_pid_kill(tmp_path):
    task_id = make_task()
    proc, background_id, _ = start_bg(["sleep", "30"], task_id)
    time.sleep(0.3)  # 等第一次心跳/sandbox_pid 落盘

    stop_proc = run_cli(["stop", background_id], task_id=task_id)
    assert stop_proc.returncode == 0
    assert "已请求停止" in stop_proc.stdout

    rec = bgr.load_registry(task_id)[background_id]
    assert rec.get("stop_requested_at") is not None

    # 仍活着的原 wrapper 自己在等待循环里发现请求、终止自己持有的子进程、
    # 落 state=stopped，然后这个 OS 进程本身退出——不是被外部按 pid kill 的。
    ret = proc.wait(timeout=10)
    assert ret == 0
    finished = wait_until_state(task_id, background_id, states=("stopped",))
    assert finished["state"] == "stopped"


def test_cmd_stop_unknown_id():
    task_id = make_task()
    proc = run_cli(["stop", "bg-nope"], task_id=task_id)
    assert proc.returncode == 1


def test_cmd_stop_already_finished_is_noop():
    task_id = make_task()

    def add(data):
        data["bg-done"] = {"state": "finished"}

    bgr.modify_registry(task_id, add)
    proc = run_cli(["stop", "bg-done"], task_id=task_id)
    assert proc.returncode == 0
    # 已完成的项不该被打上 stop_requested_at（没有活着的 wrapper 会消费它）
    assert bgr.load_registry(task_id)["bg-done"].get("stop_requested_at") is None


def test_cmd_stop_lost_wrapper_just_records_request_does_not_crash(tmp_path):
    """没有活着的 wrapper 去消费这条请求（比如原沙箱那次 exec 真的丢了）：
    stop 只管原子写 stop_requested_at，不假装它已经停了，也不崩。判定"这项
    大概率已经没人管了"是 scheduler 心跳超时那一层的事，不是这里的事。"""
    task_id = make_task()

    def add(data):
        data["bg-orphan"] = {"state": "running", "heartbeat_at": "2020-01-01T00:00:00Z"}

    bgr.modify_registry(task_id, add)
    proc = run_cli(["stop", "bg-orphan"], task_id=task_id)
    assert proc.returncode == 0
    rec = bgr.load_registry(task_id)["bg-orphan"]
    assert rec["stop_requested_at"] is not None
    assert rec["state"] == "running"  # 没人消费，state 保持不变，不能谎报已停


# ---------- 并发：同任务多个后台项互不覆盖 ----------


def test_concurrent_background_items_do_not_clobber_each_other(tmp_path):
    task_id = make_task()
    p1, id1, _ = start_bg(["sleep", "0.4"], task_id)
    p2, id2, _ = start_bg(["sleep", "0.4"], task_id)
    assert id1 != id2
    p1.wait(timeout=10)
    p2.wait(timeout=10)
    wait_until_state(task_id, id1)
    wait_until_state(task_id, id2)
    registry = bgr.load_registry(task_id)
    assert set(registry) == {id1, id2}
    assert registry[id1]["state"] == "finished"
    assert registry[id2]["state"] == "finished"
