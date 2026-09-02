"""工作树：建树/复用、存档点、合并/丢弃、启动对账（Git 子进程全部集中在这）。

规矩（开工令 S5）：
- 所有 Git 调用一律参数数组 + capture_output + text + 有限超时，绝不拼 shell；
- 错误只回传 stdout/stderr 尾部，不把大输出塞进状态或网页；
- 树的位置固定 <project>/.claude/worktrees/<slug>，分支固定 ns/<slug>；
- 建树幂等：路径/分支/项目都对得上就复用；只撞一半或元数据矛盾判失败；
- 对账只认分支以 refs/heads/ns/ 开头的树，绝不自动删除任何东西；
- 合并/丢弃只动经过路径/分支双核验的树，绝不 reset --hard / clean / rm -rf。
"""

from __future__ import annotations

import fcntl
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

from . import store

__all__ = [
    "GitError",
    "WorktreeError",
    "chain_tasks",
    "chain_window_ids",
    "check_task_tree",
    "checkpoint",
    "discard_task",
    "ensure_exclude",
    "ensure_worktree",
    "is_git_repo",
    "list_worktrees",
    "merge_task",
    "reconcile_all",
    "reconcile_project",
    "slug_for",
    "wants_worktree",
    "worktree_clean",
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
    except OSError as exc:
        raise WorktreeError(f"git {args[0]} 起不来：{exc}") from None


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
    want = Path(path).resolve(strict=False)
    for entry in list_worktrees(project_path):
        if Path(entry["path"]).resolve(strict=False) == want:
            return entry
    return None


def _tree_location_error(project_path: str | Path, path: str | Path) -> str | None:
    """核验工作树物理位置没有借 `..` 或软链接逃出项目。

    S5 的删除边界不只比较字符串：若 `.claude/worktrees` 本身或任务路径中的
    某一级是指向项目外的软链接，也必须拒绝。
    """
    project = Path(project_path).resolve(strict=False)
    root = (project / WORKTREES_DIR).resolve(strict=False)
    candidate = Path(path).resolve(strict=False)
    try:
        root.relative_to(project)
    except ValueError:
        return f"项目的 {WORKTREES_DIR.as_posix()}/ 通过软链接指到项目外，拒绝动它"
    if candidate.parent != root:
        return f"工作树路径不在项目的 {WORKTREES_DIR.as_posix()}/ 下，拒绝动它：{path}"
    return None


def _recorded_identity_error(task: dict, project_path: str | Path, status: dict) -> str | None:
    """核验任务记录自己的路径/分支，以及同一条链各班记录的一致性。

    分支名以建树时落盘的 status 为准，不能按可编辑的任务标题重新计算；否则
    运行中改一次标题，正常的旧分支就永远无法合并或丢弃。
    """
    wt = status.get("worktree_path")
    branch = status.get("branch")
    if not wt or not branch:
        return "这个任务没有登记工作树（还没建过树，无从合并/丢弃）"
    location_error = _tree_location_error(project_path, wt)
    if location_error:
        return location_error
    if not re.fullmatch(r"ns/[a-z0-9][a-z0-9-]{0,63}", str(branch)):
        return f"分支不是夜班登记的安全 ns 分支：{branch}"
    root_id = task.get("root_id") or task["id"]
    id_tail = re.sub(r"[^a-z0-9]", "", str(root_id)[-4:].lower()) or "0000"
    if not str(branch).startswith(f"ns/{id_tail}-"):
        return f"分支与这条链的任务 id 不符：{branch}"
    if Path(wt).resolve(strict=False).name != str(branch)[len("ns/"):]:
        return f"工作树目录名与分支不符：{wt} / {branch}"
    for member in chain_tasks(task):
        member_status = store.read_status(member["id"])
        member_path = member_status.get("worktree_path")
        member_branch = member_status.get("branch")
        if member_path and Path(member_path).resolve(strict=False) != Path(wt).resolve(strict=False):
            return f"同一条链的工作树记录不一致：{member['id']} 指向 {member_path}"
        if member_branch and member_branch != branch:
            return f"同一条链的分支记录不一致：{member['id']} 记的是 {member_branch}"
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
    project_path = str(Path(project_path).resolve(strict=False))
    if not is_git_repo(project_path):
        raise WorktreeError(f"项目不是 Git 仓库，建不了工作树：{project_path}")
    task_id = task["id"]

    status = store.read_status(task_id)
    recorded_path = status.get("worktree_path")
    recorded_branch = status.get("branch")
    if bool(recorded_path) != bool(recorded_branch):
        raise WorktreeError("登记的工作树元数据不完整（path / branch 只剩一个），不敢另建第二棵")
    if recorded_path and recorded_branch:
        # 后继班 / 重试：按登记的树复用（slug 属于整条链，不按本班 id 重推）
        path = Path(recorded_path)
        branch = str(recorded_branch)
        if not path.is_absolute():
            raise WorktreeError(f"登记的工作树路径不是绝对路径：{path}")
        identity_error = _recorded_identity_error(task, project_path, status)
        if identity_error:
            raise WorktreeError(identity_error)
        if not status.get("base_ref"):
            raise WorktreeError("登记的工作树元数据缺少 base_ref，不敢拿当前分支头冒充原基准")
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
    location_error = _tree_location_error(project_path, path)
    if location_error:
        raise WorktreeError(location_error)

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
    ensure_exclude(project_path)
    git_out(
        project_path, "worktree", "add", str(path), "-b", branch, base_ref,
    )
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


# ---------- 收工存档点（S5②） ----------


def worktree_clean(worktree_path: str | Path, *, include_untracked: bool = True) -> bool:
    """工作树是否干净：`git status --porcelain` 为空才算——tracked 修改、
    删除和 untracked 新文件全看得见；只用 git diff --quiet 会漏 untracked，禁止。

    总review二 G6：`include_untracked=False` 只看已跟踪文件的改动
    （`--untracked-files=no`）——只给"检查主线目录"那几处调用方用：主线上
    一个临时笔记/`.orig` 这类 untracked 杂物不该拦自动合并，已跟踪文件被
    改过仍然拦。检查**工作树自身**"存档点后又有没有改动"不能用这个口子：
    untracked 新文件就是改动本身。
    """
    args = ["status", "--porcelain"]
    if not include_untracked:
        args.append("--untracked-files=no")
    return not git_out(worktree_path, *args).strip()


def checkpoint(task: dict, worktree_path: str | Path) -> str | None:
    """打存档点：工作树有改动就 `git add -A` + commit，返回完整 sha。

    - 无改动（porcelain 为空 / staged 为空）→ 不建空 commit，返回 None；
    - add / commit 失败抛 GitError（人话尾部），由调用方落 needs_attention；
    - commit message：`ns: <标题> 第<round>轮 <role>#<shift>`，S5 没有
      角色/轮次字段，缺省 round=1、role=build，班次取真实 shift。
    """
    status_out = git_out(worktree_path, "status", "--porcelain")
    if not status_out.strip():
        return None
    git_out(worktree_path, "add", "-A")
    staged = git_out(worktree_path, "diff", "--cached", "--name-only")
    if not staged.strip():
        return None
    shift = int(task.get("shift") or 1)
    title = task.get("title") or task["id"]
    message = f"ns: {title} 第1轮 build#{shift}"
    git_out(worktree_path, "commit", "-m", message)
    return git_out(worktree_path, "rev-parse", "HEAD").strip()


# ---------- 合并 / 丢弃（S5②：auto 收工与人工按钮走同一个入口） ----------


def chain_tasks(task: dict) -> list[dict]:
    """同一条链（root_id 相同）的所有班，按 shift 升序。"""
    root = task.get("root_id") or task["id"]
    members = [
        item["task"] for item in store.list_tasks()
        if (item["task"].get("root_id") or item["task"]["id"]) == root
    ]
    members.sort(key=lambda t: int(t.get("shift") or 1))
    return members


def chain_window_ids(task: dict) -> list[str]:
    """这条链自己开过的全部 tmux 窗口 id（只按任务记录，绝不猜）。"""
    ids: list[str] = []
    for item in store.list_tasks():
        other = item["task"]
        if (other.get("root_id") or other["id"]) != (task.get("root_id") or task["id"]):
            continue
        wid = (item["status"] or {}).get("window_id")
        if wid:
            ids.append(str(wid))
    return ids


def check_task_tree(
    task: dict, project_path: str | Path, status: dict,
) -> str | None:
    """合并/丢弃前的双核验。返回 None = 合法；否则给人话原因。

    - worktree_path 必须严格位于 <project>/.claude/worktrees/ 下；
    - branch 必须是与链根任务 id、目录名和各班落盘记录一致的安全 ns/<slug>；
      标题可编辑，不拿当前标题重算旧分支；
    - 树必须在项目册上且登记分支与记录一致。
    """
    reason = _recorded_identity_error(task, project_path, status)
    if reason:
        return reason
    wt = status["worktree_path"]
    branch = status["branch"]
    try:
        entry = registered_worktree(project_path, wt)
    except WorktreeError as exc:
        # 项目仓库本身 Git 出错：说人话返回，不让异常逃到调度器 tick 里
        return f"核对工作树登记时 Git 出错：{exc}"
    if entry is None:
        return f"这个路径不是项目登记的工作树：{wt}"
    if entry.get("branch") != f"refs/heads/{branch}":
        return (
            f"工作树的分支被换过：登记 {branch}，"
            f"实际 {entry.get('branch') or '游离 HEAD'}"
        )
    return None


def _is_ancestor(project_path: str | Path, branch: str) -> bool:
    """分支是否已经是主线 HEAD 的祖先（= 已经合并过）。

    总review二 G13：显式 `refs/heads/<branch>`，不裸传短名——裸短名走 git
    自己的 ref 解析顺序，同名 tag/远端分支存在时会被那个顶替，"已经合并"
    这个判断的对象就不是我们自己建的这条本地分支了。
    """
    proc = _git(project_path, "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD")
    return proc.returncode == 0


def _ref_is_ancestor(project_path: str | Path, ref: str) -> bool:
    """一个已记录 sha/ref 是否已在主线历史中。命令失败按 False，不猜。"""
    return _git(project_path, "merge-base", "--is-ancestor", ref, "HEAD").returncode == 0


def _branch_exists(project_path: str | Path, branch: str) -> bool:
    return _git(
        project_path, "rev-parse", "--verify", f"refs/heads/{branch}"
    ).returncode == 0


def _merge_in_progress(project_path: str | Path) -> bool:
    return _git(
        project_path, "rev-parse", "-q", "--verify", "MERGE_HEAD"
    ).returncode == 0


def _tail(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stderr or "") + (proc.stdout or "")).strip()[-_ERROR_TAIL:]


