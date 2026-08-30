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


def test_build_prompt_codex_no_context_limit_renders_human_text():
    """S6.1 B3：Codex 模型查不到稳定水位时，{context_limit} 要渲染成人话
    "暂无稳定水位来源"，不能是字面的 "None"，也不能悄悄套顶层
    default_context_limit 冒充一个假数字。"""
    config = dict(CONFIG)
    config["prompt_template"] = "上限 {context_limit}\n{task}"
    config["default_context_limit"] = 200000
    config["runners"] = {
        "codex": {"models": {"gpt-5.6-luna": {"context_limit": None}}},
    }
    out = store.build_prompt(
        config, "标题A", "demo", "gpt-5.6-luna", "正文B", runner="codex",
    )
    assert out == "上限 暂无稳定水位来源\n正文B"
    assert "None" not in out
    assert "200000" not in out  # 不能悄悄套 Claude 的 default


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
        "runners",
    ):
        assert key in config, f"config.example.json 缺键：{key}"
    for name in ("claude", "codex"):
        rc = config["runners"][name]
        assert rc["models"], f"config.runners.{name}.models 不能是空的"
        assert rc["efforts"], f"config.runners.{name}.efforts 不能是空的"
    # S6：Codex 模型没有稳定上下文水位来源，必须显式 null，不许猜一个窗口大小
    for spec in config["runners"]["codex"]["models"].values():
        assert spec["context_limit"] is None


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
        {"review": {"enabled": True}},                       # 没给 runner，S7 也拒
        {"review": {"enabled": False, "merge_policy": "yolo"}},
        {"review": {"enabled": "yes"}},
        {"review": "审一下"},
        {"review": {"enabled": False, "unknown_key": "多出来的键"}},  # S7：criteria_text 已合法，换个真正未知的键
        {"worktree": False, "review": {"enabled": True, "runner": "claude",
                                        "model": "claude-fable-5", "effort": "high"}},  # S7：enabled 要求 worktree=true
        {"review": {"enabled": True, "runner": "claude", "model": "claude-fable-5",
                     "effort": "high", "max_rounds": True}},  # bool 不能冒充正整数
        {"review": {"enabled": True, "runner": "claude", "model": "claude-fable-5",
                     "effort": "high", "on_no_quota": "postpone"}},
        {"review": {"enabled": True, "runner": "codex", "model": "gpt-5.6-luna",
                     "effort": "high"}},  # CONFIG 没配 codex runner
    ):
        with pytest.raises(ValueError):
            store.create_task(make_task(**over), CONFIG)


def test_review_enabled_requires_worktree_and_valid_reviewer():
    config = dict(CONFIG)
    config["runners"] = {
        "claude": {"models": {"claude-fable-5": {"context_limit": 500000}},
                   "efforts": ["low", "medium", "high", "xhigh", "max"]},
    }
    tid = store.create_task(
        make_task(worktree=True, review={
            "enabled": True, "runner": "claude", "model": "claude-fable-5",
            "effort": "high",
        }),
        config,
    )
    task = store.load_task(tid)
    assert task["review"]["enabled"] is True
    assert task["review"]["max_rounds"] == 5
    assert task["review"]["on_no_quota"] == "release"
    assert task["review"]["merge_policy"] == "manual"
    assert task["review"]["criteria_text"] == ""
    assert task["role"] == "build"
    assert task["round"] == 1
    assert task["role_shift"] == 1
    assert task["pipeline_id"] == tid


def test_review_config_defaults_and_overrides():
    config = dict(CONFIG)
    config["review"] = {"max_rounds": 2, "on_no_quota": "hold", "criteria_text": "全局标准"}
    task = make_task(review={"enabled": True, "runner": "claude",
                              "model": "claude-fable-5", "effort": "high"})
    merged = store.review_config(task, config)
    assert merged["max_rounds"] == 2
    assert merged["on_no_quota"] == "hold"
    assert merged["criteria_text"] == "全局标准"
    assert merged["merge_policy"] == "manual"  # 代码内置默认
    task["review"]["max_rounds"] = 1
    merged2 = store.review_config(task, config)
    assert merged2["max_rounds"] == 1  # task 显式给的优先于 config


