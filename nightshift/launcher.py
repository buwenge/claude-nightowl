"""启动器：生成 run.sh/settings.json、在 tmux 里开窗口、失败窗口、屏幕快照。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from . import store, worktree

__all__ = [
    "capture_pane",
    "claude_bin",
    "close_windows",
    "codex_bin",
    "codex_resume_thread_id",
    "ensure_tmux_session",
    "hook_settings",
    "is_trusted",
    "launch",
    "open_failure_window",
    "open_notice_window",
    "pid_alive",
    "send_escape",
    "send_keys",
    "window_alive",
    "workdir_for",
    "write_task_files",
]

# 窗口 id 只认 tmux 的 @N 形状：杜绝任何模糊目标（会话名/窗口名通配）
_WINDOW_ID_RE = re.compile(r"^@\d+$")

# Claude hook 挂的七个事件（设计稿 §4.1）；Codex 的七件套固定写在
# codex_profile.py 生成的 nightowl profile 里，不经这张表（那份 profile
# 内容不能随任务变，见 codex_profile.py 顶部说明）
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


def codex_bin(config: dict) -> str:
    """要用的 codex 可执行文件；环境变量 NIGHTSHIFT_CODEX_BIN 优先（测试用）。"""
    rc = store.runner_config(config).get("codex") or {}
    return os.environ.get("NIGHTSHIFT_CODEX_BIN") or rc.get("bin", "codex")


def codex_resume_thread_id(task: dict) -> str | None:
    """同角色续班要不要 resume 同一个 Codex thread：只有这一班是某个父班的
    后继（task.parent_id 存在）时才查；父班没登记 thread_id（没起过、
    Claude 父班、或还没等到 SessionStart）一律返回 None，调用方据此
    fail-closed，不能悄悄开一个没有上下文的新会话。"""
    parent_id = task.get("parent_id")
    if not parent_id:
        return None
    return store.read_status(parent_id).get("thread_id") or None


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


def workdir_for(task: dict, config: dict) -> str:
    """claude 实际施工的目录：工作树任务用 status 里登记的 worktree_path
    （launch 已幂等建好树）；老式任务仍是 config 里的项目主目录。"""
    if worktree.wants_worktree(task):
        wt = store.read_status(task["id"]).get("worktree_path")
        if wt:
            return str(wt)
    return str(config["projects"][task["project"]])


def _claude_command(task: dict, config: dict, session_id: str) -> str:
    """Claude Code 的命令行，字节级保持一期以来的样子（S6 不许动）。"""
    d = store.task_dir(task["id"])
    return " ".join([
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
    ])


def _codex_command(task: dict, config: dict, workdir: str, resume_thread_id: str | None) -> str:
    """Codex 的命令行（开工令 S6②样例）：resume_thread_id 给了就 resume
    同一个 thread（同角色续班），否则起一个全新会话。"""
    d = store.task_dir(task["id"])
    rc = store.runner_config(config).get("codex") or {}
    profile = rc.get("profile", "nightowl")
    trust_override = f'projects."{workdir}".trust_level="trusted"'
    effort_override = f'model_reasoning_effort="{task["effort"]}"'
    parts = [_sq(codex_bin(config))]
    if resume_thread_id:
        parts += ["resume", _sq(resume_thread_id)]
    parts += [
        f"-C {_sq(workdir)}",
        "--sandbox workspace-write",
        "--ask-for-approval never",
        f"-m {_sq(task['model'])}",
        f"-c {_sq(effort_override)}",
        f"-c {_sq(trust_override)}",
        f"--profile {_sq(profile)}",
        f"\"$(cat {_sq(d / 'prompt.txt')})\"",
    ]
    return " ".join(parts)


def run_sh_text(
    task: dict, config: dict, session_id: str | None,
    *, resume_thread_id: str | None = None,
) -> str:
    """run.sh 的内容（模板见开工令）：环境、cgroup 内存围栏、起工人、留窗。

    session_id 的语义按 runner 分叉：
    - claude：launcher 起跑前预先分配的 UUID，透传 --session-id（不变）；
    - codex：还不知道（要等 SessionStart hook 报），这里恒定不用它，
      resume 与否单独由 resume_thread_id 决定。
    """
    d = store.task_dir(task["id"])
    workdir = workdir_for(task, config)
    repo_root = Path(__file__).resolve().parent.parent
    runner = task.get("runner") or "claude"
    lines = [
        "#!/bin/bash",
        f"# nightshift 任务 {task['id']}：{task['title']}",
        f"export NIGHTSHIFT_HOME={_sq(store.home())}",
        f"export PYTHONPATH={_sq(repo_root)}",
        f"export NIGHTOWL_TASK_ID={_sq(task['id'])}",
        f"export NIGHTOWL_RUNNER={_sq(runner)}",
        "unset CLAUDECODE",
        f"cd {_sq(workdir)} || {{ echo \"[nightshift] 进不了施工目录\"; read; exit 1; }}",
        f"CGROUP=\"/sys/fs/cgroup/nightshift-{task['id']}\"",
        'if mkdir "$CGROUP" 2>/dev/null; then',
        f"    echo {config['memory_max_bytes']} > \"$CGROUP/memory.max\" 2>/dev/null",
        '    echo $$ > "$CGROUP/cgroup.procs" 2>/dev/null',
        "fi",
    ]
    if runner == "codex":
        lines.append(_codex_command(task, config, workdir, resume_thread_id))
        exit_label = "codex"
    else:
        lines.append(_claude_command(task, config, str(session_id)))
        exit_label = "claude"
    lines += [
        "code=$?",
        # 工人死透的铁证：调度器靠它识破"read 留窗"的假活（宽限期内也能重试）
        f'echo "$code" > {_sq(d / "exit_code")}',
        f'echo "[nightshift] {exit_label} 已退出（退出码 $code）。窗口保留，按回车关闭。"',
        "read",
    ]
    return "\n".join(lines) + "\n"


def _prompt_text(task: dict) -> str:
    """真正写进 prompt.txt 的提示词。

    工作树任务保证带上运行时安全前言（不要 commit、只在工作树施工）：
    模板经 {worktree_instruction} 渲染过就已有这句；用户用 --prompt-file
    给的全文若漏了它，在这里补上——前言只追加、绝不改用户正文。
    """
    text = task["prompt_final"]
    if worktree.wants_worktree(task) and store.WORKTREE_INSTRUCTION not in text:
        text = store.WORKTREE_INSTRUCTION + "\n\n" + text
    return text


def write_task_files(
    task: dict, config: dict, session_id: str | None,
    *, resume_thread_id: str | None = None,
) -> None:
    """写 prompt.txt / settings.json / run.sh（0o700）。

    settings.json（Claude Code 的 per-task hook 配置）只有 Claude 任务才写：
    Codex 走固定的 nightowl profile（codex_profile.py），不需要这份文件，
    写了反而误导人以为 Codex 也在用它。
    """
    d = store.task_dir(task["id"])
    d.mkdir(parents=True, exist_ok=True)
    # 上一轮窗口留下的 exit_code 先删：那是旧工人的死讯，不能拿来误判新窗口
    (d / "exit_code").unlink(missing_ok=True)
    if (task.get("runner") or "claude") != "codex":
        store.atomic_write_json(d / "settings.json", hook_settings(task["id"]))
    store.atomic_write_text(d / "prompt.txt", _prompt_text(task))
    run_sh = d / "run.sh"
    store.atomic_write_text(
        run_sh, run_sh_text(task, config, session_id, resume_thread_id=resume_thread_id)
    )
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
    """起一个任务的窗口。顺序是硬性的：信任检查 → 工作树建树 → 预定
    session → 落盘 → 先写 launching 再碰 tmux（崩溃恢复三条的根基，设计稿 §3）。
    """
    task = store.load_task(task_id)
    project_path = config["projects"][task["project"]]
    runner = task.get("runner") or "claude"

    # ① 目录没信任过，交互式 claude 会卡在信任问答 → 直接判失败。
    # 只查主项目路径：信任预检不要求工作树单独出现在 ~/.claude.json 里。
    # Codex 不吃这份文件（它自己的信任状态在 ~/.codex/config.toml），信任
    # 覆盖每次都显式带在命令行上（_codex_command 的 trust_override），
    # 这里对 codex 任务不做这个检查。
    if runner == "claude" and not is_trusted(project_path):
        reason = f"目录未信任，请先手动在该目录开一次 claude：{project_path}"
        store.append_event(task_id, f"启动被拦：{reason}")
        status = store.update_status(
            task_id, state="failed", error=reason, last_event_at=store.utc_now_iso()
        )
        open_failure_window(task, reason, config)
        return status

    # ①' S5：工作树任务先幂等建树/复用（直接 CLI run-now 也走这里，绕不过）。
    # 元数据落 status，后继班靠 create_successor 沿用同一棵树
    if worktree.wants_worktree(task):
        try:
            meta = worktree.ensure_worktree(task, project_path)
        except worktree.WorktreeError as exc:
            reason = f"建工作树失败：{exc}"
            store.append_event(task_id, f"启动被拦：{reason}")
            status = store.update_status(
                task_id, state="failed", error=reason,
                last_event_at=store.utc_now_iso(),
            )
            open_failure_window(task, reason, config)
            return status
        store.update_status(task_id, **meta)

    # ①'' S6：Codex 同角色续班要 resume 父班的 thread；父班没留下 thread_id
    # 就 fail-closed（不能悄悄开一个没有上下文的新会话）。
    resume_thread_id = None
    if runner == "codex" and task.get("parent_id"):
        resume_thread_id = codex_resume_thread_id(task)
        if not resume_thread_id:
            reason = (
                "Codex 续班找不到父班登记的 thread_id，"
                "拒绝悄悄开一个没有上下文的新会话"
            )
            store.append_event(task_id, f"启动被拦：{reason}")
            status = store.update_status(
                task_id, state="failed", error=reason,
                last_event_at=store.utc_now_iso(),
            )
            open_failure_window(task, reason, config)
            return status

    # ② 预定 session_id 与 transcript 路径。
    # Claude：--session-id 决定文件名（设计稿 F4），编码路径按实际 cwd 算
    #   （工作树任务在 <项目>/.claude/worktrees/<slug> 里跑）；
    # Codex：新会话时它自己起 thread id，起跑前不知道，session_id/transcript
    #   留空等 SessionStart hook 报；resume 时 session_id 就是 resume_thread_id。
    if runner == "codex":
        session_id = resume_thread_id
        expected_transcript = None
    else:
        session_id = str(uuid.uuid4())
        encoded = workdir_for(task, config).replace("/", "-").replace(".", "-")
        expected_transcript = str(
            Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
        )

    # ③ 任务三件套落盘
    write_task_files(task, config, session_id, resume_thread_id=resume_thread_id)

    # ④ 先落盘 launching（含预订的 session/transcript），再去碰 tmux
    extra_fields = {}
    if runner == "codex":
        # resume 时提前把 thread_id 坐实（SessionStart 不会重新触发，见靶测
        # 记录第 6 项）；新会话先置 None，等 SessionStart hook 报了再补
        extra_fields["thread_id"] = session_id
        extra_fields["quota_source"] = "codex"
    store.update_status(
        task_id,
        state="launching",
        launched_at=store.utc_now_iso(),
        session_id=session_id,
        transcript_path=expected_transcript,
        window_id=None,
        pane_pid=None,
        error=None,            # 上一次失败/推迟的原因到此作废，别在卡片上赖着（8/27 工头看见旧红字）
        postpone_reason=None,
        last_event_at=store.utc_now_iso(),
        **extra_fields,
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


def send_escape(window_id: str) -> subprocess.CompletedProcess:
    """往窗口的 pane 按一下 Esc（中止：让 CC 停下当前轮次，不改任务状态）。"""
    return _tmux("send-keys", "-t", str(window_id), "Escape")


def window_alive(window_id: str, config: dict) -> bool:
    """window_id 是否还在 tmux 会话的窗口列表里。"""
    proc = _tmux("list-windows", "-t", f"{config['tmux_session']}:", "-F", "#{window_id}")
    if proc.returncode != 0:
        return False
    return window_id in proc.stdout.splitlines()


def close_windows(window_ids, config: dict) -> list[str]:
    """精确关闭任务记录里登记的窗口（S5② 合并/丢弃后的收尾）。

    只认 @N 形状的 window id，且先用 window_alive 确认它还在本会话的窗口
    列表里再 kill-window——绝不 kill-session、绝不碰名为 claude 的会话或
    没登记的窗口。返回真正关掉的 id。
    """
    closed: list[str] = []
    for raw in window_ids or []:
        wid = str(raw)
        if not _WINDOW_ID_RE.match(wid):
            continue
        if not window_alive(wid, config):
            continue
        proc = _tmux("kill-window", "-t", wid)
        if proc.returncode == 0:
            closed.append(wid)
    return closed


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
