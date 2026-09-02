"""启动器：生成 run.sh/settings.json、在 tmux 里开窗口、失败窗口、屏幕快照。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from pathlib import Path

from . import background_runner, store, worktree

__all__ = [
    "CodexTrustError",
    "REVIEW_ALLOWED_TOOLS",
    "REVIEW_DISALLOWED_TOOLS",
    "REVIEW_TOOLS",
    "capture_pane",
    "claude_bin",
    "close_windows",
    "codex_bin",
    "codex_config_path",
    "codex_resume_thread_id",
    "ensure_codex_trusted",
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

# S7：审稿班（无论 Claude 还是 Codex）的只读工具面。Claude 用 --tools 整体
# 收窄可见工具集 + --allowedTools 只放行只读文件工具与参数受限的
# git diff/log/show/status、pytest；再用 --disallowedTools 明确挡
# Write/Edit/NotebookEdit——双保险，不靠单一机制。
REVIEW_TOOLS = "Read,Glob,Grep,Bash"
REVIEW_ALLOWED_TOOLS = (
    "Read", "Glob", "Grep",
    "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git status *)",
    "Bash(python3 -m pytest *)",
)
REVIEW_DISALLOWED_TOOLS = "Write,Edit,NotebookEdit"

# 窗口 id 只认 tmux 的 @N 形状：杜绝任何模糊目标（会话名/窗口名通配）
_WINDOW_ID_RE = re.compile(r"^@\d+$")

# Claude hook 挂的六个事件（设计稿 §4.1）；Codex 是完全独立的另一套
# （SessionStart 而非 PreCompact），固定写在 codex_profile.py 生成的
# nightowl profile 里，不经这张表（那份 profile 内容不能随任务变，见
# codex_profile.py 顶部说明）。
# 总review二 G15（B④-5）：PreCompact 删掉了——hook.py 那支分支只记一行
# "有人开了 compact？"日志，没人读、没有任何调度决策依赖它，每次 compact
# 还多起一个 python 进程，纯浪费。
_HOOK_EVENTS = (
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "PostToolUse",
    "SessionEnd",
)


def claude_bin(config: dict) -> str:
    """要用的 claude 可执行文件；环境变量 NIGHTSHIFT_CLAUDE_BIN 优先（测试用）。

    S6.1 B3：统一从 `store.runner_config(config)["claude"]` 取，不再单独读
    顶层 `config["claude_bin"]`——两处配置一旦不同会出现"校验按新表、启动
    按旧表"的分裂；`runner_config` 的兼容视图本来就是从顶层键合成的，旧
    config 行为不变。
    """
    rc = store.runner_config(config).get("claude") or {}
    return os.environ.get("NIGHTSHIFT_CLAUDE_BIN") or rc.get("bin", "claude")


def codex_bin(config: dict) -> str:
    """要用的 codex 可执行文件；环境变量 NIGHTSHIFT_CODEX_BIN 优先（测试用）。"""
    rc = store.runner_config(config).get("codex") or {}
    return os.environ.get("NIGHTSHIFT_CODEX_BIN") or rc.get("bin", "codex")


def _requires_codex_resume(task: dict) -> bool:
    """这一班是否要求 resume 父班的 Codex thread（S7：跨角色永远新会话，
    只有同角色续班才 resume）。

    role_shift 字段存在（S7 起新建的任务）时按"role_shift > 1"判断——
    只有 create_same_role_successor 会把这个数字往上推，角色轮转
    （create_cross_role_successor）永远从 1 起，天然不要求 resume。
    旧任务（S7 之前落盘，没有这个字段）退回 S6 的老规则：只要有
    parent_id 就必须 resume，不能因为新字段缺失被误判成"角色轮转第一班"
    从而悄悄开一个没有上下文的新会话。
    """
    if "role_shift" in task:
        return int(task.get("role_shift") or 1) > 1
    return bool(task.get("parent_id"))


def codex_resume_thread_id(task: dict) -> str | None:
    """同角色续班要不要 resume 同一个 Codex thread：`_requires_codex_resume`
    判否就直接返回 None（这一班天然该起新会话，不是异常——role_shift 只有
    create_same_role_successor 会推进，天然保证父班同角色，不需要额外
    再查一次父班 task.json 的 role）；判是但父班没登记 thread_id（没起过、
    Claude 父班、或还没等到 SessionStart）一律返回 None，调用方据此
    fail-closed，不能悄悄开一个没有上下文的新会话。"""
    if not _requires_codex_resume(task):
        return None
    parent_id = task.get("parent_id")
    if not parent_id:
        return None
    return store.read_status(parent_id).get("thread_id") or None


def trust_check(project_path: str) -> str:
    """目录信任状态的三态判定：'trusted' / 'untrusted' / 'unreadable'。

    读 NIGHTSHIFT_CLAUDE_JSON（默认 ~/.claude.json）的
    projects[project_path].hasTrustDialogAccepted；文件不存在/键缺 →
    'untrusted'（这个目录确实还没被信任过）。总review二 G5：文件存在但
    解析失败（撕裂读——CC 用 tmp+rename 写这份文件，概率很低但不是零）
    单独算 'unreadable'，跟"真的没信任过"不是一回事：前者该推迟重试，
    后者才是该判死刑的终态失败。只读，永远不写这个文件。
    """
    path = Path(os.environ.get("NIGHTSHIFT_CLAUDE_JSON") or (Path.home() / ".claude.json"))
    if not path.is_file():
        return "untrusted"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return "unreadable"
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return "untrusted"
    entry = projects.get(project_path)
    if not isinstance(entry, dict):
        return "untrusted"
    return "trusted" if entry.get("hasTrustDialogAccepted") is True else "untrusted"


def is_trusted(project_path: str) -> bool:
    """目录是否已在 Claude Code 里点过信任（trust_check 的布尔视图，
    不区分"未信任"和"信任文件暂时读不了"——需要区分的调用方用
    trust_check）。"""
    return trust_check(project_path) == "trusted"


def codex_config_path() -> Path:
    """Codex 的 config.toml 路径：尊重 CODEX_HOME 环境变量（Codex CLI 自己的
    约定，测试把它指到 tmp_path 即可隔离，绝不碰真实 ~/.codex），默认
    ~/.codex/config.toml。"""
    home = os.environ.get("CODEX_HOME")
    base = Path(home) if home else (Path.home() / ".codex")
    return base / "config.toml"


def _toml_quote(value: str) -> str:
    """TOML 基本字符串转义（反斜杠、双引号）：worktree 路径理论上不会带
    引号，但持久化写盘这种"一旦写错就永久卡住无人值守流水线"的操作，值得
    多这一步。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class CodexTrustError(Exception):
    """Codex 信任条目没法安全持久化：config.toml 解析不了，或者同名
    `[projects."<workdir>"]` 已经在但 trust_level 不是 trusted。TOML 不允许同一
    张表声明两次，这时再追加会让**整份** config.toml 解析失败——不只这一班起
    不来，工头自己交互式开的 Codex 也一起挂。宁可这一班启动失败说清原因。"""