def test_review_config_rejects_bad_config_level_values():
    """S7.1 非阻断尾巴：config.review 的坏值要在 review_config() 合并时就
    地报出人话原因（ConfigInvalid），不能一路传到 scheduler 里某个
    int(...)/字符串比较才炸——那时完全看不出是哪份配置的问题。task.review
    本身仍由 validate_task 在创建时挡住（这里测的是 config 级默认值）。"""
    task = make_task(review={"enabled": True, "runner": "claude",
                              "model": "claude-fable-5", "effort": "high"})

    bad_rounds = dict(CONFIG, review={"max_rounds": "five"})
    with pytest.raises(store.ConfigInvalid, match="max_rounds"):
        store.review_config(task, bad_rounds)

    bad_rounds_zero = dict(CONFIG, review={"max_rounds": 0})
    with pytest.raises(store.ConfigInvalid, match="max_rounds"):
        store.review_config(task, bad_rounds_zero)

    bad_quota = dict(CONFIG, review={"on_no_quota": "explode"})
    with pytest.raises(store.ConfigInvalid, match="on_no_quota"):
        store.review_config(task, bad_quota)

    bad_policy = dict(CONFIG, review={"merge_policy": "yolo"})
    with pytest.raises(store.ConfigInvalid, match="merge_policy"):
        store.review_config(task, bad_policy)


def test_render_review_prompts_fall_back_when_config_missing_template_keys():
    """S7.1 非阻断尾巴：旧生产 config 没有 review_template/review_fix_template
    两个键时，以前是 config["review_template"] 直接索引，实测
    KeyError('review_template')；改成 .get(...) or DEFAULT_xxx 后不该再炸，
    且渲染出的正文要能看到任务标题（证明确实走了兜底模板而不是空字符串）。"""
    task = make_task(title="没有模板键也要能审")
    prompt = store.render_review_prompt(
        CONFIG, task, base_ref="abc123", diff_command="git diff abc123..HEAD",
        build_handover="交接内容", previous_review="", round_=1,
    )
    assert "没有模板键也要能审" in prompt
    assert "NEXT: done" in prompt

    fix_prompt = store.render_review_fix_prompt(
        CONFIG, task, round_=2, review_text="退回：改一下命名",
    )
    assert "没有模板键也要能审" in fix_prompt
    assert "退回：改一下命名" in fix_prompt


def test_effective_runner_model_effort_by_role():
    task = {
        "id": "x", "runner": "claude", "model": "claude-fable-5", "effort": "high",
        "review": {"enabled": True, "runner": "codex", "model": "gpt-5.6-luna", "effort": "xhigh"},
        "role": "build",
    }
    assert store.effective_runner(task) == "claude"
    assert store.effective_model(task) == "claude-fable-5"
    assert store.effective_effort(task) == "high"
    task["role"] = "review"
    assert store.effective_runner(task) == "codex"
    assert store.effective_model(task) == "gpt-5.6-luna"
    assert store.effective_effort(task) == "xhigh"


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


# ---------- S6：runner 配置、任务字段与兼容层 ----------

RUNNERS_CONFIG = {
    **CONFIG,
    "chain_template": "{task} 第 {shift} 班 {handover}",
    "default_context_limit": 1000000,
    "runners": {
        "claude": {
            "bin": "claude",
            "models": {"claude-fable-5": {"context_limit": 500000}},
            "efforts": ["low", "medium", "high", "xhigh", "max"],
        },
        "codex": {
            "bin": "codex",
            "profile": "nightowl",
            "models": {"gpt-5.6-luna": {"context_limit": None}},
            "efforts": ["low", "medium", "high", "xhigh"],
        },
    },
}


def test_runner_config_old_config_synthesizes_claude_only():
    """旧 config（没有 runners 键）：合成只含 claude 的兼容视图，没有 codex。"""
    old_config = {
        **CONFIG,
        "claude_bin": "claude",
        "probe_model": "claude-haiku-4-5-20251001",
        "models": {"claude-fable-5": {"context_limit": 500000}},
    }
    rc = store.runner_config(old_config)
    assert set(rc) == {"claude"}
    assert rc["claude"]["bin"] == "claude"
    assert rc["claude"]["probe_model"] == "claude-haiku-4-5-20251001"
    assert rc["claude"]["models"] == {"claude-fable-5": {"context_limit": 500000}}
    assert rc["claude"]["efforts"] == CONFIG["efforts"]


def test_runner_config_new_config_used_as_is():
    rc = store.runner_config(RUNNERS_CONFIG)
    assert rc is RUNNERS_CONFIG["runners"]
    assert set(rc) == {"claude", "codex"}


