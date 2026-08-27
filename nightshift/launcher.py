"""启动器：生成 run.sh/settings.json、在 tmux 里开窗口、失败窗口、屏幕快照。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from . import store

__all__ = [
    "capture_pane",
    "claude_bin",
    "ensure_tmux_session",
    "hook_settings",
    "is_trusted",
    "launch",
    "open_failure_window",
    "open_notice_window",
    "pid_alive",
    "send_keys",
    "window_alive",
    "write_task_files",
]

# hook 挂的七个事件（设计稿 §4.1）
_HOOK_EVENTS = (
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "PostToolUse",
    "PreCompact",
    "SessionEnd",
)


def claude_bin(config: dict) -> str:
    """要用的 claude 可执行文件；环境变量 NIGHTSHIFT_CLAUDE_BIN 优先（测试用）。"""
    return os.environ.get("NIGHTSHIFT_CLAUDE_BIN") or config["claude_bin"]


def is_trusted(project_path: str) -> bool:
    """目录是否已在 Claude Code 里点过信任。

    读 NIGHTSHIFT_CLAUDE_JSON（默认 ~/.claude.json）的
    projects[project_path].hasTrustDialogAccepted；文件缺/键缺 → False。
    只读，永远不写这个文件。
    """
    path = Path(os.environ.get("NIGHTSHIFT_CLAUDE_JSON") or (Path.home() / ".claude.json"))
    if not path.is_file():
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return False
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    entry = projects.get(project_path)
    if not isinstance(entry, dict):
        return False
    return entry.get("hasTrustDialogAccepted") is True


def hook_settings(task_id: str) -> dict:
    """单个任务专属的 hook 配置，形状与 Claude Code settings 的 hooks 一致。"""

    def entry(event: str) -> dict:
        return {
            "type": "command",
            "command": f"{sys.executable} -m nightshift.hook {task_id} {event}",
            "timeout": 10,
        }

    return {"hooks": {event: [{"hooks": [entry(event)]}] for event in _HOOK_EVENTS}}


def _sq(value) -> str:
    """把值安全地放进 shell 单引号里。"""
    return "'" + str(value).replace("'", "'\\''") + "'"


def run_sh_text(task: dict, config: dict, session_id: str) -> str:
    """run.sh 的内容（模板见开工令）：环境、cgroup 内存围栏、起 claude、留窗。"""
    d = store.task_dir(task["id"])
    project_path = config["projects"][task["project"]]
    repo_root = Path(__file__).resolve().parent.parent
    lines = [
        "#!/bin/bash",
        f"# nightshift 任务 {task['id']}：{task['title']}",
        f"export NIGHTSHIFT_HOME={_sq(store.home())}",
        f"export PYTHONPATH={_sq(repo_root)}",
        "unset CLAUDECODE",
        f"cd {_sq(project_path)} || {{ echo \"[nightshift] 进不了项目目录\"; read; exit 1; }}",
        f"CGROUP=\"/sys/fs/cgroup/nightshift-{task['id']}\"",
        'if mkdir "$CGROUP" 2>/dev/null; then',
        f"    echo {config['memory_max_bytes']} > \"$CGROUP/memory.max\" 2>/dev/null",
        '    echo $$ > "$CGROUP/cgroup.procs" 2>/dev/null',
        "fi",
        " ".join([
            _sq(claude_bin(config)),
            f"--model {_sq(task['model'])}",
            f"--effort {_sq(task['effort'])}",
            "--permission-mode auto",
            f"--name {_sq(config['window_prefix'] + task['title'])}",
            f"--session-id {_sq(session_id)}",
            f"--settings {_sq(d / 'settings.json')}",
            # 提示词必须整体包在双引号里：裸 $(cat …) 会被 shell 按空白切词、
            # 展开 $ 与通配符，多行任务内容会打散成一堆参数。
            f"\"$(cat {_sq(d / 'prompt.txt')})\"",
        ]),
        "code=$?",
        # claude 死透的铁证：调度器靠它识破"read 留窗"的假活（宽限期内也能重试）
        f'echo "$code" > {_sq(d / "exit_code")}',
        'echo "[nightshift] claude 已退出（退出码 $code）。窗口保留，按回车关闭。"',
        "read",
    ]
    return "\n".join(lines) + "\n"


def write_task_files(task: dict, config: dict, session_id: str) -> None:
    """写 prompt.txt / settings.json / run.sh（0o700）。"""
    d = store.task_dir(task["id"])
    d.mkdir(parents=True, exist_ok=True)
    # 上一轮窗口留下的 exit_code 先删：那是旧 claude 的死讯，不能拿来误判新窗口
    (d / "exit_code").unlink(missing_ok=True)
    store.atomic_write_json(d / "settings.json", hook_settings(task["id"]))
    store.atomic_write_text(d / "prompt.txt", task["prompt_final"])
    run_sh = d / "run.sh"
    store.atomic_write_text(run_sh, run_sh_text(task, config, session_id))
    os.chmod(run_sh, 0o700)


def _tmux(*args) -> subprocess.CompletedProcess:
    """所有 tmux 调用都走这里：10 秒超时，超时当作失败返回。"""
    try:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return subprocess.CompletedProcess(args, 124, "", f"tmux 超时：{stderr[-500:]}")


def ensure_tmux_session(name: str) -> subprocess.CompletedProcess:
    """会话存在就不管（`=名字` 精确匹配），不存在就 -d 起一个。"""
    proc = _tmux("has-session", "-t", f"={name}")
    if proc.returncode == 0:
        return proc
    return _tmux("new-session", "-d", "-s", name)


def _fail(task: dict, config: dict, error: str) -> dict:
    status = store.update_status(
        task["id"], state="failed", error=error, last_event_at=store.utc_now_iso()
    )
    store.append_event(task["id"], f"启动失败：{error}")
    open_failure_window(task, error, config)
    return status


def launch(task_id: str, config: dict) -> dict:
    """起一个任务的窗口。顺序是硬性的：信任检查 → 预定 session → 落盘 → 先写
    launching 再碰 tmux（崩溃恢复三条的根基，设计稿 §3）。
    """
    task = store.load_task(task_id)
    project_path = config["projects"][task["project"]]

    # ① 目录没信任过，交互式 claude 会卡在信任问答 → 直接判失败
    if not is_trusted(project_path):
        reason = f"目录未信任，请先手动在该目录开一次 claude：{project_path}"
        store.append_event(task_id, f"启动被拦：{reason}")
        status = store.update_status(
            task_id, state="failed", error=reason, last_event_at=store.utc_now_iso()
        )
        open_failure_window(task, reason, config)
        return status

    # ② 预定 session_id 与 transcript 路径（--session-id 决定文件名，设计稿 F4）
    session_id = str(uuid.uuid4())
    encoded = str(project_path).replace("/", "-").replace(".", "-")
    expected_transcript = str(
        Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
    )

    # ③ 任务三件套落盘
    write_task_files(task, config, session_id)

    # ④ 先落盘 launching（含预订的 session/transcript），再去碰 tmux
    store.update_status(
        task_id,
        state="launching",
        launched_at=store.utc_now_iso(),
        session_id=session_id,
        transcript_path=expected_transcript,
        window_id=None,
        pane_pid=None,
        last_event_at=store.utc_now_iso(),
    )

    # ⑤ tmux 会话兜底（服务器重启后没有会话）
    session = config["tmux_session"]
    proc = ensure_tmux_session(session)
    if proc.returncode != 0:
        return _fail(task, config, f"tmux 会话 {session} 起不来：{proc.stderr.strip()}")

    # ⑥ 开窗口拿 window_id
    run_sh = str(store.task_dir(task_id) / "run.sh")
    window_name = f"{config['window_prefix']}{task['title']}"
    # -t 必须写成 "会话名:"（带冒号）：不带冒号时 tmux 会先按窗口名解析，
    # 会话里恰好有个同名窗口就会落到那个 index 上报 "index N in use"（8/27 真机踩到）。
    proc = _tmux(
        "new-window", "-d", "-P", "-F", "#{window_id}", "-t", f"{session}:",
        "-n", window_name, run_sh,
    )
    if proc.returncode != 0:
        return _fail(task, config, f"tmux new-window 失败：{proc.stderr.strip()}")
    window_id = proc.stdout.strip()

    # ⑦ 拿 pane_pid
    proc = _tmux("list-panes", "-t", window_id, "-F", "#{pane_pid}")
    if proc.returncode != 0:
        return _fail(task, config, f"tmux list-panes 失败：{proc.stderr.strip()}")
    try:
        pane_pid = int(proc.stdout.strip())
    except ValueError:
        return _fail(task, config, f"pane_pid 认不出来：{proc.stdout!r}")

    # ⑧ 记账
    status = store.update_status(
        task_id,
        window_id=window_id,
        pane_pid=pane_pid,
        last_event_at=store.utc_now_iso(),
    )
    store.append_event(
        task_id, f"已开窗口 {window_id}（pane {pane_pid}）session={session_id}"
    )
    return status


def open_notice_window(
    task: dict, suffix: str, lines: list[str], config: dict
) -> None:
    """开一个通知窗口写清事情（失败/推迟共用）；tmux 不可用就只记 events.log。

    suffix 是窗口名后缀（如 "(失败)" / "(推迟)"），lines 是正文行（已含标签）。
    """
    task_id = task["id"]
    title = task.get("title") or task_id
    d = store.task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    script = d / "notice.sh"
    out = [
        "#!/bin/bash",
        "# nightshift 通知窗口（失败/推迟共用）",
        f"echo {_sq(f'[nightshift] 任务 {task_id}（{title}）{suffix}')}",
        "echo -n '[nightshift] 时间：'; date '+%Y-%m-%d %H:%M:%S %Z'",
    ]
    out += [f"echo {_sq(line)}" for line in lines]
    out += [
        "echo",
        "echo '[nightshift] 按回车关闭。'",
        "read",
    ]
    store.atomic_write_text(script, "\n".join(out) + "\n")
    os.chmod(script, 0o700)

    session = config["tmux_session"]
    proc = ensure_tmux_session(session)
    if proc.returncode != 0:
        store.append_event(task_id, f"通知窗口开不了（会话起不来）：{proc.stderr.strip()}")
        return
    proc = _tmux(
        "new-window", "-d", "-t", f"{session}:",
        "-n", f"{config['window_prefix']}{title}{suffix}", str(script),
    )
    if proc.returncode != 0:
        store.append_event(task_id, f"通知窗口开不了：{proc.stderr.strip()}")
    else:
        store.append_event(task_id, f"已开通知窗口（{suffix}）")


def open_failure_window(task: dict, reason: str, config: dict) -> None:
    """开一个红字窗口写清失败原因；tmux 本身不可用就只记 events.log。"""
    open_notice_window(task, "(失败)", [f"原因：{reason}"], config)


def send_keys(window_id: str, text: str) -> subprocess.CompletedProcess:
    """往窗口的 pane 敲一段文本加回车（保活戳用）。"""
    return _tmux("send-keys", "-t", str(window_id), text, "Enter")


def window_alive(window_id: str, config: dict) -> bool:
    """window_id 是否还在 tmux 会话的窗口列表里。"""
    proc = _tmux("list-windows", "-t", f"{config['tmux_session']}:", "-F", "#{window_id}")
    if proc.returncode != 0:
        return False
    return window_id in proc.stdout.splitlines()


def pid_alive(pid: int) -> bool:
    """进程是否还活着（os.kill 探测，PID 复用靠三条件组合兜底，这里是其一）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def capture_pane(window_id: str, lines: int = 200) -> str:
    """抓窗口最近 N 行屏幕文本；抓不到返回空串。"""
    proc = _tmux("capture-pane", "-p", "-t", str(window_id), "-S", f"-{lines}")
    if proc.returncode != 0:
        return ""
    return proc.stdout