def _codex_project_entry(config_path: Path, workdir: str) -> dict | None:
    """config.toml 里 `projects.<workdir>` 那张表；没有这一条（或文件不存在）
    返回 None。文件在但解析失败、`projects` 不是表、条目不是表 → 抛
    CodexTrustError：解析不了的文件上追加一段是盲写（Codex 照样起不来，还让人
    以为信任已处理），不做。"""
    if not config_path.is_file():
        return None
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise CodexTrustError(
            f"Codex config.toml 解析失败，不敢追加信任条目（请人工修复 {config_path}）：{exc}"
        ) from None
    projects = data.get("projects")
    if projects is None:
        return None
    if not isinstance(projects, dict):
        raise CodexTrustError(f"Codex config.toml 的 projects 不是表，不敢追加信任条目：{config_path}")
    entry = projects.get(workdir)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise CodexTrustError(
            f'Codex config.toml 里 projects."{workdir}" 不是表，不敢追加信任条目'
        )
    return entry


def ensure_codex_trusted(workdir: str) -> None:
    """把 workdir 以 `[projects."<workdir>"] trust_level = "trusted"` 持久化
    写进 Codex 的 config.toml（跟 moving/work/ob 几个人工配置的项目同款）。

    S7.6：`_codex_command` 原来靠命令行 `-c projects."<wt>".trust_level=
    "trusted"` 覆盖，监理 2026-08-31 两发受控实测坐实这个覆盖 Codex 的信任
    闸门根本不认——已信任的父根不让 worktree 子目录继承信任，`-c` 覆盖对
    未信任目录照样弹交互式信任对话框；真无人值守下每个新 worktree 的第一次
    Codex 会话都会静默卡死在那个对话框上，没有任何日志能提示。唯一生效的
    机制是持久化写盘（详见 reports/夜班-S7.4-真机smoke.md §9.7）。

    幂等：已经是 trusted 就不重复追加，避免同一路径的 `[projects."..."]`
    段落在 config.toml 里堆积。原子 + 加锁：多个 Codex 任务可能并发起跑并发
    写同一份 config.toml（build 和 review 都会调用这个函数），用文件锁串行
    化，写盘走"临时文件 + os.replace"，锁内先重新确认一次是否已信任（双重
    检查），避免并发场景下重复追加。
    """
    config_path = codex_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = config_path.with_name(f".{config_path.name}.lock")
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            entry = _codex_project_entry(config_path, workdir)
            if entry is not None:
                if entry.get("trust_level") == "trusted":
                    return
                raise CodexTrustError(
                    f'Codex config.toml 已有 [projects."{workdir}"] 但 trust_level='
                    f'{entry.get("trust_level")!r}，不是 trusted；TOML 不允许同名表出现两次，'
                    "追加会让整份 config.toml 解析失败（工头自己的 Codex 也会挂）——"
                    "请人工把这段改成 trusted 或删掉后重跑"
                )
            existing = ""
            if config_path.is_file():
                existing = config_path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
            block = f'[projects."{_toml_quote(workdir)}"]\ntrust_level = "trusted"\n'
            store.atomic_write_text(config_path, existing + block)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
    """Claude Code 的命令行。build 角色字节级保持一期以来的样子（S6 不许
    动，effective_model/effective_effort 对 build 等价于 task['model']/
    task['effort']，不改变输出）；review 角色额外插入只读工具面三件套
    （--tools 收窄可见工具、--allowedTools 只放行只读文件工具与参数受限的
    git diff/log/show/status/pytest、--disallowedTools 明确挡
    Write/Edit/NotebookEdit）。

    S7.1 阻断五：review 角色的 `--permission-mode` 改成 `dontAsk`（build
    角色不变，仍是 `auto`）——`auto` 在这台 CLI 上的语义是"allowedTools
    之外的也无需询问直接放行"，不是"列表外一律拒绝"；`dontAsk` 才是"列表
    外一律拒绝、且不询问"的无人值守语义，配合 --allowedTools/--tools/
    --disallowedTools 三件套才是真正的权限层只读，不能只靠提示词自觉。
    """
    d = store.task_dir(task["id"])
    parts = [
        _sq(claude_bin(config)),
        f"--model {_sq(store.effective_model(task))}",
        f"--effort {_sq(store.effective_effort(task))}",
    ]
    is_review = store.role_of(task) == "review"
    if is_review:
        parts += [
            f"--tools {_sq(REVIEW_TOOLS)}",
            f"--allowedTools {_sq(','.join(REVIEW_ALLOWED_TOOLS))}",
            f"--disallowedTools {_sq(REVIEW_DISALLOWED_TOOLS)}",
        ]
    permission_mode = "dontAsk" if is_review else "auto"
    parts += [
        f"--permission-mode {permission_mode}",
        f"--name {_sq(config['window_prefix'] + task['title'])}",
        f"--session-id {_sq(session_id)}",
        f"--settings {_sq(d / 'settings.json')}",
        # 提示词必须整体包在双引号里：裸 $(cat …) 会被 shell 按空白切词、
        # 展开 $ 与通配符，多行任务内容会打散成一堆参数。
        f"\"$(cat {_sq(d / 'prompt.txt')})\"",
    ]
    return " ".join(parts)


