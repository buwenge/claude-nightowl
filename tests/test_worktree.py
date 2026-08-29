"""worktree.py 的测试：slug、建树/复用幂等、exclude、对账（临时 Git 仓库，离线）。

只测 S5① 底座：安全 Git runner、识别新旧任务、ensure/reuse、reconcile。
存档点/合并/丢弃在 S5② 补（同文件继续往下加）。
"""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift import store, worktree

CONFIG = {
    "projects": {"demo": "/home/user/projects/demo"},
    "models": {"claude-fable-5": {"context_limit": 500000}},
    "default_context_limit": 200000,
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
    "chain": {"max_windows": 3, "on_no_handover": "continue"},
    "chain_template": "第 {shift} 班。交接：{handover}\n{task}",
}


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    return tmp_path


def init_git_repo(path: Path) -> str:
    """最小 Git 仓库，返回初始 commit 的完整 sha。"""
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
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_task(config: dict, **over) -> dict:
    """落盘一个 worktree=true 的新任务，返回完整 task.json 内容。"""
    task = {
        "title": "修测试任务",
        "project": "demo",
        "model": "claude-fable-5",
        "effort": "high",
        "run_at": "2026-08-27T18:00:00Z",
        "task_text": "正文",
        "prompt_final": "提示词",
    }
    task.update(over)
    task_id = store.create_task(task, config)
    return store.load_task(task_id)


@pytest.fixture
def repo(tmp_path):
    """Git 项目 + 指向它的 config；返回 (项目路径, config)。"""
    proj = tmp_path / "proj"
    init_git_repo(proj)
    config = dict(CONFIG)
    config["projects"] = {"demo": str(proj)}
    store.atomic_write_json(store.home() / "config.json", config)
    return proj, config


# ---------- slug ----------


def test_slug_for_ascii_and_fallback():
    slug = worktree.slug_for("20260829-101112-abcd", "Fix Login Flow!")
    assert slug == "abcd-fix-login-flow"
    assert worktree.slug_for("20260829-101112-abcd", "中文标题没有字母") \
        == "abcd-task"
    assert worktree.slug_for("20260829-101112-abcd", "全中文没有字母abc") \
        == "abcd-abc"
    # 总长 ≤64、只含小写字母/数字/短横线、不以短横线收尾
    long_title = "A" * 200
    slug = worktree.slug_for("20260829-101112-beef", long_title)
    assert len(slug) <= 64 and slug == slug.lower()
    assert slug == slug.rstrip("-").lstrip("-")
    assert not [c for c in slug if c not in "abcdefghijklmnopqrstuvwxyz0123456789-"]


def test_branch_and_path_derive_from_slug(repo):
    proj, config = repo
    task = make_task(config, title="Mixed Case 标题")
    slug = worktree.slug_for(task["id"], task["title"])
    assert worktree.branch_for(task["id"], task["title"]) == f"ns/{slug}"
    assert worktree.worktree_path_for(proj, task["id"], task["title"]) == \
        proj / ".claude" / "worktrees" / slug


# ---------- 建树 / 复用（幂等） ----------


