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
