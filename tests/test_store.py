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


# ---------- S4① 触发方式 trigger / 前置链判定 chain_state ----------


def test_create_task_trigger_defaults_and_after():
    # 缺省补 {"type": "time"}
    tid = store.create_task(make_task(), CONFIG)
    assert store.load_task(tid)["trigger"] == {"type": "time"}

    pre = store.create_task(make_task(title="前置"), CONFIG)
    after = store.create_task(
        make_task(title="后继", trigger={"type": "after", "task": pre, "when": "finished"}),
        CONFIG,
    )
    assert store.load_task(after)["trigger"] == {
        "type": "after", "task": pre, "when": "finished",
    }
    # after 任务缺 run_at 能建：补成创建时刻（Z 结尾，只当排序用）
    no_time = store.create_task(
        make_task(title="没时间", run_at=None,
                  trigger={"type": "after", "task": pre, "when": "ended"}),
        CONFIG,
    )
    assert store.load_task(no_time)["run_at"].endswith("Z")


def test_create_task_trigger_validations():
    # 前置任务不存在
    with pytest.raises(ValueError):
        store.create_task(
            make_task(trigger={"type": "after", "task": "20990101-000000-ffff",
                               "when": "finished"}),
            CONFIG,
        )
    pre = store.create_task(make_task(title="前置"), CONFIG)
    with pytest.raises(ValueError):  # when 不认识
        store.create_task(
            make_task(trigger={"type": "after", "task": pre, "when": "完工"}),
            CONFIG,
        )
    with pytest.raises(ValueError):  # when 缺了也不行
        store.create_task(make_task(trigger={"type": "after", "task": pre}), CONFIG)
    with pytest.raises(ValueError):  # type 不认识
        store.create_task(make_task(trigger={"type": "later"}), CONFIG)
    with pytest.raises(ValueError):  # 不是对象
        store.create_task(make_task(trigger="按时间"), CONFIG)


def test_chain_state_returns_latest_shift_state():
    root = store.create_task(make_task(title="链根"), CONFIG)
    assert store.chain_state(root) == "scheduled"
    store.update_status(root, state="finished")
    # 造出第 2 班：最新一班变成它，链状态跟着变
    succ = store.create_task(
        make_task(title="链根", shift=2, root_id=root, parent_id=root), CONFIG
    )
    assert store.chain_state(root) == "scheduled"
    store.update_status(succ, state="working")
    assert store.chain_state(succ) == "working"
    store.update_status(succ, state="finished")
    assert store.chain_state(root) == "finished"
    assert store.chain_state(succ) == "finished"


def test_validate_task_shared_rules():
    """validate_task 是 create_task 与网页编辑共用的同一套校验。"""
    pre = store.create_task(make_task(title="前置"), CONFIG)
    task = store.load_task(pre)
    # 整份 task.json（含 id/created_at 等额外键）过校验：行
    assert store.validate_task(task, CONFIG, task_id=pre) == "time"
    # 编辑时不许把自己当前置
    with pytest.raises(ValueError):
        store.validate_task(
            {**task, "trigger": {"type": "after", "task": pre, "when": "finished"}},
            CONFIG, task_id=pre,
        )
    # 别的任务当前置：行
    other = store.create_task(make_task(title="别的"), CONFIG)
    assert store.validate_task(
        {**task, "trigger": {"type": "after", "task": other, "when": "ended"}},
        CONFIG, task_id=pre,
    ) == "after"
    # 非法值同样拦
    with pytest.raises(ValueError):
        store.validate_task({**task, "effort": "ultra"}, CONFIG)
    with pytest.raises(ValueError):
        store.validate_task({**task, "run_at": None}, CONFIG)  # 非 after 缺 run_at


def test_validate_task_guards_chain_shapes():
    """S4.1 必修4：guards / chain 存在必须是对象；auto_interrupt_minutes
    非 null 必须是正整数（bool 不算整数）——网页 PUT 的最后一道闸。"""
    task = make_task()
    with pytest.raises(ValueError):
        store.validate_task({**task, "guards": "不是对象"}, CONFIG)
    with pytest.raises(ValueError):
        store.validate_task({**task, "chain": 5}, CONFIG)
    with pytest.raises(ValueError):
        store.validate_task({**task, "chain": [1, 2]}, CONFIG)
    for bad in (0, -2, True, False, "5", 2.5):
        with pytest.raises(ValueError):
            store.validate_task(
                {**task, "guards": {"auto_interrupt_minutes": bad}}, CONFIG
            )
    # 合法形状：不给 / null / 正整数
    assert store.validate_task({**task, "guards": None, "chain": None}, CONFIG) == "time"
    assert store.validate_task(
        {**task, "guards": {"auto_interrupt_minutes": None}}, CONFIG
    ) == "time"
    assert store.validate_task(
        {**task, "guards": {"auto_interrupt_minutes": 5}}, CONFIG
    ) == "time"


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


# ---------- S5：worktree / review 字段 ----------


def test_new_task_worktree_review_defaults():
    tid = store.create_task(make_task(), CONFIG)
    task = store.load_task(tid)
    assert task["worktree"] is True  # 新任务缺省建树
    assert task["review"] == {"enabled": False, "merge_policy": "manual"}
    # 显式 false 原样保留（一期回归开关）
    tid2 = store.create_task(make_task(worktree=False), CONFIG)
    assert store.load_task(tid2)["worktree"] is False


