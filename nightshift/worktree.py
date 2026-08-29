"""工作树：建树/复用、识别新旧任务、启动对账（Git 子进程全部集中在这）。

规矩（开工令 S5）：
- 所有 Git 调用一律参数数组 + capture_output + text + 有限超时，绝不拼 shell；
- 错误只回传 stdout/stderr 尾部，不把大输出塞进状态或网页；
- 树的位置固定 <project>/.claude/worktrees/<slug>，分支固定 ns/<slug>；
- 建树幂等：路径/分支/项目都对得上就复用；只撞一半或元数据矛盾判失败；
- 对账只认分支以 refs/heads/ns/ 开头的树，绝不自动删除任何东西。

存档点/合并/丢弃在 S5② 加进来（同一模块，不散落到 server/scheduler/launcher）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import store

__all__ = [
    "GitError",
    "WorktreeError",
    "ensure_exclude",
    "ensure_worktree",
    "is_git_repo",
    "list_worktrees",
    "reconcile_all",
    "reconcile_project",
    "slug_for",
    "wants_worktree",
    "worktree_path_for",
]

# 项目里放工作树的固定位置（相对项目根）
WORKTREES_DIR = Path(".claude") / "worktrees"
# 夜班管理的分支前缀（对账只认它）
BRANCH_PREFIX = "refs/heads/ns/"
# slug 里标题部分兜底与总长上限
_SLUG_MAX = 64
_GIT_TIMEOUT = 60.0
_ERROR_TAIL = 400  # Git 报错只带尾部，别把大输出塞进状态/网页


class WorktreeError(Exception):
    """工作树相关失败（人话原因，可直接进 status.error / 网页红字）。"""


class GitError(WorktreeError):
    """Git 子进程失败：带命令名、退出码与 stderr 尾部。"""

    def __init__(self, args: list[str], returncode: int, output: str):
        self.args = args
        self.returncode = returncode
        self.output = output
        tail = (output or "").strip()[-_ERROR_TAIL:]
        super().__init__(f"git {args[0]} 失败（exit {returncode}）：{tail or '无输出'}")


# ---------- 识别新旧任务（真正实现在 store，这里转一手集中对外） ----------


def wants_worktree(task: dict) -> bool:
    """task.json 该不该走工作树路径：只有显式 true 才是；旧记录缺字段按 false
    （一期路径：直接在项目目录跑，不建树、不打存档点）。"""
    return store.worktree_enabled(task)


# ---------- 安全 Git runner ----------


def _git(cwd: str | Path, *args: str, timeout: float = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """参数数组调 Git：绝不 shell=True，输出全捕获，有限超时。"""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise WorktreeError(
            f"git {args[0]} 超时（>{timeout:.0f} 秒），先放弃不动任何东西"
        ) from None


def git_out(cwd: str | Path, *args: str, timeout: float = _GIT_TIMEOUT) -> str:
    """跑一条必须成功的 Git 命令，返回 stdout；失败抛 GitError（只带尾部）。"""
    proc = _git(cwd, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr or proc.stdout)
    return proc.stdout


def is_git_repo(project_path: str | Path) -> bool:
    """目录是否在一个 Git 仓库工作树里。"""
    return _git(project_path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def head_sha(project_path: str | Path) -> str:
    """项目当前 HEAD 的完整 sha（建树的 base_ref）。"""
    return git_out(project_path, "rev-parse", "HEAD").strip()


# ---------- slug 与路径 ----------


def slug_for(task_id: str, title: str) -> str:
    """稳定安全的 slug：`<任务 id 最后 4 位>-<标题 ASCII slug>`。

    只含小写 ASCII 字母/数字/短横线；标题转不出 ASCII 用 task；
    总长超过 64 截断（再去掉尾巴上的短横线）。不引入拼音第三方库。
    """
    tail = re.sub(r"[^a-z0-9]", "", (task_id or "")[-4:].lower()) or "0000"
    ascii_part = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    if not ascii_part:
        ascii_part = "task"
    slug = f"{tail}-{ascii_part}"[:_SLUG_MAX].rstrip("-")
    return slug or f"{tail}-task"


def branch_for(task_id: str, title: str) -> str:
    """夜班分支名：ns/<slug>。"""
    return f"ns/{slug_for(task_id, title)}"


def worktree_path_for(project_path: str | Path, task_id: str, title: str) -> Path:
    """树的位置固定：<project>/.claude/worktrees/<slug>。"""
    return Path(project_path) / WORKTREES_DIR / slug_for(task_id, title)


# ---------- 工作树清单 ----------


def list_worktrees(project_path: str | Path) -> list[dict]:
    """`git worktree list --porcelain` 解析成
    [{"path": str, "head": str, "branch": str | None}, ...]；非 Git 项目返回 []。"""
    if not is_git_repo(project_path):
        return []
    out: list[dict] = []
    current: dict = {}
    for line in git_out(project_path, "worktree", "list", "--porcelain").splitlines():
        if not line.strip():
            if current:
                out.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "head": "", "branch": None}
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        # bare / detached 等行：branch 留 None，照样当"不是夜班树"处理
    if current:
        out.append(current)
    return out


def registered_worktree(project_path: str | Path, path: str | Path) -> dict | None:
    """path 若是该项目登记在册的工作树，返回它的条目；否则 None。"""
    want = Path(path).absolute()
    for entry in list_worktrees(project_path):
        if Path(entry["path"]).absolute() == want:
            return entry
    return None


# ---------- .git/info/exclude ----------


def ensure_exclude(project_path: str | Path) -> None:
    """把 `.claude/worktrees/` 与 `.claude/settings.local.json` 补进共享的
    info/exclude（保留原内容、只补缺失项；重复启动不重复追加）。

    不改项目的 .gitignore；用 `git rev-parse --git-common-dir` 定位共享目录。
    """
    common = git_out(project_path, "rev-parse", "--git-common-dir").strip()
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = Path(project_path) / common_dir
    exclude = common_dir / "info" / "exclude"
    needed = [f"{WORKTREES_DIR}/", ".claude/settings.local.json"]
    existing: set[str] = set()
    if exclude.is_file():
        existing = {
            line.strip() for line in
            exclude.read_text(encoding="utf-8", errors="replace").splitlines()
        }
    missing = [item for item in needed if item not in existing]
    if not missing:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if exclude.is_file() and exclude.stat().st_size:
        raw = exclude.read_text(encoding="utf-8", errors="replace")
        if not raw.endswith("\n"):
            prefix = "\n"  # 原内容没收尾，先补个换行再追加，别把两行拼一起
    with open(exclude, "a", encoding="utf-8") as f:
        f.write(prefix + "\n".join(missing) + "\n")


# ---------- 建树 / 复用（幂等） ----------


def ensure_worktree(task: dict, project_path: str | Path) -> dict:
    """确保这棵树存在，返回可直接写进 status 的三件元数据
    {"worktree_path", "branch", "base_ref"}。

    - 首班：`git -C <project> worktree add <path> -b ns/<slug> <base_ref>`，
      路径/分支按本任务 id 推 slug；
    - 后继班：沿用 status 里登记的三件元数据（一条链从头到尾只有一棵树、
      一个分支、一个基准提交），核对在册就复用，绝不另建第二棵；
    - launching 重试再进来：路径、分支、项目都对得上就原样复用；
    - 只撞一半（分支在树不在 / 路径被别的东西占着 / 元数据互相矛盾）→
      抛 WorktreeError 把原因说清，绝不删东西、绝不建第二棵。
    """
    project_path = str(project_path)
    if not is_git_repo(project_path):
        raise WorktreeError(f"项目不是 Git 仓库，建不了工作树：{project_path}")
    task_id = task["id"]

    status = store.read_status(task_id)
    recorded_path = status.get("worktree_path")
    recorded_branch = status.get("branch")
    if recorded_path and recorded_branch:
        # 后继班 / 重试：按登记的树复用（slug 属于整条链，不按本班 id 重推）
        path = Path(recorded_path)
        branch = str(recorded_branch)
        if not path.is_absolute():
            raise WorktreeError(f"登记的工作树路径不是绝对路径：{path}")
        if not path.exists():
            raise WorktreeError(
                f"登记的工作树不见了：{path}（链上的树必须共用，夜班不另建第二棵）"
            )
        if not path.is_dir():
            raise WorktreeError(f"登记的工作树路径被非目录的东西占着：{path}")
        entry = registered_worktree(project_path, path)
        if entry is None:
            raise WorktreeError(
                f"目录已存在但不是本项目登记的工作树，不敢动：{path}"
            )
        if entry.get("branch") != f"refs/heads/{branch}":
            raise WorktreeError(
                f"工作树分支对不上：登记 {branch}，"
                f"实际 {entry.get('branch') or '游离 HEAD'}（{path}）"
            )
        ensure_exclude(project_path)
        base_ref = status.get("base_ref")
        return {
            "worktree_path": str(path),
            "branch": branch,
            "base_ref": str(base_ref) if base_ref else entry.get("head") or head_sha(path),
        }

    slug = slug_for(task_id, task.get("title") or "")
    branch = f"ns/{slug}"
    path = worktree_path_for(project_path, task_id, task.get("title") or "")

    if path.exists():
        if not path.is_dir():
            raise WorktreeError(f"工作树路径被非目录的东西占着：{path}")
        entry = registered_worktree(project_path, path)
        if entry is None:
            raise WorktreeError(
                f"目录已存在但不是本项目登记的工作树，不敢动：{path}"
            )
        if entry.get("branch") != f"refs/heads/{branch}":
            raise WorktreeError(
                f"工作树分支对不上：期望 {branch}，实际 {entry.get('branch') or '游离 HEAD'}（{path}）"
            )
        ensure_exclude(project_path)
        base_ref = status_base_ref(task)
        return {
            "worktree_path": str(path),
            "branch": branch,
            "base_ref": base_ref or entry.get("head") or head_sha(path),
        }

    # 树不在：先看分支是不是只撞了一半（分支已在、树没建成）——判失败说清楚
    probe = _git(project_path, "rev-parse", "--verify", f"refs/heads/{branch}")
    if probe.returncode == 0:
        raise WorktreeError(
            f"分支 {branch} 已存在但工作树 {path} 不在（上次只建了一半？），"
            "请人工核对后处理，夜班不抢着删"
        )
    base_ref = head_sha(project_path)
    git_out(
        project_path, "worktree", "add", str(path), "-b", branch, base_ref,
    )
    ensure_exclude(project_path)
    return {"worktree_path": str(path), "branch": branch, "base_ref": base_ref}


def status_base_ref(task: dict) -> str | None:
    """这班 status.json 里已登记的 base_ref（复用树时保持原基准）。"""
    base = store.read_status(task["id"]).get("base_ref")
    return str(base) if base else None


# ---------- 启动对账（孤儿只提示，绝不自动删） ----------


def reconcile_project(project_name: str, project_path: str | Path) -> list[dict]:
    """对账单个项目：返回孤儿工作树列表（写盘由 reconcile_all 统一做）。"""
    orphans: list[dict] = []
    entries = list_worktrees(project_path)
    if not entries:
        return orphans
    referenced: set[str] = set()
    for item in store.list_tasks():
        wt = (item["status"] or {}).get("worktree_path")
        if wt:
            referenced.add(str(Path(wt).absolute()))
    for entry in entries:
        branch = entry.get("branch") or ""
        if not branch.startswith(BRANCH_PREFIX):
            continue  # 用户自己或 CC 原生建的树，不是夜班的，别误报
        if str(Path(entry["path"]).absolute()) in referenced:
            continue
        orphans.append({
            "project": project_name,
            "path": entry["path"],
            "branch": branch[len("refs/heads/"):],
            "reason": "夜班工作树没有任务引用它（任务被删了？），只提示不自动删",
        })
    return orphans


def reconcile_all(config: dict) -> dict:
    """服务启动对账：逐 project 找孤儿树 + 找"引用的树没了/分支不符"的任务。

    - 孤儿写数据目录 orphan_worktrees.json（原子替换；无孤儿也写 []，不赖旧告警）；
    - 未合并/未丢弃任务引用的树消失或分支不符 → needs_attention，
      同一原因不重复刷事件（重启不再轰炸）。
    """
    orphans: list[dict] = []
    for name, path in (config.get("projects") or {}).items():
        try:
            orphans.extend(reconcile_project(str(name), str(path)))
        except WorktreeError:
            continue  # 单个项目 Git 坏了不拖垮其他项目
    store.atomic_write_json(store.home() / "orphan_worktrees.json", orphans)
    _flag_missing_trees()
    return {"orphans": orphans}


def _flag_missing_trees() -> None:
    """任务还引用着树、树却没了或分支不符 → needs_attention（人话错误）。"""
    for item in store.list_tasks():
        task, status = item["task"], item["status"]
        if not store.worktree_enabled(task):
            continue
        state = status.get("state")
        if state in ("merged", "discarded"):
            continue
        wt = status.get("worktree_path")
        if not wt:
            continue  # 还没起跑过/建树失败的任务，这里不管
        branch = status.get("branch")
        message = None
        if not Path(wt).exists():
            message = f"任务引用的工作树不见了：{wt}（被人删了？处理完可在网页丢弃本任务）"
        elif not _project_of(task):
            continue  # 项目已经不在 config 里，无从核验，别瞎报
        else:
            entry = None
            try:
                entry = registered_worktree(_project_of(task), wt)
            except WorktreeError:
                entry = None
            if entry is None:
                message = f"目录还在但不是项目登记的工作树：{wt}"
            elif branch and entry.get("branch") != f"refs/heads/{branch}":
                message = (
                    f"工作树分支对不上：任务记的是 {branch}，"
                    f"实际 {entry.get('branch') or '游离 HEAD'}（{wt}）"
                )
        if message is None:
            continue
        if state == "needs_attention" and status.get("error") == message:
            continue  # 上次启动已经报过同一件事，不重复刷
        store.update_status(
            task["id"], state="needs_attention", error=message,
            last_event_at=store.utc_now_iso(),
        )
        store.append_event(task["id"], f"启动对账：{message}")


def _project_of(task: dict) -> str:
    """task.json 里存的项目路径（建树时记过 project_path 的老任务也兼容）。"""
    try:
        config = store.load_config()
        return str(config["projects"][task["project"]])
    except Exception:
        return ""
