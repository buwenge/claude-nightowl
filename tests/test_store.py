"""store.py 的测试：建任务、列任务、并发写状态、模板渲染。"""

import json
import multiprocessing
import re
import time
from pathlib import Path

import pytest

from nightshift import store

CONFIG = {
    "projects": {"demo": "/home/user/projects/demo"},
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
    "chain": {"max_windows": 3, "on_no_handover": "continue"},
}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))


def make_task(**over):
    task = {
        "title": "演示任务",
        "project": "demo",
        "model": "claude-fable-5",
        "effort": "high",
        "run_at": "2026-08-27T18:00:00Z",
        "task_text": "干点活，正文里有 {不认识的} 花括号",
        "prompt_final": "拼好的完整提示词",
    }
    task.update(over)
    return task


def test_new_task_id_format():
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", store.new_task_id())


def test_load_config_missing():
    with pytest.raises(store.ConfigMissing):
        store.load_config()


def test_create_list_update():
    tid = store.create_task(make_task(), CONFIG)
    assert store.task_dir(tid).is_dir()

    status = store.read_status(tid)
    assert status["state"] == "scheduled"
    assert status["retries"] == 0
    assert status["turns"] == 0
    assert status["tool_calls"] == 0
    assert status["subagents_running"] == 0
    assert status["background_tasks"] == []
    assert status["context_tokens"] is None
    assert status["updated_at"].endswith("Z")

    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["task"]["id"] == tid
    assert tasks[0]["task"]["shift"] == 1
    assert tasks[0]["task"]["created_at"].endswith("Z")
    # guards / chain 缺的键从 config 同名段拷贝
    assert tasks[0]["task"]["guards"] == {"session_pct_max": 80, "weekly_pct_max": 95}
    assert tasks[0]["task"]["chain"] == {"max_windows": 3, "on_no_handover": "continue"}

    before = store.read_status(tid)
    time.sleep(1.1)  # updated_at 秒级精度，隔一秒才看得出变化
    after = store.update_status(tid, state="working", turns=1)
    assert after["state"] == "working"
    assert after["turns"] == 1
    assert after["retries"] == 0  # 原有字段合并不丢
    assert after["updated_at"] > before["updated_at"]


def test_list_tasks_sorted_by_run_at():
    t1 = store.create_task(make_task(run_at="2026-08-27T20:00:00Z", title="晚的"), CONFIG)
    t2 = store.create_task(make_task(run_at="2026-08-27T10:00:00Z", title="早的"), CONFIG)
    ids = [item["task"]["id"] for item in store.list_tasks()]
    assert ids == [t2, t1]


def test_create_task_validations():
    with pytest.raises(ValueError):
        store.create_task(make_task(project="nope"), CONFIG)
    with pytest.raises(ValueError):
        store.create_task(make_task(effort="ultra"), CONFIG)
    with pytest.raises(ValueError):
        store.create_task(make_task(run_at="2026-08-27 18:00:00"), CONFIG)  # 没有 Z
    with pytest.raises(ValueError):
        store.create_task(make_task(run_at="2026-08-27T18:00:00+08:00"), CONFIG)
    with pytest.raises(ValueError):
        store.create_task({"title": "缺一堆字段"}, CONFIG)


def test_render_only_known_placeholders():
    tpl = "你好 {title}，正文是 {task}，{不认识的} 和 {{ 保持原样"
    out = store.render(tpl, title="T", task="正文A")
    assert out == "你好 T，正文是 正文A，{不认识的} 和 {{ 保持原样"


def test_build_prompt_known_placeholders():
    """build_prompt 与 cmd_add 的模板渲染同一套占位符。"""
    config = dict(CONFIG)
    config["prompt_template"] = "项目 {project_path}｜标题 {title}｜上限 {context_limit}\n{task}"
    config["models"] = {"claude-fable-5": {"context_limit": 500000}}
    config["default_context_limit"] = 200000
    out = store.build_prompt(config, "标题A", "demo", "claude-fable-5", "正文B")
    assert out == "项目 /home/user/projects/demo｜标题 标题A｜上限 500000\n正文B"
    # 模型不在 config.models 里 → 退到 default_context_limit
    out2 = store.build_prompt(config, "标题A", "demo", "unknown-model", "正文B")
    assert "200000" in out2
    # 旧写法（cmd_add 原来手拼 render 的等价形式）结果一致
    old = store.render(
        config["prompt_template"],
        task="正文B",
        title="标题A",
        project_path=config["projects"]["demo"],
        context_limit=config["models"]["claude-fable-5"]["context_limit"],
    )
    assert out == old


def _worker(task_id: str, tag: str, count: int) -> None:
    for i in range(count):
        store.update_status(task_id, **{f"{tag}_{i}": i})
        store.append_event(task_id, f"{tag} 第 {i} 条")


def test_concurrent_update_status_from_two_processes():
    tid = store.create_task(make_task(), CONFIG)
    procs = [
        multiprocessing.Process(target=_worker, args=(tid, f"p{n}", 200))
        for n in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert all(p.exitcode == 0 for p in procs)

    status = store.read_status(tid)
    assert status["p0_199"] == 199  # 两个进程的最后一个字段都在
    assert status["p1_199"] == 199
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    lines = events.splitlines()
    assert len(lines) == 400  # 一行不多一行不少
    assert all("\t" in line for line in lines)


def _bump_n(status: dict) -> None:
    """mutator：原地 +1（返回值被 modify_status 忽略）。"""
    status["n"] = status.get("n", 0) + 1


def _modify_worker(task_id: str, count: int) -> None:
    for _ in range(count):
        store.modify_status(task_id, _bump_n)


def test_modify_status_counts_no_loss():
    """两个子进程各 200 次锁内 +1，一条不丢（R2）。"""
    tid = store.create_task(make_task(), CONFIG)
    procs = [
        multiprocessing.Process(target=_modify_worker, args=(tid, 200))
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert all(p.exitcode == 0 for p in procs)
    assert store.read_status(tid)["n"] == 400

    # 直接调一次：mutator 原地改、盖 updated_at、返回新 status
    out = store.modify_status(tid, _bump_n)
    assert out["n"] == 401
    assert "updated_at" in out


def test_config_example_json_valid():
    example = Path(__file__).resolve().parent.parent / "config.example.json"
    config = json.loads(example.read_text(encoding="utf-8"))
    for key in (
        "tmux_session",
        "window_prefix",
        "claude_bin",
        "probe_model",
        "display_tz_offset_hours",
        "memory_max_bytes",
        "projects",
        "models",
        "default_context_limit",
        "efforts",
        "guards",
        "chain",
        "prompt_template",
        "context_warn_text",
        "chain_template",
    ):
        assert key in config, f"config.example.json 缺键：{key}"