def test_ensure_creates_tree_with_branch_base_and_exclude(repo):
    proj, config = repo
    base = subprocess.run(["git", "-C", str(proj), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    task = make_task(config)
    meta = worktree.ensure_worktree(task, proj)
    slug = worktree.slug_for(task["id"], task["title"])

    assert Path(meta["worktree_path"]) == proj / ".claude" / "worktrees" / slug
    assert meta["branch"] == f"ns/{slug}"
    assert meta["base_ref"] == base
    assert Path(meta["worktree_path"]).is_dir()
    # 分支确实指向 base
    out = subprocess.run(["git", "-C", str(proj), "rev-parse", meta["branch"]],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == base
    # 共享 info/exclude 补了两项
    exclude = proj / ".git" / "info" / "exclude"
    text = exclude.read_text(encoding="utf-8")
    assert ".claude/worktrees/" in text.splitlines()
    assert ".claude/settings.local.json" in text.splitlines()


def test_ensure_is_idempotent_and_exclude_not_duplicated(repo):
    proj, config = repo
    task = make_task(config)
    first = worktree.ensure_worktree(task, proj)
    second = worktree.ensure_worktree(task, proj)
    assert first == second
    # 树只有一棵
    entries = [e for e in worktree.list_worktrees(proj)
               if (e.get("branch") or "").startswith("refs/heads/ns/")]
    assert len(entries) == 1
    # exclude 只补缺失项，重复调用不重复追加
    worktree.ensure_exclude(proj)
    worktree.ensure_exclude(proj)
    lines = (proj / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert lines.count(".claude/worktrees/") == 1
    assert lines.count(".claude/settings.local.json") == 1
    # 项目自己的 .gitignore 不许被碰
    assert not (proj / ".gitignore").exists()


def test_ensure_reuse_keeps_original_base(repo):
    """launching 重试再进来：树已存在且元数据匹配 → 复用，base_ref 保持登记值。"""
    proj, config = repo
    task = make_task(config)
    first = worktree.ensure_worktree(task, proj)
    # 项目主线继续走，HEAD 变了也不影响复用
    (proj / "README.md").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(proj), "commit", "-aqm", "advance"],
                   check=True, capture_output=True)
    second = worktree.ensure_worktree(task, proj)
    assert second == first


def test_ensure_on_non_git_project_fails_readable(tmp_path):
    proj = tmp_path / "plain"
    proj.mkdir()
    store.atomic_write_json(store.home() / "config.json",
                            {**CONFIG, "projects": {"demo": str(proj)}})
    task = make_task({**CONFIG, "projects": {"demo": str(proj)}})
    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.ensure_worktree(task, proj)
    assert "Git 仓库" in str(exc.value)
    assert not (proj / ".claude").exists()  # 什么都没乱建


def test_ensure_branch_conflict_fails_without_touching(repo):
    """分支已存在但树不在：判失败说清楚，绝不删分支、绝不建第二棵。"""
    proj, config = repo
    task = make_task(config)
    slug = worktree.slug_for(task["id"], task["title"])
    subprocess.run(["git", "-C", str(proj), "branch", f"ns/{slug}"],
                   check=True, capture_output=True)
    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.ensure_worktree(task, proj)
    assert f"ns/{slug}" in str(exc.value)
    # 分支还在，树没建
    out = subprocess.run(["git", "-C", str(proj), "branch", "--list", f"ns/{slug}"],
                         capture_output=True, text=True, check=True).stdout
    assert f"ns/{slug}" in out
    assert not (proj / ".claude" / "worktrees" / slug).exists()


def test_ensure_path_occupied_by_foreign_dir_fails(repo):
    """路径被非工作树的目录占着：报错不动手。"""
    proj, config = repo
    task = make_task(config)
    slug = worktree.slug_for(task["id"], task["title"])
    occupied = proj / ".claude" / "worktrees" / slug
    occupied.mkdir(parents=True)
    (occupied / "user-file.txt").write_text("别删我的东西\n", encoding="utf-8")
    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.ensure_worktree(task, proj)
    assert "不敢动" in str(exc.value)
    assert (occupied / "user-file.txt").exists()


def test_ensure_registered_tree_with_wrong_branch_fails(repo):
    """树在册但分支被换掉：元数据互相矛盾，判失败。"""
    proj, config = repo
    task = make_task(config)
    meta = worktree.ensure_worktree(task, proj)
    slug = worktree.slug_for(task["id"], task["title"])
    other = f"ns/{slug}-x"
    subprocess.run(["git", "-C", str(meta["worktree_path"]), "checkout", "-q", "-b", other],
                   check=True, capture_output=True)
    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.ensure_worktree(task, proj)
    assert "分支对不上" in str(exc.value)


# ---------- 识别新旧任务 ----------


def test_wants_worktree_new_default_true_false_and_old_missing(repo):
    _, config = repo
    # 新任务缺省 true
    task = make_task(config)
    assert task["worktree"] is True
    assert worktree.wants_worktree(task) is True
    # 显式 false
    task_false = make_task(config, worktree=False)
    assert worktree.wants_worktree(task_false) is False
    # 手造旧 task.json（缺字段）按 false，且 load_task 不偷偷回写
    old = {
        "id": "20260101-000000-ffff", "title": "旧任务", "project": "demo",
        "model": "claude-fable-5", "effort": "high", "shift": 1,
        "run_at": "2026-01-01T00:00:00Z", "task_text": "正文",
        "prompt_final": "提示词", "created_at": "2026-01-01T00:00:00Z",
    }
    d = store.task_dir("20260101-000000-ffff")
    d.mkdir(parents=True, exist_ok=True)
    store.atomic_write_json(d / "task.json", old)
    loaded = store.load_task("20260101-000000-ffff")
    assert "worktree" not in loaded
    assert worktree.wants_worktree(loaded) is False
    assert store.worktree_enabled(loaded) is False


def test_review_shape_and_validation(repo):
    _, config = repo
    # 缺省 review 占住形状
    task = make_task(config)
    assert task["review"] == {"enabled": False, "merge_policy": "manual"}
    # 只认 manual / auto
    task_auto = make_task(config, review={"enabled": False, "merge_policy": "auto"})
    assert task_auto["review"]["merge_policy"] == "auto"
    # enabled=true 明确拒绝（S7 才开放）
    with pytest.raises(ValueError) as exc:
        make_task(config, review={"enabled": True, "merge_policy": "manual"})
    assert "S7" in str(exc.value)
    with pytest.raises(ValueError):
        make_task(config, review={"enabled": False, "merge_policy": "yolo"})
    with pytest.raises(ValueError):
        make_task(config, review={"enabled": "yes"})
    with pytest.raises(ValueError):
        make_task(config, review="审一下")
    with pytest.raises(ValueError):
        make_task(config, review={"enabled": False, "criteria_text": "多出来的键"})
    # worktree 非布尔拒绝
    with pytest.raises(ValueError):
        make_task(config, worktree="true")


# ---------- 后继班共享一棵树 ----------


def test_create_successor_copies_worktree_review_and_meta(repo):
    proj, config = repo
    parent = make_task(config)
    meta = worktree.ensure_worktree(parent, proj)
    store.update_status(parent["id"], **meta)
    succ_id = store.create_successor(parent, "交接\nNEXT: continue", config)
    succ = store.load_task(succ_id)
    assert succ["worktree"] is True
    assert succ["review"] == parent["review"]
    status = store.read_status(succ_id)
    for key in ("worktree_path", "branch", "base_ref"):
        assert status[key] == meta[key]
    # ensure 再进来：同一棵树，不建第二棵
    again = worktree.ensure_worktree(succ, proj)
    assert again == meta
    entries = [e for e in worktree.list_worktrees(proj)
               if (e.get("branch") or "").startswith("refs/heads/ns/")]
    assert len(entries) == 1


def test_create_successor_of_old_style_task_stays_false(repo):
    """旧式父任务（缺 worktree 字段）的后继必须保持 false，不吃新任务缺省。"""
    _, config = repo
    parent = make_task(config)
    data = store.load_task(parent["id"])
    del data["worktree"]  # 手工退回旧记录形状
    store.atomic_write_json(store.task_dir(parent["id"]) / "task.json", data)
    succ_id = store.create_successor(store.load_task(parent["id"]), "交接", config)
    succ = store.load_task(succ_id)
    assert succ["worktree"] is False
    assert worktree.wants_worktree(succ) is False


# ---------- 启动对账 ----------


def test_reconcile_reports_only_ns_orphans_and_never_deletes(repo):
    proj, config = repo
    # 夜班的树：建出来，但删掉任务引用（孤儿）
    task = make_task(config)
    meta = worktree.ensure_worktree(task, proj)
    # 用户自己的普通 worktree：不该被报
    subprocess.run(
        ["git", "-C", str(proj), "worktree", "add", str(proj / "user-wt"), "-b", "feature/x"],
        check=True, capture_output=True,
    )
    # 主签出目录本身也不算
    orphans = worktree.reconcile_all(config)["orphans"]
    paths = [o["path"] for o in orphans]
    assert str(Path(meta["worktree_path"]).resolve()) in [str(Path(p).resolve()) for p in paths]
    assert all("user-wt" not in p for p in paths)
    assert len(paths) == 1
    # 形状稳定：project / path / branch / reason
    assert set(orphans[0]) == {"project", "path", "branch", "reason"}
    assert orphans[0]["project"] == "demo"
    assert orphans[0]["branch"].startswith("ns/")
    # 落盘且绝不自动删
    saved = json.loads((store.home() / "orphan_worktrees.json").read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert Path(meta["worktree_path"]).exists()
    out = subprocess.run(["git", "-C", str(proj), "branch", "--list", meta["branch"]],
                         capture_output=True, text=True, check=True).stdout
    assert meta["branch"] in out


def test_reconcile_no_orphans_writes_empty_list(repo):
    proj, config = repo
    task = make_task(config)
    meta = worktree.ensure_worktree(task, proj)
    store.update_status(task["id"], **meta)  # 有任务引用 → 不是孤儿
    orphans = worktree.reconcile_all(config)["orphans"]
    assert orphans == []
    saved = json.loads((store.home() / "orphan_worktrees.json").read_text(encoding="utf-8"))
    assert saved == []  # 无孤儿也原子写 []，旧告警不赖着


def test_reconcile_flags_task_whose_tree_vanished_only_once(repo):
    proj, config = repo
    task = make_task(config)
    meta = worktree.ensure_worktree(task, proj)
    store.update_status(task["id"], **meta)
    # 树被人删了
    subprocess.run(["git", "-C", str(proj), "worktree", "remove", "--force", meta["worktree_path"]],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(proj), "worktree", "prune"],
                   check=True, capture_output=True)
    first = worktree.reconcile_all(config)
    assert first["orphans"] == []  # 树没了自然不进孤儿表
    status = store.read_status(task["id"])
    assert status["state"] == "needs_attention"
    assert "不见了" in status["error"]
    events = (store.task_dir(task["id"]) / "events.log").read_text(encoding="utf-8")
    assert events.count("启动对账") == 1
    # 再跑一次启动对账：同一件事不重复刷
    worktree.reconcile_all(config)
    events = (store.task_dir(task["id"]) / "events.log").read_text(encoding="utf-8")
    assert events.count("启动对账") == 1


def test_reconcile_flags_branch_mismatch(repo):
    proj, config = repo
    task = make_task(config)
    meta = worktree.ensure_worktree(task, proj)
    store.update_status(task["id"], **meta)
    # 有人在树里把分支换掉了
    subprocess.run(["git", "-C", str(meta["worktree_path"]), "checkout", "-q", "-b", "ns/other"],
                   check=True, capture_output=True)
    worktree.reconcile_all(config)
    status = store.read_status(task["id"])
    assert status["state"] == "needs_attention"
    assert "分支对不上" in status["error"]