def _clear_tree_meta(task_id: str) -> None:
    """链成员的 status 清掉工作树三件元数据（树已不在，别让删除保护卡着）。"""
    def mut(status: dict) -> None:
        for key in ("worktree_path", "branch", "base_ref"):
            status.pop(key, None)

    store.modify_status(task_id, mut)


@contextmanager
def _operation_lock(task: dict):
    """串行化同一任务的 merge/discard。

    HTTP 是 ThreadingHTTPServer，手机双击会真的并发进两条 Git 流程；第二条
    绝不能在第一条 merge 中途误判冲突并 abort。文件锁也覆盖同机其他进程。
    """
    root_id = task.get("root_id") or task["id"]
    path = store.task_dir(root_id) / ".worktree-op.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def merge_task(
    task: dict, project_path: str | Path, status: dict, config: dict,
    close_windows=None,
) -> tuple[bool, str]:
    """串行入口；锁内重读状态，过期的第二个请求不会重复执行 Git。"""
    with _operation_lock(task):
        fresh_status = store.read_status(task["id"])
        if fresh_status.get("state") == "merged":
            return True, "已经合并进主线，工作树与分支已清理"
        try:
            return _merge_task_locked(
                task, project_path, fresh_status, config, close_windows=close_windows,
            )
        except WorktreeError as exc:
            # S8 审查 B：任何一步 Git 出错（典型：工作树被人 rm -rf 后 `git -C <树>`
            # 进不去目录）都收成 needs_attention + 人话，绝不让异常逃出去——
            # 调度器 tick 的 _finalize_done 没接这个异常，逃出去会中断整轮
            # tick，排在后面的任务全部饿死。
            return _merge_fail(
                task["id"], f"合并过程中 Git 出错，已停手（树与分支保留）：{exc}",
                fresh_status.get("merge_sha"),
            )