def test_runner_config_codex_only_runners_key_keeps_claude_compat():
    """runners 里只写了 codex（没写 claude）：claude 仍从顶层兼容合成。"""
    config = {
        **CONFIG,
        "claude_bin": "claude",
        "models": {"claude-fable-5": {"context_limit": 500000}},
        "runners": {"codex": RUNNERS_CONFIG["runners"]["codex"]},
    }
    rc = store.runner_config(config)
    assert set(rc) == {"claude", "codex"}
    assert rc["claude"]["models"] == {"claude-fable-5": {"context_limit": 500000}}
    assert rc["codex"]["bin"] == "codex"


def test_validate_task_default_runner_is_claude():
    task = make_task()
    assert "runner" not in task
    assert store.validate_task(task, RUNNERS_CONFIG) == "time"


def test_validate_task_explicit_codex_runner():
    task = make_task(runner="codex", model="gpt-5.6-luna", effort="high")
    assert store.validate_task(task, RUNNERS_CONFIG) == "time"


def test_validate_task_rejects_unknown_runner_literal():
    with pytest.raises(ValueError):
        store.validate_task(make_task(runner="gemini"), RUNNERS_CONFIG)


def test_validate_task_rejects_runner_not_configured():
    """codex 请求了但 config.runners 里没配 codex：报人话错误，不是泛化崩溃。"""
    with pytest.raises(ValueError, match="Codex"):
        store.validate_task(make_task(runner="codex", model="x", effort="high"), CONFIG)


def test_validate_task_cross_runner_model_rejected():
    """拿 Claude 的模型给 codex 任务用：按 codex 的模型表校验，直接拒。"""
    with pytest.raises(ValueError, match="Codex"):
        store.validate_task(
            make_task(runner="codex", model="claude-fable-5", effort="high"),
            RUNNERS_CONFIG,
        )
    with pytest.raises(ValueError, match="Claude Code"):
        store.validate_task(
            make_task(runner="claude", model="gpt-5.6-luna", effort="high"),
            RUNNERS_CONFIG,
        )


def test_validate_task_cross_runner_effort_rejected():
    with pytest.raises(ValueError, match="Codex"):
        store.validate_task(
            make_task(runner="codex", model="gpt-5.6-luna", effort="max"),  # codex 没有 max
            RUNNERS_CONFIG,
        )


def test_validate_task_model_check_skipped_when_models_dict_empty():
    """旧配置常见：runner 的 models 字典是空的——不拦任意模型名，只保留 effort 校验。"""
    assert store.validate_task(make_task(model="随便什么模型"), CONFIG) == "time"


def test_create_task_defaults_runner_claude_and_persists():
    tid = store.create_task(make_task(), RUNNERS_CONFIG)
    assert store.load_task(tid)["runner"] == "claude"


def test_create_task_explicit_codex_runner_persists():
    tid = store.create_task(
        make_task(runner="codex", model="gpt-5.6-luna", effort="high"), RUNNERS_CONFIG
    )
    assert store.load_task(tid)["runner"] == "codex"


def test_old_task_json_missing_runner_stays_claude_via_validate():
    """S6 上线前落盘的旧记录没有 runner 字段：按 claude 解释，不回写不迁移。"""
    old = {
        "id": "20250101-000000-eeee", "title": "旧任务", "project": "demo",
        "model": "claude-fable-5", "effort": "high", "shift": 1,
        "run_at": "2025-01-01T00:00:00Z", "task_text": "正文",
        "prompt_final": "提示词", "created_at": "2025-01-01T00:00:00Z",
        "trigger": {"type": "time"},
    }
    d = store.task_dir("20250101-000000-eeee")
    d.mkdir(parents=True, exist_ok=True)
    store.atomic_write_json(d / "task.json", old)
    loaded = store.load_task("20250101-000000-eeee")
    assert "runner" not in loaded
    assert store.validate_task(loaded, RUNNERS_CONFIG, task_id=loaded["id"]) == "time"


def test_create_successor_copies_runner():
    parent_id = store.create_task(
        make_task(runner="codex", model="gpt-5.6-luna", effort="high"), RUNNERS_CONFIG
    )
    succ = store.load_task(store.create_successor(
        store.load_task(parent_id), "交接", RUNNERS_CONFIG))
    assert succ["runner"] == "codex"
    # 旧式父任务（缺 runner）：后继也按 claude 解释，不吃别的默认
    parent2 = make_task()
    del parent2["run_at"]
    parent2["run_at"] = "2026-08-27T18:00:00Z"
    parent2_id = store.create_task(parent2, RUNNERS_CONFIG)
    task2 = store.load_task(parent2_id)
    del task2["runner"]  # 模拟 S6 上线前落盘、没有这个字段的旧记录
    store.atomic_write_json(store.task_dir(parent2_id) / "task.json", task2)
    succ2 = store.load_task(store.create_successor(task2, "交接", RUNNERS_CONFIG))
    assert succ2["runner"] == "claude"


