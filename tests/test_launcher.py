"""launcher 的测试：纯函数部分 + tmux 集成（假 claude，不花钱）。"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from nightshift import launcher, store, worktree

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = FIXTURES.parent / "fake_claude.sh"
SELFTEST_SESSION = "ns-selftest"  # 守则：测试专用会话名，用完必杀

CONFIG = {
    "tmux_session": SELFTEST_SESSION,
    "window_prefix": "ns:",
    "claude_bin": "claude",
    "probe_model": "claude-haiku-4-5-20251001",
    "display_tz_offset_hours": 8,
    "memory_max_bytes": 2147483648,
    "projects": {"demo": "/home/user/projects/demo"},
    "models": {
        "claude-fable-5": {"context_limit": 500000, "usage_label": "Fable"},
    },
    "default_context_limit": 200000,
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
    "chain": {"max_windows": 3, "on_no_handover": "continue"},
}


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    monkeypatch.delenv("NIGHTSHIFT_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("NIGHTSHIFT_CLAUDE_JSON", raising=False)
    # S7.6：ensure_codex_trusted 会往 CODEX_HOME/config.toml 写盘——指到 tmp，
    # 绝不让任何一条测试摸到真实 ~/.codex。
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    store.atomic_write_json(tmp_path / "config.json", CONFIG)
    return tmp_path


def make_task(project_path: str | None = None, **over):
    """建一个任务；project_path 不为 None 时改写数据目录里的 config.json，
    返回 (task_id, 实际用的 config)——launch 必须用这份 config。"""
    config = dict(CONFIG)
    if project_path:
        config["projects"] = {"demo": project_path}
        store.atomic_write_json(store.home() / "config.json", config)
    task = {
        "title": "集成测试任务",
        "project": "demo",
        "model": "claude-fable-5",
        "effort": "high",
        "run_at": "2026-08-27T18:00:00Z",
        "task_text": "正文",
        "prompt_final": "完整提示词",
    }
    task.update(over)
    return store.create_task(task, config), config


# ---------- 纯函数部分 ----------


def test_hook_settings_seven_events():
    settings = launcher.hook_settings("abc-123")
    hooks = settings["hooks"]
    assert set(hooks) == {
        "UserPromptSubmit",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "PostToolUse",
        "PreCompact",
        "SessionEnd",
    }
    for event, entries in hooks.items():
        assert len(entries) == 1
        inner = entries[0]["hooks"][0]
        assert inner["type"] == "command"
        assert inner["timeout"] == 10
        assert f"nightshift.hook abc-123 {event}" in inner["command"]


def test_write_task_files(tmp_path):
    task_id, config = make_task(project_path="/home/user/projects/demo")
    task = store.load_task(task_id)
    d = store.task_dir(task_id)
    # R1：上一轮窗口留下的旧 exit_code 必须先删，免得误判新窗口
    d.mkdir(parents=True, exist_ok=True)
    (d / "exit_code").write_text("3\n", encoding="utf-8")
    launcher.write_task_files(task, config, "01234567-89ab-cdef-0123-456789abcdef")

    run_sh = (d / "run.sh").read_text(encoding="utf-8")
    assert "--model 'claude-fable-5'" in run_sh
    assert "--effort 'high'" in run_sh
    assert "--permission-mode auto" in run_sh
    assert "--session-id '01234567-89ab-cdef-0123-456789abcdef'" in run_sh
    assert f"--settings '{d / 'settings.json'}'" in run_sh
    assert "cd '/home/user/projects/demo'" in run_sh
    assert "unset CLAUDECODE" in run_sh
    assert f"NIGHTSHIFT_HOME='{store.home()}'" in run_sh
    assert f"PYTHONPATH='{REPO_ROOT}'" in run_sh
    assert "claude 已退出" in run_sh
    # R1：claude 退出码落盘，调度器靠它识破 read 留窗的假活
    assert not (d / "exit_code").exists()  # 旧的先删了
    assert 'echo "$code" > ' in run_sh
    assert f"'{d / 'exit_code'}'" in run_sh
    # 提示词参数必须整体包在双引号里（防切词/展开）：含 "$(cat 且该行以 )" 收尾
    cat_line = next(line for line in run_sh.splitlines() if "$(cat " in line)
    assert '"$(cat ' in cat_line
    assert cat_line.endswith(')"')
    mode = (d / "run.sh").stat().st_mode
    assert mode & 0o700 == 0o700  # 可执行

    settings = json.loads((d / "settings.json").read_text(encoding="utf-8"))
    assert len(settings["hooks"]) == 7
    # S5：新任务缺省 worktree=true，prompt.txt 必须带运行时安全前言（不可遗漏），
    # 且原文仍在；老式任务（显式 false）prompt.txt 与 prompt_final 一字不差
    prompt_txt = (d / "prompt.txt").read_text(encoding="utf-8")
    assert prompt_txt.startswith(store.WORKTREE_INSTRUCTION)
    assert prompt_txt.endswith(task["prompt_final"])
    task_id2, config2 = make_task(
        project_path="/home/user/projects/demo", worktree=False
    )
    launcher.write_task_files(
        store.load_task(task_id2), config2, "01234567-89ab-cdef-0123-456789abcdee"
    )
    assert (store.task_dir(task_id2) / "prompt.txt").read_text(
        encoding="utf-8"
    ) == store.load_task(task_id2)["prompt_final"]


def test_claude_bin_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_BIN", str(tmp_path / "fake.sh"))
    assert launcher.claude_bin(CONFIG) == str(tmp_path / "fake.sh")
    monkeypatch.delenv("NIGHTSHIFT_CLAUDE_BIN")
    assert launcher.claude_bin(CONFIG) == CONFIG["claude_bin"]


# ---------- S6：Codex 工人 ----------

CODEX_CONFIG = {
    **CONFIG,
    "chain_template": "{task} 第 {shift} 班 {handover}",
    "runners": {
        "claude": {"bin": "claude", "models": CONFIG["models"], "efforts": CONFIG["efforts"]},
        "codex": {
            "bin": "codex", "profile": "nightowl",
            "models": {"gpt-5.6-luna": {"context_limit": None}},
            "efforts": ["low", "medium", "high", "xhigh"],
        },
    },
}


def test_codex_bin_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("NIGHTSHIFT_CODEX_BIN", raising=False)
    assert launcher.codex_bin(CODEX_CONFIG) == "codex"
    monkeypatch.setenv("NIGHTSHIFT_CODEX_BIN", str(tmp_path / "fake.sh"))
    assert launcher.codex_bin(CODEX_CONFIG) == str(tmp_path / "fake.sh")


def test_codex_resume_thread_id_cases(tmp_path):
    task_id, config = make_task_codex(project_path=str(tmp_path / "proj"))
    task = store.load_task(task_id)
    assert launcher.codex_resume_thread_id(task) is None  # 首班没有 parent_id

    # S7：role_shift == 1（缺省，或角色轮转的第一班）天然不要求 resume——
    # 即便手滑给了 parent_id，也不该去查它
    task["parent_id"] = "nonexistent-parent"
    assert launcher.codex_resume_thread_id(task) is None

    # role_shift > 1 才是"同角色续班"，这时才要求 resume 父班的 thread_id
    task["role_shift"] = 2
    assert launcher.codex_resume_thread_id(task) is None  # 父班没登记 thread_id

    store.update_status("nonexistent-parent", thread_id="thread-abc")
    assert launcher.codex_resume_thread_id(task) == "thread-abc"

    # 旧任务（没有 role_shift 字段）退回 S6 老规则：有 parent_id 就必须 resume
    legacy = {k: v for k, v in task.items() if k != "role_shift"}
    assert launcher.codex_resume_thread_id(legacy) == "thread-abc"


def make_task_codex(project_path: str | None = None, **over):
    config = dict(CODEX_CONFIG)
    if project_path:
        config["projects"] = {"demo": project_path}
        store.atomic_write_json(store.home() / "config.json", config)
    task = {
        "title": "Codex集成测试任务",
        "project": "demo",
        "runner": "codex",
        "model": "gpt-5.6-luna",
        "effort": "high",
        "run_at": "2026-08-27T18:00:00Z",
        "task_text": "正文",
        "prompt_final": "完整提示词",
    }
    task.update(over)
    return store.create_task(task, config), config


def test_run_sh_text_codex_new_session_command():
    task_id, config = make_task_codex(project_path="/home/user/projects/demo")
    task = store.load_task(task_id)
    store.update_status(task_id, worktree_path="/home/user/projects/demo")
    run_sh = launcher.run_sh_text(task, config, None)
    assert f"export NIGHTOWL_TASK_ID='{task_id}'" in run_sh
    assert "export NIGHTOWL_RUNNER='codex'" in run_sh
    cmd_line = next(line for line in run_sh.splitlines() if line.startswith("'codex'"))
    # S7.6：命令行 trust 覆盖已删除（Codex 信任闸门根本不认它，见
    # ensure_codex_trusted）；信任改由 launch() 起会话前持久化写盘解决。
    assert cmd_line == (
        "'codex' -C '/home/user/projects/demo' --sandbox workspace-write "
        "--ask-for-approval never -m 'gpt-5.6-luna' "
        '-c \'model_reasoning_effort="high"\' '
        "--profile 'nightowl' "
        f"\"$(cat '{store.task_dir(task_id) / 'prompt.txt'}')\""
    )
    assert "trust_level" not in cmd_line
    assert "codex 已退出" in run_sh
    assert "--session-id" not in run_sh  # Codex 没有这个概念
    assert "resume" not in cmd_line  # 新会话不 resume


def test_run_sh_text_codex_resume_command():
    task_id, config = make_task_codex(project_path="/home/user/projects/demo")
    task = store.load_task(task_id)
    run_sh = launcher.run_sh_text(task, config, "thread-xyz", resume_thread_id="thread-xyz")
    cmd_line = next(line for line in run_sh.splitlines() if line.startswith("'codex'"))
    assert cmd_line.startswith("'codex' resume 'thread-xyz' -C ")


def test_write_task_files_codex_skips_settings_json(tmp_path):
    task_id, config = make_task_codex(project_path="/home/user/projects/demo")
    task = store.load_task(task_id)
    store.update_status(task_id, worktree_path="/home/user/projects/demo")
    launcher.write_task_files(task, config, None)
    d = store.task_dir(task_id)
    assert not (d / "settings.json").exists()
    assert (d / "run.sh").is_file()
    assert (d / "prompt.txt").is_file()


def test_write_task_files_codex_includes_f12_instruction_once(tmp_path):
    """S6.1 A1：Codex 任务的 prompt.txt 必须带 F12 后台协议前言（新会话），
    且只出现一次；worktree 前言同时存在时两条都要在。"""
    task_id, config = make_task_codex(project_path="/home/user/projects/demo")
    task = store.load_task(task_id)
    store.update_status(task_id, worktree_path="/home/user/projects/demo")
    launcher.write_task_files(task, config, None)
    prompt_txt = (store.task_dir(task_id) / "prompt.txt").read_text(encoding="utf-8")
    assert prompt_txt.count(store.CODEX_BACKGROUND_INSTRUCTION) == 1
    assert store.WORKTREE_INSTRUCTION in prompt_txt  # 新任务缺省建树，两条前言都该在
    assert prompt_txt.endswith(task["prompt_final"])


def test_write_task_files_codex_resume_also_includes_f12_instruction(tmp_path):
    """S6.1 A1：续班（有 resume_thread_id）同样要带这条协议——不能只有首班有。"""
    task_id, config = make_task_codex(project_path="/home/user/projects/demo")
    task = store.load_task(task_id)
    launcher.write_task_files(
        task, config, "thread-abc", resume_thread_id="thread-abc",
    )
    prompt_txt = (store.task_dir(task_id) / "prompt.txt").read_text(encoding="utf-8")
    assert prompt_txt.count(store.CODEX_BACKGROUND_INSTRUCTION) == 1


def test_write_task_files_codex_custom_prompt_file_still_gets_instruction_once(tmp_path):
    """S6.1 A1：用户自己用 --prompt-file 给的全文（这里模拟成完全自定义的
    prompt_final，跟模板占位符无关）一样要保证兜底追加，且如果这段文本碰巧
    已经原样包含这条协议（比如用户自己抄了一遍），也不能重复追加成两遍。"""
    task_id, config = make_task_codex(project_path="/home/user/projects/demo", worktree=False)
    task = store.load_task(task_id)
    task["prompt_final"] = "用户自己写的完整提示词，跟模板毫无关系。"
    store.atomic_write_json(store.task_dir(task_id) / "task.json", task)
    launcher.write_task_files(task, config, None)
    prompt_txt = (store.task_dir(task_id) / "prompt.txt").read_text(encoding="utf-8")
    assert prompt_txt.count(store.CODEX_BACKGROUND_INSTRUCTION) == 1
    assert prompt_txt.endswith(task["prompt_final"])

    # 用户自己的文本碰巧已经包含这条协议：不能变成两遍
    task2_id, config2 = make_task_codex(project_path="/home/user/projects/demo", worktree=False)
    task2 = store.load_task(task2_id)
    task2["prompt_final"] = store.CODEX_BACKGROUND_INSTRUCTION + "\n\n用户自己的正文。"
    store.atomic_write_json(store.task_dir(task2_id) / "task.json", task2)
    launcher.write_task_files(task2, config2, None)
    prompt_txt2 = (store.task_dir(task2_id) / "prompt.txt").read_text(encoding="utf-8")
    assert prompt_txt2.count(store.CODEX_BACKGROUND_INSTRUCTION) == 1


def test_write_task_files_codex_review_role_skips_f12_instruction(tmp_path):
    """S7.1 阻断五：F12 后台协议只适用于可写的 build 角色（起长任务、等
    后台完成）——review 角色只读、不该起后台进程，之前只按
    effective_runner=="codex" 判断，漏了角色，会诱导只读的审稿班去干起
    后台任务这种不该做的事。"""
    task_id, config = make_task_codex(
        project_path="/home/user/projects/demo",
        review={"enabled": True, "runner": "codex", "model": "gpt-5.6-luna", "effort": "high"},
    )
    task = store.load_task(task_id)
    task["role"] = "review"
    task["round"] = 1
    store.atomic_write_json(store.task_dir(task_id) / "task.json", task)
    task = store.load_task(task_id)
    store.update_status(task_id, worktree_path="/home/user/projects/demo")
    launcher.write_task_files(task, config, None)
    prompt_txt = (store.task_dir(task_id) / "prompt.txt").read_text(encoding="utf-8")
    assert store.CODEX_BACKGROUND_INSTRUCTION not in prompt_txt
    assert prompt_txt == task["prompt_final"]

    # 对照：同一份 config，build 角色（缺省）照常带这条协议
    build_id, _ = make_task_codex(project_path="/home/user/projects/demo", title="build 对照")
    build_task = store.load_task(build_id)
    store.update_status(build_id, worktree_path="/home/user/projects/demo")
    launcher.write_task_files(build_task, config, None)
    build_prompt_txt = (store.task_dir(build_id) / "prompt.txt").read_text(encoding="utf-8")
    assert store.CODEX_BACKGROUND_INSTRUCTION in build_prompt_txt


def test_write_task_files_claude_prompt_not_padded_with_codex_instruction(tmp_path):
    """Claude 任务的 prompt 一字不多——F12 协议是 Codex 专属，不该出现在
    Claude 的 prompt.txt 里。"""
    task_id, config = make_task(project_path="/home/user/projects/demo", worktree=False)
    task = store.load_task(task_id)
    launcher.write_task_files(task, config, "01234567-89ab-cdef-0123-456789abcdef")
    prompt_txt = (store.task_dir(task_id) / "prompt.txt").read_text(encoding="utf-8")
    assert store.CODEX_BACKGROUND_INSTRUCTION not in prompt_txt
    assert prompt_txt == task["prompt_final"]


def test_launch_codex_resume_fail_closed_without_parent_thread_id(tmp_path, monkeypatch):
    """S6：Codex 续班找不到父班 thread_id，宁可判失败也不悄悄开新会话。"""
    proj = tmp_path / "proj"
    init_git_repo(proj)
    task_id, config = make_task_codex(project_path=str(proj))
    task = store.load_task(task_id)
    task["parent_id"] = "some-parent-without-thread"
    task["role_shift"] = 2  # S7：role_shift > 1 才代表"同角色续班"，要求 resume
    store.atomic_write_json(store.task_dir(task_id) / "task.json", task)
    # 判失败仍会走既有的失败提醒窗口流程（碰 tmux 开个通知窗），这里只假它
    monkeypatch.setattr(
        launcher, "_tmux",
        lambda *a: subprocess.CompletedProcess(a, 0, "", ""),
    )
    status = launcher.launch(task_id, config)
    assert status["state"] == "failed"
    assert "thread_id" in status["error"]


def test_is_trusted_three_cases(tmp_path, monkeypatch):
    claude_json = tmp_path / "claude.json"
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_JSON", str(claude_json))

    # ① 文件不存在 → False
    assert launcher.is_trusted("/some/dir") is False

    # ② 文件在但没有该目录的信任记录 → False
    claude_json.write_text(
        json.dumps({"projects": {"/other/dir": {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    assert launcher.is_trusted("/some/dir") is False
    assert launcher.is_trusted("/other/dir") is True  # 对照：信任过的是 True

    # ③ 显式 false / 坏 JSON → False
    claude_json.write_text(
        json.dumps({"projects": {"/some/dir": {"hasTrustDialogAccepted": False}}}),
        encoding="utf-8",
    )
    assert launcher.is_trusted("/some/dir") is False
    claude_json.write_text("{坏的", encoding="utf-8")
    assert launcher.is_trusted("/some/dir") is False


def test_pid_alive():
    assert launcher.pid_alive(os.getpid()) is True
    # 超出 pid_max（默认约 4 百万）的 pid 必然不存在
    assert launcher.pid_alive(2**24) is False


# ---------- tmux 集成部分（假 claude）----------


@pytest.fixture
def tmux_session():
    if shutil.which("tmux") is None:
        pytest.skip("tmux 不在 PATH，跳过集成测试")
    subprocess.run(
        ["tmux", "kill-session", "-t", SELFTEST_SESSION], capture_output=True
    )
    proc = subprocess.run(
        ["tmux", "new-session", "-d", "-s", SELFTEST_SESSION],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    yield SELFTEST_SESSION
    # 守则：无论断言成败都要杀掉测试会话
    subprocess.run(["tmux", "kill-session", "-t", SELFTEST_SESSION], capture_output=True)


@pytest.fixture
def trusted_env(tmux_session, tmp_path, monkeypatch):
    """假 claude + 假信任记录 + 参数日志，都指到 tmp。"""
    proj = tmp_path / "proj"
    init_git_repo(proj)
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(proj): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_JSON", str(claude_json))
    os.chmod(FAKE_CLAUDE, 0o755)
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_BIN", str(FAKE_CLAUDE))
    fake_log = tmp_path / "fake_claude_args.log"
    monkeypatch.setenv("NIGHTSHIFT_FAKE_LOG", str(fake_log))
    # tmux 窗口里的进程继承的是 server 的环境，不是测试进程的；
    # 把测试用的环境变量写进 ns-selftest 会话环境，随会话生灭。
    subprocess.run(
        ["tmux", "set-environment", "-t", tmux_session,
         "NIGHTSHIFT_FAKE_LOG", str(fake_log)],
        capture_output=True,
    )
    return {"proj": proj, "fake_log": fake_log}


def init_git_repo(path: Path) -> None:
    """把目录变成最小 Git 仓库（S5：工作树任务的项目必须是 Git 仓库）。"""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "ns@example.test"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "ns"],
                   check=True, capture_output=True)
    (path / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)


def wait_for_state(task_id: str, timeout: float = 15.0):
    """轮询到 exited 为止，顺路记录见过的状态。"""
    deadline = time.time() + timeout
    seen = []
    status = {}
    while time.time() < deadline:
        status = store.read_status(task_id)
        state = status.get("state")
        if state not in seen:
            seen.append(state)
        if state == "exited":
            return status, seen
        time.sleep(0.1)
    return status, seen


def test_launch_full_cycle(tmux_session, trusted_env, tmp_path):
    # 提示词含空格/换行/通配符/$变量/单引号：run.sh 里的双引号必须把它整个包住
    prompt_final = "第一行 有空格\n第二行 *.py $HOME it's\n第三行"
    task_id, config = make_task(
        project_path=str(trusted_env["proj"]), prompt_final=prompt_final
    )
    status = launcher.launch(task_id, config)

    assert re.fullmatch(r"@\d+", status["window_id"])
    assert isinstance(status["pane_pid"], int)
    assert launcher.pid_alive(status["pane_pid"])  # 窗口正停在 read 上
    assert status["state"] in ("launching", "working", "idle")

    status, seen = wait_for_state(task_id)
    assert status["state"] == "exited", f"15 秒没等到 exited，见过 {seen}"
    assert "working" in seen, f"中途没见过 working：{seen}"
    assert "idle" in seen, f"中途没见过 idle：{seen}"
    assert status["exit_reason"] == "other"
    # 假 claude 把参数里的 session_id 通过 hook 坐实回了 status
    assert status["session_id"] == status.get("session_id")
    assert launcher.window_alive(status["window_id"], CONFIG)

    # 屏幕快照有收尾横幅（给一点时间让 echo 落到屏幕）
    text = ""
    for _ in range(50):
        text = launcher.capture_pane(status["window_id"])
        if "claude 已退出" in text:
            break
        time.sleep(0.1)
    assert "claude 已退出" in text

    # 假 claude 的参数日志：run.sh 真把那些参数传下去了
    fake_log = trusted_env["fake_log"].read_text(encoding="utf-8")
    assert "--permission-mode" in fake_log
    assert "--settings" in fake_log
    assert "--session-id" in fake_log
    assert status["session_id"] in fake_log

    # 提示词参数核对：printf '%s\n' "$@" 会把参数里的换行原样打出来，
    # 所以 --settings 之后要按"整段"读，不能按行数数。
    idx = fake_log.index("\n--settings\n") + len("\n--settings\n")
    settings_path, sep, rest = fake_log[idx:].partition("\n")
    assert sep == "\n"
    assert settings_path == str(store.task_dir(task_id) / "settings.json")
    assert rest.endswith("\n")  # printf 给每个参数补的换行
    prompt_arg = rest[:-1]  # 去掉 printf 补的换行，剩下的就是那个参数原文
    # --settings 之后只有一个参数，且与 prompt.txt 去掉末尾换行后完全相等
    # （命令替换本来就会剥掉末尾换行，两者天然一致）
    prompt_txt = (store.task_dir(task_id) / "prompt.txt").read_text(encoding="utf-8")
    if prompt_txt.endswith("\n"):
        prompt_txt = prompt_txt[:-1]
    assert prompt_arg == prompt_txt
    # S5：新任务缺省建树，prompt.txt 前面多了运行时安全前言，任务原文原样跟在后面
    assert prompt_arg == store.WORKTREE_INSTRUCTION + "\n\n" + (
        "第一行 有空格\n第二行 *.py $HOME it's\n第三行"
    )


def test_launch_untrusted_opens_failure_window(tmux_session, trusted_env, tmp_path):
    # 用另一个没登记信任的目录
    other = tmp_path / "other"
    other.mkdir()
    task_id, config = make_task(project_path=str(other))
    status = launcher.launch(task_id, config)

    assert status["state"] == "failed"
    assert "未信任" in status["error"]
    # 假 claude 根本不该被叫起来
    assert not trusted_env["fake_log"].exists()

    # 失败窗口出现在 ns-selftest 会话里，名字含 (失败)
    proc = subprocess.run(
        ["tmux", "list-windows", "-t", SELFTEST_SESSION, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert any("(失败)" in name for name in proc.stdout.splitlines())



def test_tmux_targets_use_session_colon(tmp_path, monkeypatch):
    """-t 目标必须是 "会话名:"——不带冒号时 tmux 会先按窗口名解析，
    会话里恰好有个同名窗口就会撞 index（8/27 真机踩到：用户当前窗口就叫 claude）。"""
    proj = tmp_path / "proj"
    init_git_repo(proj)
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(proj): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_JSON", str(claude_json))
    task_id, config = make_task(str(proj))
    config["tmux_session"] = "claude"
    calls = []

    def fake_tmux(*args):
        calls.append(args)
        out = "@1\n" if args[0] == "new-window" else "123\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(launcher, "_tmux", fake_tmux)
    launcher.launch(task_id, config)
    launcher.open_failure_window(store.load_task(task_id), "测试", config)
    launcher.window_alive("@1", config)
    targets = [a[a.index("-t") + 1] for a in calls if a[0] in ("new-window", "list-windows")]
    assert len(targets) == 3 and all(x == "claude:" for x in targets), targets


def test_notice_window_suffix_and_send_keys(tmp_path, monkeypatch):
    """通用通知窗口：窗口名带 suffix、正文逐行落脚本；send-keys 带 Enter。"""
    task_id, config = make_task()
    calls = []

    def fake_tmux(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "@2\n", "")

    monkeypatch.setattr(launcher, "_tmux", fake_tmux)
    launcher.open_notice_window(
        store.load_task(task_id), "(推迟)",
        ["原因：额度 90% 超线 80%", "下次尝试：08-27 19:00"], config,
    )
    new_window = next(a for a in calls if a[0] == "new-window")
    assert "(推迟)" in new_window[new_window.index("-n") + 1]
    script = store.task_dir(task_id) / "notice.sh"
    text = script.read_text(encoding="utf-8")
    assert "原因：额度 90% 超线 80%" in text
    assert "下次尝试：08-27 19:00" in text
    # 失败窗口是通知窗口的特例：名字仍是 (失败)
    calls.clear()
    launcher.open_failure_window(store.load_task(task_id), "炸了", config)
    new_window = next(a for a in calls if a[0] == "new-window")
    assert "(失败)" in new_window[new_window.index("-n") + 1]

    calls.clear()
    launcher.send_keys("@7", "保活探针")
    assert calls == [("send-keys", "-t", "@7", "保活探针", "Enter")]


def test_launch_worktree_cwd_and_transcript(tmp_path, monkeypatch):
    """S5①：worktree=true 的 run.sh cd 到工作树、transcript 按实际 cwd 编码；
    worktree=false 仍 cd 主目录。tmux 用假的，不起真窗口。"""
    proj = tmp_path / "proj"
    init_git_repo(proj)
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(proj): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_JSON", str(claude_json))
    monkeypatch.setattr(
        launcher, "_tmux",
        lambda *a: subprocess.CompletedProcess(a, 0, "@1\n" if a[0] == "new-window" else "123\n", ""),
    )

    task_id, config = make_task(str(proj))
    status = launcher.launch(task_id, config)
    assert status["state"] == "launching"
    slug = worktree.slug_for(task_id, "集成测试任务")
    wt = proj / ".claude" / "worktrees" / slug
    assert wt.is_dir()
    run_sh = (store.task_dir(task_id) / "run.sh").read_text(encoding="utf-8")
    assert f"cd '{wt}'" in run_sh
    st = store.read_status(task_id)
    assert st["worktree_path"] == str(wt)
    assert st["branch"] == f"ns/{slug}"
    assert len(st["base_ref"]) == 40
    encoded = str(wt).replace("/", "-").replace(".", "-")
    assert st["transcript_path"] == str(
        Path.home() / ".claude" / "projects" / encoded / f"{st['session_id']}.jsonl"
    )
    # prompt.txt 带运行时安全前言（模板没写占位符也跑不掉）
    prompt_txt = (store.task_dir(task_id) / "prompt.txt").read_text(encoding="utf-8")
    assert prompt_txt.startswith(store.WORKTREE_INSTRUCTION)

    # 老式任务：不建树、cd 主目录、transcript 按主目录编码
    task_id2, _ = make_task(str(proj), worktree=False)
    status2 = launcher.launch(task_id2, config)
    assert status2["state"] == "launching"
    run_sh2 = (store.task_dir(task_id2) / "run.sh").read_text(encoding="utf-8")
    assert f"cd '{proj}'" in run_sh2
    assert ".claude/worktrees" not in run_sh2
    st2 = store.read_status(task_id2)
    assert "worktree_path" not in st2
    encoded2 = str(proj).replace("/", "-").replace(".", "-")
    assert st2["transcript_path"].startswith(
        str(Path.home() / ".claude" / "projects" / encoded2)
    )
    assert not ((proj / ".claude" / "worktrees").exists() and
                any(p.name != slug for p in (proj / ".claude" / "worktrees").iterdir()))


def test_launch_ensure_failure_fails_task(tmp_path, monkeypatch):
    """建树失败（非 Git 项目）→ 任务 failed、错误人话、不开窗口。"""
    proj = tmp_path / "plain"
    proj.mkdir()
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(proj): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_JSON", str(claude_json))
    monkeypatch.setattr(
        launcher, "_tmux",
        lambda *a: subprocess.CompletedProcess(a, 0, "@1\n" if a[0] == "new-window" else "123\n", ""),
    )
    task_id, config = make_task(str(proj))
    status = launcher.launch(task_id, config)
    assert status["state"] == "failed"
    assert "建工作树失败" in status["error"]
    assert "Git 仓库" in status["error"]
    # run-now 的直接 CLI 路径也走 launch，绕不过建树
    store.update_status(task_id, state="scheduled")
    status2 = launcher.launch(task_id, config)
    assert status2["state"] == "failed"


def test_launch_clears_stale_error(tmp_path, monkeypatch):
    """重跑成功后旧的 error/postpone_reason 必须清掉——否则卡片一直显示上一次失败原因。"""
    proj = tmp_path / "proj"
    init_git_repo(proj)
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(proj): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_JSON", str(claude_json))
    task_id, config = make_task(str(proj))
    store.update_status(task_id, state="failed", error="旧错误", postpone_reason="旧原因")
    monkeypatch.setattr(launcher, "_tmux", lambda *a: subprocess.CompletedProcess(a, 0, "@1\n" if a[0] == "new-window" else "123\n", ""))
    status = launcher.launch(task_id, config)
    assert status["state"] == "launching"
    assert status["error"] is None and status["postpone_reason"] is None


# ---------- Codex tmux 集成部分（假 codex）----------

FAKE_CODEX = FIXTURES.parent / "fake_codex.sh"


@pytest.fixture
def codex_env(tmux_session, tmp_path, monkeypatch):
    """假 codex + 参数日志，都指到 tmp。Codex 不吃 ~/.claude.json 那份信任
    记录，不需要伪造它。"""
    proj = tmp_path / "proj"
    init_git_repo(proj)
    os.chmod(FAKE_CODEX, 0o755)
    monkeypatch.setenv("NIGHTSHIFT_CODEX_BIN", str(FAKE_CODEX))
    fake_log = tmp_path / "fake_codex_args.log"
    monkeypatch.setenv("NIGHTSHIFT_FAKE_LOG", str(fake_log))
    subprocess.run(
        ["tmux", "set-environment", "-t", tmux_session,
         "NIGHTSHIFT_FAKE_LOG", str(fake_log)],
        capture_output=True,
    )
    return {"proj": proj, "fake_log": fake_log}


def test_launch_codex_full_cycle_new_session(tmux_session, codex_env, tmp_path):
    task_id, config = make_task_codex(project_path=str(codex_env["proj"]))
    status = launcher.launch(task_id, config)

    assert re.fullmatch(r"@\d+", status["window_id"])
    assert launcher.pid_alive(status["pane_pid"])
    assert status["state"] == "launching"
    assert status["session_id"] is None  # 新会话：还不知道，等 SessionStart

    status, seen = wait_for_state(task_id)
    assert status["state"] == "exited", f"15 秒没等到 exited，见过 {seen}"
    assert "working" in seen, f"中途没见过 working：{seen}"
    assert "idle" in seen, f"中途没见过 idle：{seen}"
    # 假 codex 的 SessionStart 夹具把 thread_id 坐实回了 status
    assert status["thread_id"] == "01a05206-e86e-7c80-8540-1b92468c92a1"
    assert status["session_id"] == status["thread_id"]
    assert status["quota_source"] == "codex"

    fake_log = codex_env["fake_log"].read_text(encoding="utf-8")
    assert "--sandbox" in fake_log and "workspace-write" in fake_log
    assert "--ask-for-approval" in fake_log and "never" in fake_log
    assert "resume" not in fake_log  # 新会话不该出现 resume 参数
    assert "--session-id" not in fake_log


def test_launch_codex_resume_uses_parent_thread_id(tmux_session, codex_env):
    parent_id, config = make_task_codex(project_path=str(codex_env["proj"]))
    launcher.launch(parent_id, config)
    wait_for_state(parent_id)  # 等首班坐实 thread_id

    parent_task = store.load_task(parent_id)
    # S6.1 A7：生产链路里 _chain_continue 续班时会先关掉父班窗口再造后继；
    # 这里手动做同一步，否则 launch() 的新守卫会因为父窗还活着而 fail-closed
    # （父窗跑完只是"留窗"等回车，tmux 里仍然算活着，见 run.sh 的 read）
    parent_window_id = store.read_status(parent_id)["window_id"]
    launcher.close_windows([parent_window_id], config)

    succ_id = store.create_successor(parent_task, "交接", config)
    succ_status = launcher.launch(succ_id, config)
    assert succ_status["state"] == "launching"
    assert succ_status["session_id"] == "01a05206-e86e-7c80-8540-1b92468c92a1"
    assert succ_status["thread_id"] == "01a05206-e86e-7c80-8540-1b92468c92a1"

    status, seen = wait_for_state(succ_id)
    assert status["state"] == "exited", f"没等到 exited，见过 {seen}"
    # resume 场景假 codex 跳过 SessionStart，thread_id 是 launch() 提前坐实的，
    # 后续事件不该把它改掉
    assert status["thread_id"] == "01a05206-e86e-7c80-8540-1b92468c92a1"

    fake_log = (codex_env["fake_log"]).read_text(encoding="utf-8")
    assert "resume" in fake_log
    assert "01a05206-e86e-7c80-8540-1b92468c92a1" in fake_log


def test_launch_codex_resume_fails_closed_when_parent_window_still_alive(tmux_session, codex_env):
    """S6.1 A7：父班窗口没被关掉（比如 close_windows 失败/没人调用）时，
    绝不允许后继在新窗口 resume 同一个 thread——两开比开不了更糟，宁可这
    一班启动失败。"""
    parent_id, config = make_task_codex(project_path=str(codex_env["proj"]))
    launcher.launch(parent_id, config)
    wait_for_state(parent_id)  # 等首班坐实 thread_id；父窗故意不关

    parent_task = store.load_task(parent_id)
    succ_id = store.create_successor(parent_task, "交接", config)
    succ_status = launcher.launch(succ_id, config)
    assert succ_status["state"] == "failed"
    assert "仍然存活" in succ_status["error"]
    assert "两开" in succ_status["error"]


def test_launch_codex_untrusted_claude_json_does_not_block(tmux_session, codex_env, monkeypatch):
    """S6：Codex 任务不看 ~/.claude.json，哪怕那份文件完全没信任过也照跑。"""
    monkeypatch.setenv("NIGHTSHIFT_CLAUDE_JSON", "/nonexistent/claude.json")
    task_id, config = make_task_codex(project_path=str(codex_env["proj"]))
    status = launcher.launch(task_id, config)
    assert status["state"] == "launching"


# ---------- S7.6：Codex 信任持久化写盘（命令行 -c 覆盖不生效，监理受控实测坐实） ----------


def test_ensure_codex_trusted_writes_config_toml_and_is_idempotent():
    """launcher.ensure_codex_trusted 把 workdir 持久化写进 config.toml，
    重复调用同一个 workdir 不重复追加段落（幂等）。"""
    config_path = launcher.codex_config_path()
    assert not config_path.exists()  # CODEX_HOME 指到 tmp（ns_home 夹具），干净起点

    workdir = "/tmp/proj/.claude/worktrees/abcd-slug"
    launcher.ensure_codex_trusted(workdir)
    assert config_path.is_file()
    text = config_path.read_text(encoding="utf-8")
    assert f'[projects."{workdir}"]' in text
    assert 'trust_level = "trusted"' in text
    assert launcher._codex_workdir_trusted(config_path, workdir)

    launcher.ensure_codex_trusted(workdir)  # 第二次：不该再追加一段
    text2 = config_path.read_text(encoding="utf-8")
    assert text2.count(f'[projects."{workdir}"]') == 1


def test_ensure_codex_trusted_preserves_existing_content_and_other_projects():
    """已有内容（比如另一个 worktree 早就信任过）不能被截断/覆盖，只追加。"""
    config_path = launcher.codex_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        '[projects."/other/tree"]\ntrust_level = "trusted"\n', encoding="utf-8"
    )
    launcher.ensure_codex_trusted("/tmp/proj/.claude/worktrees/new-slug")
    text = config_path.read_text(encoding="utf-8")
    assert '[projects."/other/tree"]' in text
    assert '[projects."/tmp/proj/.claude/worktrees/new-slug"]' in text
    assert launcher._codex_workdir_trusted(config_path, "/other/tree")
    assert launcher._codex_workdir_trusted(config_path, "/tmp/proj/.claude/worktrees/new-slug")


def test_codex_config_path_respects_codex_home(monkeypatch, tmp_path):
    other_home = tmp_path / "other-codex-home"
    monkeypatch.setenv("CODEX_HOME", str(other_home))
    assert launcher.codex_config_path() == other_home / "config.toml"


def test_launch_codex_persists_trust_for_build_role(tmux_session, codex_env):
    """S7.6 回归测试：launch() 起 Codex build 会话前必须把 workdir 持久化
    写进 config.toml——命令行 -c projects...trust_level 覆盖对 Codex 的
    信任闸门无效（监理两发受控实测坐实，见
    reports/夜班-S7.4-真机smoke.md §9.7），补丁前 launch() 从不写这份文件，
    这条断言会失败。worktree=False：直接落在项目目录，workdir 可预测，
    不必再从 status 里现读实际建出来的工作树路径。"""
    workdir = str(codex_env["proj"])
    config_path = launcher.codex_config_path()
    assert not config_path.exists()

    task_id, config = make_task_codex(project_path=workdir, worktree=False)
    launcher.launch(task_id, config)

    assert config_path.is_file()
    assert launcher._codex_workdir_trusted(config_path, workdir)


def test_launch_codex_persists_trust_for_review_role(tmux_session, codex_env):
    """同一条回归测试的 review 角色版本：--sandbox read-only 的审稿会话
    同样必须先持久化写信任，否则真无人值守会静默卡在信任对话框（S7.4
    真机 smoke §9.3/§9.7）。这里保留默认的 worktree=True（S7 审稿角色的
    真实形态本就跑在工作树里），实际 workdir 现读 status.worktree_path，
    不能想当然假设等于项目主目录。"""
    workdir = str(codex_env["proj"])
    config_path = launcher.codex_config_path()

    task_id, config = make_task_codex(project_path=workdir)
    review_task = store.load_task(task_id)
    review_task["role"] = "review"
    review_task["round"] = 1
    review_task["review"] = {
        "enabled": True, "runner": "codex", "model": "gpt-5.6-luna",
        "effort": "low", "max_rounds": 5, "on_no_quota": "release",
        "merge_policy": "manual", "criteria_text": "",
    }
    store.atomic_write_json(store.task_dir(task_id) / "task.json", review_task)

    launcher.launch(task_id, config)

    actual_workdir = store.read_status(task_id)["worktree_path"]
    assert actual_workdir  # review 角色也建了工作树
    assert config_path.is_file()
    assert launcher._codex_workdir_trusted(config_path, actual_workdir)
    # 幂等：这次调用没有把同一个 workdir 的段落重复堆一份
    text = config_path.read_text(encoding="utf-8")
    assert text.count(f'[projects."{actual_workdir}"]') == 1


# ---------- S7：审稿角色的只读命令与有效工人 ----------


def _make_review_task(base_task_id: str, config: dict, *, review_runner: str, review_model: str, review_effort: str):
    task = store.load_task(base_task_id)
    task["role"] = "review"
    task["round"] = 1
    task["review"] = {
        "enabled": True, "runner": review_runner, "model": review_model,
        "effort": review_effort, "max_rounds": 5, "on_no_quota": "release",
        "merge_policy": "manual", "criteria_text": "",
    }
    return task


def test_write_task_files_review_claude_gets_readonly_tools(tmp_path):
    task_id, config = make_task(project_path="/home/user/projects/demo")
    review_task = _make_review_task(
        task_id, config, review_runner="claude",
        review_model="claude-fable-5", review_effort="high",
    )
    launcher.write_task_files(review_task, config, "review-session-id")
    d = store.task_dir(task_id)
    run_sh = (d / "run.sh").read_text(encoding="utf-8")
    assert "--tools 'Read,Glob,Grep,Bash'" in run_sh
    assert "--disallowedTools 'Write,Edit,NotebookEdit'" in run_sh
    for pattern in ("Bash(git diff *)", "Bash(git log *)", "Bash(git show *)",
                    "Bash(git status *)", "Bash(python3 -m pytest *)"):
        assert pattern in run_sh
    # S7.1 阻断五：review 角色用 dontAsk（无人值守拒绝语义），不是 auto
    # （auto 在这台 CLI 上是"列表外也无需询问直接放行"，不是真只读）。
    assert "--permission-mode dontAsk" in run_sh
    assert "--permission-mode auto" not in run_sh
    # Claude 角色不论 build/review 都要写 settings.json（hook 走 per-task 配置）
    assert (d / "settings.json").is_file()

    # build 角色（同一个任务，role 恢复默认）不带任何只读工具面参数
    build_task = store.load_task(task_id)
    launcher.write_task_files(build_task, config, "build-session-id")
    build_run_sh = (d / "run.sh").read_text(encoding="utf-8")
    assert "--tools " not in build_run_sh
    assert "--allowedTools" not in build_run_sh
    assert "--disallowedTools" not in build_run_sh
    # S7.1 阻断五：build 角色字节级不变，仍是 auto，不受 review 的
    # dontAsk 改动影响。
    assert "--permission-mode auto" in build_run_sh
    assert "--permission-mode dontAsk" not in build_run_sh


def test_codex_command_review_role_uses_read_only_sandbox():
    task = {
        "id": "20260830-000000-aaaa", "title": "T", "project": "demo",
        "runner": "codex", "model": "gpt-5.6-luna", "effort": "high",
        "role": "review", "round": 1,
        "review": {
            "enabled": True, "runner": "codex", "model": "gpt-5.6-luna",
            "effort": "xhigh", "max_rounds": 5, "on_no_quota": "release",
            "merge_policy": "manual", "criteria_text": "",
        },
    }
    cmd = launcher._codex_command(task, CODEX_CONFIG, "/work/tree", None)
    assert "--sandbox read-only" in cmd
    assert "--sandbox workspace-write" not in cmd
    assert "--add-dir" not in cmd
    assert "-m 'gpt-5.6-luna'" in cmd
    assert "model_reasoning_effort=\"xhigh\"" in cmd  # 有效档位取自 review.effort

    build_task = {**task, "role": "build"}
    build_cmd = launcher._codex_command(build_task, CODEX_CONFIG, "/work/tree", None)
    assert "--sandbox workspace-write" in build_cmd
    assert "model_reasoning_effort=\"high\"" in build_cmd  # build 用顶层 effort


def test_write_task_files_review_codex_skips_settings_json(tmp_path):
    """Codex 审稿班同样走固定 nightowl profile，不写 per-task settings.json，
    即便顶层 build runner 是 Claude（混合流水线：CC 施工 + Codex 审稿）。"""
    task_id, config = make_task(project_path="/home/user/projects/demo")  # 顶层 runner=claude
    review_task = _make_review_task(
        task_id, config, review_runner="codex",
        review_model="gpt-5.6-luna", review_effort="high",
    )
    review_task["review"]["runner"] = "codex"
    config = {**config, "runners": CODEX_CONFIG["runners"]}
    launcher.write_task_files(review_task, config, None)
    d = store.task_dir(task_id)
    assert not (d / "settings.json").exists()


def test_codex_resume_thread_id_cross_role_never_resumes():
    """S7：跨角色（build round2 的父班是 review round1）永远不 resume，
    即便父班（角色不同）真的登记过一个 Codex thread_id。"""
    store_home_task = {
        "id": "child", "parent_id": "review-parent",
        "role": "build", "round": 2, "role_shift": 1,  # 角色轮转：role_shift 恒为 1
    }
    store.update_status("review-parent", thread_id="review-thread-xyz")
    assert launcher.codex_resume_thread_id(store_home_task) is None