def _merge_task_locked(
    task: dict, project_path: str | Path, status: dict, config: dict,
    close_windows=None,
) -> tuple[bool, str]:
    """把这条链的工作树合并进主线并清理（auto 收工与人工"合并进主线"共用）。

    返回 (ok, 人话说明)。任何失败都把本班落 needs_attention（保留已拿到的
    merge_sha），树与分支保留，绝不 reset --hard、绝不丢用户改动：
    - 主线有未提交改动 → 不碰 merge，说清"处理完按'合并进主线'"；
    - 存档点后工作树又脏 → 不合并；
    - 冲突 → merge --abort 且确认 MERGE_HEAD 清掉，树与分支保留；
    - merge 成功但清理失败 → 保留 merge_sha，下次进来只补清理。
    """
    task_id = task["id"]
    if not store.worktree_enabled(task):
        reason = "老式任务没有工作树，不走合并"
        return False, reason

    wt = status.get("worktree_path")
    branch = status.get("branch")
    merge_sha = status.get("merge_sha")

    # 上次 merge 已成功（分支已是主线祖先）或树和分支都已不在：只补收尾
    already_merged = bool(
        wt and branch and _branch_exists(project_path, branch)
        and _is_ancestor(project_path, branch)
    )
    fully_gone = bool(
        wt and branch and merge_sha
        and not Path(wt).exists() and not _branch_exists(project_path, branch)
        and _ref_is_ancestor(project_path, str(merge_sha))
    )

    if not (already_merged or fully_gone):
        reason = check_task_tree(task, project_path, status)
        if reason:
            return _merge_fail(task_id, reason, merge_sha)
        if not Path(wt).is_dir():
            # 登记还在（git worktree list 仍列着、可 prune）但目录没了：存档点后
            # 有没有改动无从核对，不替人拍板，说清楚等人来
            reason = (
                f"工作树目录不见了（被人删了？）：{wt}；没敢合并，分支 {branch} 保留着，"
                "请人工核对后在网页合并或丢弃"
            )
            return _merge_fail(task_id, reason, merge_sha)
        if not worktree_clean(wt):
            reason = "存档点后工作树又有改动，没敢合并；先处理掉再点'合并进主线'"
            return _merge_fail(task_id, reason, merge_sha)
        if not worktree_clean(project_path, include_untracked=False):
            reason = "主线有你没提交的改动，没敢自动合并；处理完按'合并进主线'"
            return _merge_fail(task_id, reason, merge_sha)
        proc = _git(project_path, "merge", "--no-ff", "--no-edit", branch)
        if proc.returncode != 0:
            if _merge_in_progress(project_path):
                abort_proc = _git(project_path, "merge", "--abort")
                if abort_proc.returncode != 0 or _merge_in_progress(project_path):
                    reason = "合并冲突，且 merge --abort 后主线仍有合并进行态，需要人工处理"
                elif not worktree_clean(project_path, include_untracked=False):
                    reason = "合并冲突虽已 abort，但主线仍留下未提交改动，需要人工处理"
                else:
                    reason = f"合并冲突，已放弃合并（merge --abort），树与分支保留：{_tail(proc)}"
            else:
                if worktree_clean(project_path, include_untracked=False):
                    reason = f"合并失败（主线未动）：{_tail(proc)}"
                else:
                    reason = f"合并失败且主线留下未提交改动，需要人工处理：{_tail(proc)}"
            return _merge_fail(task_id, reason, merge_sha)
        merge_sha = git_out(project_path, "rev-parse", "HEAD").strip()
    elif already_merged and not merge_sha:
        # 手工合过没记账：记分支 tip，不再造第二个 merge commit
        merge_sha = git_out(project_path, "rev-parse", branch).strip()

    # 先把合并证据落盘再清树/删分支。这样即使进程恰在清理阶段崩溃，
    # 下次也只会在验证该 sha 已是主线祖先后补清理，不会凭“树不见了”猜已合并。
    if merge_sha:
        store.update_status(task_id, merge_sha=merge_sha)

    # merge 成功：先关这条链自己开的窗口，再清工作树、删分支
    if close_windows is not None:
        close_windows(chain_window_ids(task))
    cleanup_reason = _cleanup_tree(project_path, wt, branch)
    if cleanup_reason:
        return _merge_fail(
            task_id, f"合并成功但清理失败（merge_sha 已保留）：{cleanup_reason}",
            merge_sha,
        )

    for member in chain_tasks(task):
        _clear_tree_meta(member["id"])
    store.update_status(
        task_id, state="merged", merge_sha=merge_sha, error=None,
        last_event_at=store.utc_now_iso(),
    )
    store.append_event(
        task_id, f"已合并进主线（{(merge_sha or '')[:12]}），工作树与分支已清理"
    )
    return True, f"已合并进主线（{(merge_sha or '')[:12]}），工作树与分支已清理"