def test_next_pipeline_shift_bootstraps_from_existing_max_shift_on_old_chain():
    """S7.2 阻断一反例：coordinator 第一次领号（没有 pipeline_shift_seq）
    时不能固定从 1 开始——S7 上线前（S1-S6 时代）这条链上可能已经有一个
    shift=3 的旧任务落盘（scheduled/postponed/idle/chained 都算），固定从
    1+1=2 起会比现存最大 shift 还小，chain_state() 的 max-shift 扫描会继续
    认那个旧任务是"最新班"。第一次领号必须先扫一遍这条 pipeline 现存所有
    任务的最大 shift，从它开始 bootstrap。"""
    old = make_task()
    old["id"] = "20260101-000000-aaaa"
    old["created_at"] = "2026-01-01T00:00:00Z"
    old["shift"] = 3  # 模拟滚动升级前就已经推进了三班的旧任务
    d = store.task_dir(old["id"])
    d.mkdir(parents=True, exist_ok=True)
    store.atomic_write_json(d / "task.json", old)
    # 这条旧任务自己就是它自己的 coordinator（没有 pipeline_id/root_id 字段），
    # 所以 coordinator 的 status.json 上此刻也还没有 pipeline_shift_seq。
    assert "pipeline_shift_seq" not in store.read_status(old["id"])

    got = store.next_pipeline_shift(old)
    assert got == 4  # 不是 2——bootstrap 自现存最大 shift=3，而不是固定从 1 起
    got2 = store.next_pipeline_shift(old)
    assert got2 == 5  # 之后就是纯 +1，不用每次都重新扫


def test_next_pipeline_shift_bootstraps_from_root_when_task_is_a_successor():
    """同上，但旧任务是通过 pipeline_id 关联到另一个 coordinator（模拟旧
    Codex 续班链）：bootstrap 要扫的是整条 pipeline，不是只看被传进来的
    这一个任务自己的 shift。"""
    root_id = "20260101-000000-bbbb"
    root = make_task()
    root["id"] = root_id
    root["created_at"] = "2026-01-01T00:00:00Z"
    root["shift"] = 1
    store.atomic_write_json(store.task_dir(root_id) / "task.json", root)

    old_successor = make_task()
    old_successor["id"] = "20260102-000000-cccc"
    old_successor["created_at"] = "2026-01-02T00:00:00Z"
    old_successor["root_id"] = root_id
    old_successor["shift"] = 3
    store.atomic_write_json(store.task_dir(old_successor["id"]) / "task.json", old_successor)

    got = store.next_pipeline_shift(old_successor)
    assert got == 4  # 扫的是整条 root_id 链的最大 shift（3），不是自己


def test_next_pipeline_shift_concurrent_calls_do_not_collide():
    """并发两次领号（线程模拟）不撞号——bootstrap 与递增都在同一把
    modify_status 锁内完成。"""
    import threading

    task = make_task()
    task["id"] = "20260101-000000-dddd"
    task["created_at"] = "2026-01-01T00:00:00Z"
    task["shift"] = 2
    store.atomic_write_json(store.task_dir(task["id"]) / "task.json", task)

    results: list[int] = []
    lock = threading.Lock()

    def worker():
        got = store.next_pipeline_shift(task)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [3, 4, 5, 6, 7, 8]  # bootstrap 自 2，六次各自 +1，互不撞号


def test_create_successor_codex_context_limit_placeholder_is_human_text():
    """S6.1 B3：Codex 续班渲染 {context_limit} 时也要按 runner 的模型表查、
    查不到写人话，不能是字面 "None" 或悄悄借用 Claude 的 default。"""
    config = {**RUNNERS_CONFIG, "chain_template": "上限 {context_limit}\n{task} 第 {shift} 班 {handover}"}
    parent_id = store.create_task(
        make_task(runner="codex", model="gpt-5.6-luna", effort="high"), config
    )
    succ = store.load_task(store.create_successor(
        store.load_task(parent_id), "交接", config))
    assert succ["prompt_final"].startswith("上限 暂无稳定水位来源\n")
    assert "None" not in succ["prompt_final"]
    assert "1000000" not in succ["prompt_final"]  # 不能借用 default_context_limit