def test_worktree_review_validation_rejects_bad_shapes():
    for over in (
        {"worktree": "true"},
        {"worktree": 1},
        {"review": {"enabled": True}},                       # S7 才开放
        {"review": {"enabled": False, "merge_policy": "yolo"}},
        {"review": {"enabled": "yes"}},
        {"review": "审一下"},
        {"review": {"enabled": False, "criteria_text": "多出来的键"}},
    ):
        with pytest.raises(ValueError):
            store.create_task(make_task(**over), CONFIG)


def test_old_task_json_missing_worktree_stays_false():
    """S5 上线前落盘的旧记录：没有 worktree 字段按 false，不回写不迁移。"""
    old = {
        "id": "20250101-000000-ffff", "title": "旧任务", "project": "demo",
        "model": "claude-fable-5", "effort": "high", "shift": 1,
        "run_at": "2025-01-01T00:00:00Z", "task_text": "正文",
        "prompt_final": "提示词", "created_at": "2025-01-01T00:00:00Z",
        "trigger": {"type": "time"},
    }
    d = store.task_dir("20250101-000000-ffff")
    d.mkdir(parents=True, exist_ok=True)
    store.atomic_write_json(d / "task.json", old)
    loaded = store.load_task("20250101-000000-ffff")
    assert "worktree" not in loaded
    assert store.worktree_enabled(loaded) is False
    # 整份旧记录过校验（网页编辑旧任务不能被新字段卡死）
    assert store.validate_task(loaded, CONFIG, task_id=loaded["id"]) == "time"


def test_build_prompt_worktree_instruction():
    config = dict(CONFIG)
    config["prompt_template"] = "任务 {title}。{worktree_instruction}正文：{task}"
    config["models"] = {"claude-fable-5": {"context_limit": 500000}}
    config["default_context_limit"] = 200000
    out_true = store.build_prompt(config, "T", "demo", "claude-fable-5", "B", worktree=True)
    assert store.WORKTREE_INSTRUCTION in out_true
    out_false = store.build_prompt(config, "T", "demo", "claude-fable-5", "B")
    assert out_false == "任务 T。正文：B"  # 老式任务渲染为空


def test_create_successor_explicitly_copies_worktree_review():
    config = dict(CONFIG)
    config["models"] = {"claude-fable-5": {"context_limit": 500000}}
    config["default_context_limit"] = 200000
    config["chain_template"] = "{task} 第 {shift} 班 {handover}"
    parent_id = store.create_task(
        make_task(worktree=True, review={"enabled": False, "merge_policy": "auto"}),
        config,
    )
    store.update_status(parent_id, worktree_path="/p/.claude/worktrees/x",
                        branch="ns/x", base_ref="abc")
    succ = store.load_task(store.create_successor(
        store.load_task(parent_id), "交接", config))
    assert succ["worktree"] is True
    assert succ["review"] == {"enabled": False, "merge_policy": "auto"}
    status = store.read_status(succ["id"])
    assert status["worktree_path"] == "/p/.claude/worktrees/x"
    assert status["branch"] == "ns/x"
    assert status["base_ref"] == "abc"
    # 旧式父任务：后继必须仍是 false，不吃新任务缺省
    parent2 = make_task(worktree=False)
    pid2 = store.create_task(parent2, config)
    data = store.load_task(pid2)
    del data["worktree"]  # 手工退回旧记录形状
    store.atomic_write_json(store.task_dir(pid2) / "task.json", data)
    succ2 = store.load_task(store.create_successor(data, "交接", config))
    assert succ2["worktree"] is False
    assert "worktree_path" not in store.read_status(succ2["id"])


# ---------- 原子写 mode（S3①：凭据类文件落盘即收紧）----------


def test_atomic_write_text_mode_0600(tmp_path):
    target = tmp_path / "secret.txt"
    store.atomic_write_text(target, "机密内容", mode=0o600)
    assert target.read_text(encoding="utf-8") == "机密内容"
    assert target.stat().st_mode & 0o777 == 0o600
    # 临时文件不留残尸
    assert list(tmp_path.glob(".*tmp.*")) == []


def test_atomic_write_json_mode_0600(tmp_path):
    target = tmp_path / "secret.json"
    store.atomic_write_json(target, {"a": 1}, mode=0o600)
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert target.stat().st_mode & 0o777 == 0o600


def test_create_successor_renders_all_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    config = json.load(open(Path(__file__).resolve().parent.parent / "config.example.json", encoding="utf-8"))
    config["projects"] = {"demo": "/home/user/projects/demo"}
    parent_id = store.create_task({
        "title": "标题X", "project": "demo", "model": "claude-fable-5", "effort": "high",
        "run_at": "2026-08-27T18:00:00Z", "task_text": "正文Y", "prompt_final": "p",
    }, config)
    succ = store.create_successor(store.load_task(parent_id), "交接Z\nNEXT: continue", config)
    prompt = store.load_task(succ)["prompt_final"]
    for piece in ("标题X", "正文Y", "交接Z", "第 2 班", "/home/user/projects/demo", "1000000"):
        assert piece in prompt, piece
    assert "{" not in prompt.replace("{}", "")
