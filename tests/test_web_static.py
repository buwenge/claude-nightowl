"""web 静态断言（S5③）：前端工作树收口的关键元素与接口字符串都在。

不发请求、不开浏览器、不起服务器——只读 web/ 三个文件做字符串级检查，
防"改了后端忘了前端"或反过来。
"""

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def test_index_has_worktree_switch_merge_choice_and_orphan_box():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for piece in (
        'id="f-worktree"',          # 隔离工作树开关（默认开，checked）
        'id="f-mergepolicy"',       # 完工后：等我合并 / 自动合并
        'id="f-worktree-off-hint"',
        'id="merge-row"',
        'id="orphan-box"',          # 孤儿工作树提示容器
        "老式模式：直接在项目目录施工，不打存档点",
        "自动合并",
        "等我合并",
    ):
        assert piece in html, piece


def test_app_js_has_merge_discard_orphan_and_state_texts():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        '"/merge"', '"/discard"', '"./api/worktrees"',
        'awaiting_merge: "等你合并"', 'merged: "已合并"', 'discarded: "已丢弃"',
        '"合并进主线"', '"丢弃"', '"先留着"',
        # 丢弃第二次确认的破坏性警告原文
        "会永久删除这棵工作树和 ns 分支，未合并内容无法从页面恢复",
        # 先留着是纯前端 no-op
        "工作树和分支已保留，之后还能回来处理",
        # 编辑旧任务缺字段必须显示关（严格 === true）
        "task.worktree === true",
        # POST/PUT/preview 都带 worktree
        "worktree: $(\"f-worktree\").checked",
        "worktree: wt.worktree, review: wt.review",
        # 有树时不展示必然被后端 409 的“删除”假按钮
        "TERMINAL_STATES.indexOf(state) >= 0 && !hasTree",
    ):
        assert piece in js, piece


def test_css_has_touch_affordance_and_new_state_chips():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for piece in (
        "touch-action: manipulation",   # 手机触摸
        "button:active",                # active 态，不只 hover
        "st-awaiting_merge", "st-merged", "st-discarded",
        "orphan-box", "wt-branch",
    ):
        assert piece in css, piece


# ---------- S6⑤：选 Codex 工人 + 双额度展示 ----------


def test_index_has_runner_radios_and_two_runner_quota_box():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for piece in (
        'name="f-runner"',
        'value="claude"',
        'value="codex"',
        "用谁施工",
        'id="btn-requery"',
    ):
        assert piece in html, piece


def test_app_js_has_runner_selection_and_dual_quota_rendering():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        # 新建/编辑：runner 决定模型/档位下拉的取值来源
        "function runnerModelsEfforts(runner)",
        "function currentRunner()",
        "function populateModelEffortForRunner(runner)",
        "CFG.runners && CFG.runners[runner]",
        # 提交时带 runner；编辑旧任务缺字段按 claude 解释
        "runner: currentRunner()",
        'var runner = task.runner || "claude";',
        # 双额度：两家各自独立渲染，一家失败不牵连另一家
        "function renderQuotaRunner(box, label, entry)",
        'renderQuotaRunner(box, "Claude Code", data.claude);',
        'renderQuotaRunner(box, "Codex", data.codex);',
        # 卡片头部施工标签 + 有 thread_id 只展示短后缀
        '"施工：" + runnerLabel',
        "String(status.thread_id).slice(-8)",
        # Codex 额度到线等刷新的具体时间点；后台任务运行中/待读取摘要
        "等 Codex 额度刷新，",
        "后台任务：",
    ):
        assert piece in js, piece


def test_css_has_runner_chip_and_quota_runner_block():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for piece in ("runner-chip", "quota-runner"):
        assert piece in css, piece
