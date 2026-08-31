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
        # S8②：提交体扩成 worktree + keepalive + review 三件套（旧断言字符串
        # 同步更新，新字符串见 test_app_js_review_block_helpers_and_submission_fields）
        "worktree: wt.worktree, keepalive: wt.keepalive, review: wt.review",
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


# ---------- S6.1 C1-C3：前端尾巴 ----------


def test_quota_refresh_button_text_consistent():
    """C1：初始文案与请求完成后 JS 改回去的文案必须是同一句，不能一个
    "都刷新"一个"重新查额度"。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'id="btn-requery" class="ghost">都刷新<' in html
    assert 'btn.textContent = "都刷新"' in js
    assert "重新查额度" not in js


def test_model_limit_is_runner_aware_and_clears_stale_warn_tokens():
    """C2：runner 切到 context_limit=null 的模型时，没手改过警戒线就该清空
    表单里刚才带来的数字，不能留一个永远不会生效的 token 数。"""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function modelLimit(model, runner)" in js
    assert "modelLimit(currentModel(), currentRunner())" in js
    assert '$("f-warntokens").value = (ratio && limit) ? Math.round(ratio * limit) : "";' in js


def test_quota_shows_explicit_unknown_when_both_windows_missing():
    """C3：两个窗口字段都没数时要明说，不能只剩一句"几分钟前查的"看起来
    像正常有数据。"""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "窗口数据未提供/认不出" in js


# ---------- S7④：模板页七个新键 ----------


def test_index_has_review_template_textareas():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for piece in (
        'id="t-review"', 'id="t-reviewfix"', 'id="t-reviewcriteria"',
        'id="t-reviewwrapup"', 'id="t-reviewstopbuild"', 'id="t-hold"', 'id="t-resume"',
    ):
        assert piece in html, piece


def test_app_js_loads_and_saves_review_templates():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        'cfg.review_template', 'cfg.review_fix_template', 'cfg.review_criteria_text',
        'cfg.review_wrapup_text', 'cfg.review_stop_build_text', 'cfg.hold_text', 'cfg.resume_text',
        'review_template: $("t-review").value',
        'review_fix_template: $("t-reviewfix").value',
        'review_criteria_text: $("t-reviewcriteria").value',
        'review_wrapup_text: $("t-reviewwrapup").value',
        'review_stop_build_text: $("t-reviewstopbuild").value',
        'hold_text: $("t-hold").value',
        'resume_text: $("t-resume").value',
    ):
        assert piece in js, piece


# ---------- S8②：新建页施工/工作树/审稿三块收齐 ----------


def test_index_has_keepalive_and_review_form_controls():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for piece in (
        'id="f-keepalive"',
        'id="box-review"', 'id="f-review-enabled"', 'id="f-review-worktree-hint"',
        'id="review-fields"',
        'name="f-review-runner"',
        'id="f-review-model"', 'id="f-review-model-custom"', 'id="f-review-effort"',
        'id="f-review-maxrounds"',
        'name="f-review-onnoquota"', 'value="release"', 'value="hold"',
        'id="f-review-criteria"',
        'id="f-mergepolicy-label"',
    ):
        assert piece in html, piece


def test_app_js_review_block_helpers_and_submission_fields():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        "function currentReviewRunner()",
        "function currentReviewModel()",
        "function populateReviewModelEffort(runner)",
        "function syncReviewUI()",
        # 关工作树时提交强制 review.enabled=false，不只靠 disabled 样式
        "var reviewOn = wtOn && $(\"f-review-enabled\").checked;",
        "review.on_no_quota = (onnq && onnq.value) || rd.on_no_quota || \"release\";",
        # 提交体：worktree + keepalive + review 一起发
        "worktree: wt.worktree, keepalive: wt.keepalive, review: wt.review",
        "keepalive: { enabled: $(\"f-keepalive\").checked }",
        # 预览带 runner，Codex 任务不会仍按 Claude 上下文渲染
        "worktree: $(\"f-worktree\").checked, runner: currentRunner()",
        # 编辑旧任务（无 runner/model/effort 等字段的 S5 占位 review）也要完整回填
        "$(\"f-review-enabled\").checked = !!review.enabled;",
        "var rd = (CFG && CFG.review_defaults) || {};",
    ):
        assert piece in js, piece


def test_css_has_review_fields_nesting_style():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "#review-fields" in css


# ---------- S8③：任务卡——流水线阶段、六个控制与每轮详情 ----------


def test_index_has_fixnow_overlay():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for piece in (
        'id="fixnow-overlay"', 'id="fixnow-title"', 'id="fixnow-text"',
        'id="btn-fixnow-close"', 'id="btn-fixnow-send"',
    ):
        assert piece in html, piece


def test_app_js_groups_by_pipeline_id():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'var root = item.task.pipeline_id || item.task.root_id || item.task.id;' in js


def test_app_js_has_eight_phase_labels():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        "function pipelinePhaseInfo(chain)",
        '"审稿中"', '"返工中（第 "', '"等审稿额度"', '"等你来看"',
        "返工次数到线", '"等你合并"', '"已合并"', '"已丢弃"',
    ):
        assert piece in js, piece


def test_app_js_has_six_pipeline_control_actions():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        "function pipelineControlActions(chain)",
        '"./api/tasks/" + coordId + "/hold"',
        '"./api/tasks/" + coordId + "/continue"',
        '"./api/tasks/" + coordId + "/keepalive"',
        '"./api/tasks/" + coordId + "/review-now"',
        '"./api/tasks/" + coordId + "/skip-review"',
        "function openFixNow(id, title)",
        '"我来看"', '"继续"', '"暂停保活"', '"恢复保活"',
        '"现在就审"', '"跳过审稿直接收工"', '"直接返工"',
    ):
        assert piece in js, piece


def test_app_js_pipeline_window_actions_are_role_aware():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        "function pipelineWindowActions(chain)",
        'var roleLabel = t.role === "review" ? "审稿" : "施工";',
        '"看" + roleLabel + "屏幕"',
        '"给" + roleLabel + "捎话"',
        '"中止" + roleLabel',
        '"停" + roleLabel + "后台"',
    ):
        assert piece in js, piece


def test_app_js_pipeline_detail_lazy_loads_and_caches_by_pipeline_id():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        "var PIPELINE_DETAIL_CACHE = {};",
        "function pipelineDetailPanel(chain)",
        "function loadPipelineDetail(pipelineId, body)",
        "if (cached && cached.data)", "if (cached && cached.loading) return;",
        '"./api/tasks/" + pipelineId + "/pipeline"',
        "这班没有交接", "这轮还没有意见",
        "function renderPipelineDetail(body, data)",
    ):
        assert piece in js, piece


def test_app_js_pipeline_docs_use_text_node_not_innerhtml():
    """交接/审稿意见正文只走 textContent（el() 的 text 属性），不拼 innerHTML——
    含 <script>/& 之类字符只当文字显示，不会被当成标签执行。"""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js


def test_app_js_review_meta_line_and_effort_in_build_chip():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        '"施工：" + runnerLabel + " · " + task.model + " · " + task.effort',
        "task.review && task.review.enabled",
        '"审稿：" + reviewRunnerLabel',
    ):
        assert piece in js, piece


def test_app_js_held_state_is_active_and_labeled():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert '"waiting_wakeup", "idle", "held"];' in js
    assert 'held: "等待中"' in js


def test_css_has_phase_chip_held_and_pipeline_detail_styles():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for piece in ("st-held", "phase-chip", "pipeline-detail", "pipeline-doc", "overflow-wrap: anywhere"):
        assert piece in css, piece