def _cleanup_tree(project_path: str | Path, wt: str, branch: str) -> str | None:
    """清工作树与分支；树/分支已不在就跳过对应步骤（半成功可恢复）。返回失败原因或 None。"""
    if wt and Path(wt).exists():
        proc = _git(project_path, "worktree", "remove", wt)
        if proc.returncode != 0:
            return f"worktree remove：{_tail(proc)}"
    elif wt:
        # 目录已经不在但 .git/worktrees 里可能还登记着（被人 rm -rf 的树）：不 prune
        # 的话 branch -d 会报 "used by worktree" 删不掉。prune 只清"目录已不存在"
        # 的登记项，不碰任何文件；失败也不拦（下一步 branch -d 会给出真实原因）。
        _git(project_path, "worktree", "prune")
    if branch and _branch_exists(project_path, branch):
        proc = _git(project_path, "branch", "-d", branch)
        if proc.returncode != 0:
            return f"branch -d：{_tail(proc)}"
    return None


def _merge_fail(task_id: str, reason: str, merge_sha: str | None) -> tuple[bool, str]:
    fields: dict = {
        "state": "needs_attention", "error": reason,
        "last_event_at": store.utc_now_iso(),
    }
    if merge_sha:
        fields["merge_sha"] = merge_sha
    store.update_status(task_id, **fields)
    store.append_event(task_id, f"合并没成：{reason}")
    return False, reason