def _codex_command(task: dict, config: dict, workdir: str, resume_thread_id: str | None) -> str:
    """Codex 的命令行（开工令 S6②样例）：resume_thread_id 给了就 resume
    同一个 thread（同角色续班），否则起一个全新会话。build 角色沙箱固定
    workspace-write（字节级不变）；review 角色固定 --sandbox read-only——
    不用 --add-dir（会给额外目录写权限，不是只读挂载），也不靠它交审稿
    文件（Stop hook 在沙箱外原子落盘，见 hook.py）。

    S7.6：命令行 trust 覆盖（`-c projects."<wt>".trust_level="trusted"`）已
    删除——监理实测坐实 Codex 的信任闸门根本不认这个覆盖，留着只会误导人
    以为信任问题已经处理（见 ensure_codex_trusted 的 docstring）。真正的
    信任现在由 launch() 在起会话前调用 ensure_codex_trusted() 持久化写进
    Codex 自己的 config.toml 解决，这里不再需要它。

    总review F12：build 角色（workspace-write 沙箱）额外放开 F12 后台
    登记簿目录的写权限——`sandbox_mode="workspace-write"` 默认只放开
    task cwd，Codex 官方沙箱实测对 task_dir 本身仍是只读（监理 9/2 坐实：
    `codex sandbox -c 'sandbox_mode="workspace-write"' -- touch
    ~/.nightshift/x` 报 Read-only file system），background_runner 起跑
    时第一步就是往 `<task_dir>/background/registry.json` 落盘登记，必炸。
    只放开登记簿目录，不放开整个 task_dir（那样模型能改自己的
    status.json）；review 角色（read-only 沙箱）不加这条，read-only 下
    这个 writable_roots 覆盖没有意义。"""
    d = store.task_dir(task["id"])
    rc = store.runner_config(config).get("codex") or {}
    profile = rc.get("profile", "nightowl")
    effort_override = f'model_reasoning_effort="{store.effective_effort(task)}"'
    is_review = store.role_of(task) == "review"
    sandbox = "read-only" if is_review else "workspace-write"
    parts = [_sq(codex_bin(config))]
    if resume_thread_id:
        parts += ["resume", _sq(resume_thread_id)]
    parts += [
        f"-C {_sq(workdir)}",
        f"--sandbox {sandbox}",
        "--ask-for-approval never",
        f"-m {_sq(store.effective_model(task))}",
        f"-c {_sq(effort_override)}",
    ]
    if not is_review:
        bg_dir_literal = json.dumps(str(background_runner.background_dir(task["id"])))
        writable_roots_override = f"sandbox_workspace_write.writable_roots=[{bg_dir_literal}]"
        parts.append(f"-c {_sq(writable_roots_override)}")
    parts += [
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
    # S7：审稿班可能跟施工班用不同的 runner（task.review.runner），命令行/
    # 环境导出一律按这一班自己的有效工人，不能只看顶层 build runner。
    runner = store.effective_runner(task)
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

    S6.1 A1：Codex 任务同样保证带上 F12 后台协议前言（用 background_runner
    wrapper、不要裸 fork/nohup）——这是运行时兜底，跟 worktree 那条同一个
    模式：不管 config.prompt_template/chain_template 有没有同步更新，也不管
    是新会话/续班/用户自己 --prompt-file 给的全文，Codex 任务的 prompt.txt
    永远且只会出现一次这段协议。
    """
    text = task["prompt_final"]
    # S7：工作树安全前言只对 build 角色追加——review 角色本来就只读、不
    # commit，review_template 自己的措辞已经说清楚，硬套"调度器会打存档点"
    # 这句反而误导（审稿班不产生存档点）。
    if (
        worktree.wants_worktree(task)
        and store.role_of(task) == "build"
        and store.WORKTREE_INSTRUCTION not in text
    ):
        text = store.WORKTREE_INSTRUCTION + "\n\n" + text
    # S7.1 阻断五：F12 后台协议只适用于可写的 build 角色（起长任务、等
    # 后台完成）——review 角色只读、不该起后台进程，硬塞这段协议只会诱导
    # 它去做不该做的事。
    if (
        store.effective_runner(task) == "codex"
        and store.role_of(task) == "build"
        and store.CODEX_BACKGROUND_INSTRUCTION not in text
    ):
        text = store.CODEX_BACKGROUND_INSTRUCTION + "\n\n" + text
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
    if store.effective_runner(task) != "codex":
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
    # S7：这一班自己的有效工人（review 角色可能跟顶层 build runner 不同），
    # 信任检查/session 生成方式/thread 记账全部按它来，不能只看 task["runner"]。
    runner = store.effective_runner(task)

    # ① 目录没信任过，交互式 claude 会卡在信任问答 → 直接判失败。
    # 只查主项目路径：信任预检不要求工作树单独出现在 ~/.claude.json 里。
    # Codex 不吃这份文件（它自己的信任状态在 ~/.codex/config.toml），这里
    # 对 codex 任务不做这个检查——Codex 走的是下面 ①''' 的持久化写盘。
    if runner == "claude" and not is_trusted(project_path):
        reason = f"目录未信任，请先手动在该目录开一次 claude：{project_path}"
        store.append_event(task_id, f"启动被拦：{reason}")
        status = store.update_status(
            task_id, state="failed", error=reason, last_event_at=store.utc_now_iso()
        )
        open_failure_window(task, reason, config)
        return status

    # ①' S5：工作树任务先幂等建树/复用（直接 CLI run-now 也走这里，绕不过）。
    # 元数据落 status，后继班靠 create_same_role_successor 沿用同一棵树
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
    if runner == "codex" and _requires_codex_resume(task):
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
        # S6.1 A7：父班窗口必须先确认不在了才能 resume 同一个 thread——
        # _chain_continue 续班时已经尝试关过父窗，但关闭可能失败（tmux 抽风/
        # 窗口刚好在被别的东西占用）；这里是最后一道防线，宁可这一班启动
        # 失败也不让父窗和这个新窗口同时持有同一个 Codex thread（两开）。
        parent_status = store.read_status(task["parent_id"])
        parent_window_id = parent_status.get("window_id")
        if parent_window_id and window_alive(str(parent_window_id), config):
            reason = (
                f"父班窗口 {parent_window_id} 仍然存活，"
                "拒绝在新窗口 resume 同一个 Codex thread（防止两开）"
            )
            store.append_event(task_id, f"启动被拦：{reason}")
            status = store.update_status(
                task_id, state="failed", error=reason,
                last_event_at=store.utc_now_iso(),
            )
            open_failure_window(task, reason, config)
            return status

    # ①''' S7.6：Codex 会话开始前，把这一班的工作目录持久化写进
    # ~/.codex/config.toml 的信任表——命令行 -c projects...trust_level 覆盖
    # 对 Codex 的信任闸门无效（监理受控实测坐实，见 reports/夜班-S7.4-真机
    # smoke.md §9.7），只有持久化写盘才能免交互；不分 build/review 角色，
    # 两边都要（review 的 --sandbox read-only 同样会撞信任对话框）。写盘
    # 失败（权限/磁盘问题）直接判这一班失败，不要在信任缺失的情况下继续
    # 起一个注定会静默卡死的窗口。
    if runner == "codex":
        try:
            ensure_codex_trusted(workdir_for(task, config))
        except (OSError, CodexTrustError) as exc:
            reason = f"写 Codex 信任配置失败：{exc}"
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
    # 总review F12：F12 后台登记簿目录必须由调度器侧预建——Codex 的
    # workspace-write 沙箱对 task_dir 本身是只读的（沙箱内 mkdir 不了），
    # background_runner 起跑时第一步就要往这个目录下落盘登记簿，等它自己
    # 建就晚了。两家 runner 都建，反正只是个空目录，无害。
    background_runner.background_dir(task_id).mkdir(parents=True, exist_ok=True)
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


def _tmux_stdin(text: str, *args) -> subprocess.CompletedProcess:
    """带 stdin 的 tmux 调用（只给 `load-buffer -` 用）：同 _tmux 的 10 秒超时语义。
    单独一个函数而不是给 _tmux 加参数——测试里用 `lambda *a` 桩掉 _tmux 的地方
    不少，多一个关键字参数会把它们全部炸掉。"""
    try:
        return subprocess.run(
            ["tmux", *args], input=text, capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return subprocess.CompletedProcess(args, 124, "", f"tmux 超时：{stderr[-500:]}")


# 文本落进 pane 之后、单独发 Enter 之前的间隔。CC 的输入解析器把一次读到的多字符
# 块当粘贴处理，块里的回车变成换行（9/1 靶测坐实：文本与 Enter 放同一条 tmux
# 命令永远提交不了）；分开发之后，Enter 只要落在解析器的 NORMAL_TIMEOUT
# （CC 2.1.257 二进制里 NORMAL_TIMEOUT=50 ms）之外就是一次独立按键。这里不用
# paste-buffer 的 -p（不带括号粘贴序列），所以 PASTE_TIMEOUT=2000 ms 那个
# IN_PASTE 模式不会进。0.3 秒留足余量，测试可把它改成 0。
_SEND_ENTER_DELAY_SECONDS = 0.3
# 调度器线程与 HTTP 线程同进程：文本与 Enter 之间不许被别的按键插队
# （捎话/中止/停后台都走这一个入口或 send_escape）。
_send_lock = threading.Lock()


def send_keys(window_id: str, text: str) -> subprocess.CompletedProcess:
    """往窗口的 pane 敲一段文本再回车——所有往会话里塞文字的地方（捎话、保活、
    额度停/续、我来看/继续、审稿意见回传、F12 唤醒、自检提示）都走这里。

    三步，任何一步失败就停在那一步并返回它的 CompletedProcess（文本没进去
    绝不再发 Enter——那会把 pane 里现有的半截输入提交出去）：
    1. `load-buffer -b <一次性名字> -`：文本走 stdin，不经 tmux 命令行解析。
       send-keys 把文本当命令行参数有四个坑（tmux 3.4 本机实测）：单参数超过
       约 16 KB 报 "command too long"（审稿意见回传的返工文案就会撞）；以 `-`
       开头被当 flag 报 "unknown flag"；尾部 ASCII `;` 被当命令分隔符（静默吞掉，
       后面跟 Enter 时整条报 "unknown command: Enter"）；文本恰好是键名
       （Enter/Space/Tab…）被当按键。stdin 路线四个坑一起绕开。
    2. `paste-buffer -d -r -b <名字> -t <窗口>`：写进 pane，-d 用完即删，-r 保留
       换行原样（不换成回车，跟以前 send-keys 送出的字节一致）。
    3. 隔 _SEND_ENTER_DELAY_SECONDS 再单独 `send-keys Enter`（见常量说明）。
    """
    wid = str(window_id)
    with _send_lock:
        if text:
            buf = f"ns-{uuid.uuid4().hex[:12]}"
            proc = _tmux_stdin(text, "load-buffer", "-b", buf, "-")
            if proc.returncode != 0:
                return proc
            proc = _tmux("paste-buffer", "-d", "-r", "-b", buf, "-t", wid)
            if proc.returncode != 0:
                _tmux("delete-buffer", "-b", buf)
                return proc
            time.sleep(_SEND_ENTER_DELAY_SECONDS)
        return _tmux("send-keys", "-t", wid, "Enter")


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
    """进程是否还活着（os.kill 探测，PID 复用靠三条件组合兜底，这里是其一）。

    总review二 G15（B④-2）：以前多一个 `except PermissionError: return True`
    ——nightshift/hook/所有会话全是 root，`os.kill(pid, 0)` 对任何进程都
    不会因权限不够而报错，这个分支进不去，删了对当前单用户 root 部署零
    行为变化。将来要是以非 root 身份跑，得先重做整套进程围栏（不止这一处），
    到时候再加回来。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def capture_pane(window_id: str, lines: int = 200) -> str:
    """抓窗口最近 N 行屏幕文本；抓不到返回空串。"""
    proc = _tmux("capture-pane", "-p", "-t", str(window_id), "-S", f"-{lines}")
    if proc.returncode != 0:
        return ""
    return proc.stdout