def discard_task(
    task: dict, project_path: str | Path, status: dict, config: dict,
    close_windows=None,
) -> tuple[bool, str]:
    """串行入口；锁内重读状态，双击/重试不会并发拆同一棵树。"""
    with _operation_lock(task):
        fresh_status = store.read_status(task["id"])
        if fresh_status.get("state") == "discarded":
            return True, "已经丢弃：工作树与 ns 分支已删除"
        try:
            return _discard_task_locked(
                task, project_path, fresh_status, config, close_windows=close_windows,
            )
        except WorktreeError as exc:
            # 同 merge_task：Git 出错只在核验阶段发生（删除之前），收成人话返回
            return False, f"丢弃失败（什么都没删）：{exc}"


def _discard_task_locked(
    task: dict, project_path: str | Path, status: dict, config: dict,
    close_windows=None,
) -> tuple[bool, str]:
    """丢弃：先关这条链自己的窗口，再 `git worktree remove --force` +
    `git branch -D`，终态 discarded。只动经过路径/分支双核验的树。"""
    task_id = task["id"]
    reason = _recorded_identity_error(task, project_path, status)
    if reason:
        return False, reason
    wt = status["worktree_path"]
    branch = status["branch"]

    tree_exists = Path(wt).exists()
    if tree_exists:
        reason = check_task_tree(task, project_path, status)
        if reason:
            return False, reason

    if close_windows is not None:
        close_windows(chain_window_ids(task))
    if tree_exists:
        proc = _git(project_path, "worktree", "remove", "--force", wt)
        if proc.returncode != 0:
            reason = f"丢弃失败（什么都没删成，树保留）：worktree remove：{_tail(proc)}"
            return False, reason
    else:
        # 目录已被人删掉：先 prune 掉残留登记，否则 branch -D 报 "used by worktree"
        _git(project_path, "worktree", "prune")
    if _branch_exists(project_path, branch):
        proc = _git(project_path, "branch", "-D", branch)
    else:
        proc = subprocess.CompletedProcess([], 0, "", "")
    if proc.returncode != 0:
        reason = (
            f"丢弃做了一半：工作树已删，但分支 {branch} 没删掉，需要人工 git branch -D："
            f"{_tail(proc)}"
        )
        store.update_status(
            task_id, state="needs_attention", error=reason,
            last_event_at=store.utc_now_iso(),
        )
        store.append_event(task_id, f"丢弃：{reason}")
        return False, reason

    for member in chain_tasks(task):
        _clear_tree_meta(member["id"])
    store.update_status(
        task_id, state="discarded", error=None, last_event_at=store.utc_now_iso(),
    )
    store.append_event(task_id, "已丢弃：工作树与 ns 分支已删除（未合并内容不可恢复）")
    return True, "已丢弃：工作树与 ns 分支已删除"
