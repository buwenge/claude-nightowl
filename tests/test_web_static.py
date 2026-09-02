"""web 静态断言（S5③）：前端工作树收口的关键元素与接口字符串都在。

不发请求、不开浏览器、不起服务器——只读 web/ 三个文件做字符串级检查，
防"改了后端忘了前端"或反过来。
"""

from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"

# S8.1 阻断三：字符串级检查测不出"重复 id / 孤儿节点游离在 section 外"这
# 类结构问题（旧字符串断言在重复内容存在时照样全部通过）——用标准库
# html.parser 做一次真正的结构解析，锁住 id 唯一性和"新建表单控件只应该
# 出现在 #new-form 内、任务页看不到它们"这两条。


class _IdCollector(HTMLParser):
    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__()
        self.stack: list[str | None] = []
        self.id_count: dict[str, int] = {}
        self.id_ancestors: dict[str, set[str]] = {}

    def _record(self, this_id):
        if not this_id:
            return
        self.id_count[this_id] = self.id_count.get(this_id, 0) + 1
        self.id_ancestors.setdefault(this_id, set()).update(a for a in self.stack if a)

    def handle_starttag(self, tag, attrs):
        this_id = dict(attrs).get("id")
        self._record(this_id)
        if tag not in self._VOID_TAGS:
            self.stack.append(this_id)

    def handle_startendtag(self, tag, attrs):
        self._record(dict(attrs).get("id"))

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()


def _parse_ids(html_text: str) -> _IdCollector:
    parser = _IdCollector()
    parser.feed(html_text)
    return parser


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
        "if (cached && cached.data)",
        # S8.1 非阻断竞态收尾：飞行中的请求改存 Promise，新 body 挂到同一个
        # Promise 的完成/失败分支，不是命中 loading 哨兵就直接返回
        "if (cached && cached.promise)",
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


# ---------- S8④：模板、双额度与手机视觉总收口 ----------


def test_index_has_all_new_template_textareas_grouped():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for piece in (
        'class="tpl-group"',
        'id="t-codexquotapause"', 'id="t-codexresume"', 'id="t-codexstopbg"',
        'id="t-reviewresume"', 'id="t-reviewholdresume"', 'id="t-buildholdresume"',
        'id="t-keepaliveclaude"', 'id="t-keepalivecodex"', 'id="box-keepalivecodex"',
        # 老键全部还在（不许删旧模板能力）
        'id="t-prompt"', 'id="t-warntext"', 'id="t-quotapause"', 'id="t-quotawrap"',
        'id="t-quotaother"', 'id="t-chain"', 'id="t-stopbg"', 'id="t-stuckinterrupt"',
        'id="t-review"', 'id="t-reviewfix"', 'id="t-reviewcriteria"', 'id="t-reviewwrapup"',
        'id="t-reviewstopbuild"', 'id="t-hold"', 'id="t-resume"',
        'id="btn-save-tpl"',  # 只有一个主保存按钮
    ):
        assert piece in html, piece
    assert html.count('id="btn-save-tpl"') == 1


def test_app_js_templates_load_and_save_new_keys_with_split_runner_keepalive_put():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        "cfg.codex_quota_pause_text", "cfg.codex_resume_text", "cfg.codex_stop_background_text",
        "cfg.review_resume_text", "cfg.review_hold_resume_text", "cfg.build_hold_resume_text",
        "runners.claude && runners.claude.keepalive_text",
        "runners.codex && runners.codex.keepalive_text",
        "codex_quota_pause_text: $(\"t-codexquotapause\").value",
        "review_resume_text: $(\"t-reviewresume\").value",
        # 主体文案与 runner_keepalive_text 分两次 PUT，不因后者可选失败拖累前者
        'return api("PUT", "./api/templates", { runner_keepalive_text: runnerKt });',
    ):
        assert piece in js, piece


def test_index_warmup_label_is_claude_code_specific():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "每天预热 Claude Code 五小时窗口" in html
    assert "Codex 没有预热开关" in html


def test_app_js_does_not_add_codex_warmup_toggle():
    """不给 Codex 加预热开关：模板/预热相关代码里不出现 codex 预热字样。"""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "codex_warmup" not in js
    assert "codexWarmup" not in js


def test_overlays_support_escape_backdrop_close_and_focus_return():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for piece in (
        "function trackFocusBeforeOverlay()", "function restoreFocusAfterOverlay()",
        "var OVERLAY_CLOSERS = ", '"Escape"',
        "restoreFocusAfterOverlay();",
    ):
        assert piece in js, piece


def test_css_mobile_wrap_and_no_horizontal_scroll():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for piece in ("overflow-wrap: anywhere", "overflow-x: hidden", ".tpl-group"):
        assert piece in css, piece


# ---------- S8.1 阻断三：index.html 重复表单结构反例 ----------


def test_index_all_ids_unique():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    parser = _parse_ids(html)
    dups = {k: v for k, v in parser.id_count.items() if v > 1}
    assert dups == {}, dups


def test_index_new_form_controls_appear_exactly_once_inside_new_form():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    parser = _parse_ids(html)
    for target in (
        "f-sessionleft", "f-weekleft", "f-modelleft", "f-autointerrupt",
        "f-chainmax", "f-nohandover", "f-mergepolicy", "f-prompt",
        "new-err", "new-submit", "prompt-box", "tag-edited", "btn-regen",
    ):
        assert parser.id_count.get(target) == 1, target
        assert "new-form" in parser.id_ancestors.get(target, set()), target


def test_index_task_page_has_no_orphan_new_form_controls():
    """阻断三反例：commit③ 合并 patch 时曾把 f-sessionleft/f-prompt/
    new-submit 等一整段新建表单尾段重复插在 </section> 之外，变成游离节点，
    浏览器解析后会作为任务页的兄弟内容一直显示（不受 view-new 的
    hidden 属性控制）。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    parser = _parse_ids(html)
    for target in ("f-sessionleft", "f-prompt", "new-submit", "f-nohandover", "f-mergepolicy"):
        ancestors = parser.id_ancestors.get(target, set())
        assert "view-tasks" not in ancestors, target
        assert "view-new" in ancestors, target


def test_index_view_new_appears_exactly_once():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert html.count('<section id="view-new"') == 1


# ---------- S8.1 阻断二：syncReviewUI() 不得重建审稿模型/档位下拉 ----------


def _extract_js_function_body(js: str, name: str) -> str:
    """粗糙但够用的花括号配平提取：找 `function <name>(...) {`，从那对
    应的 `{` 数括号配平到函数体结束。测试专用，不追求处理任意 JS 语法。"""
    marker = f"function {name}("
    start = js.index(marker)
    brace_start = js.index("{", start)
    depth = 0
    i = brace_start
    while i < len(js):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[brace_start:i + 1]
        i += 1
    raise AssertionError(f"未找到 {name} 的完整函数体（花括号未配平）")


def test_sync_review_ui_never_rebuilds_model_effort_options():
    """阻断二：syncReviewUI() 以前无条件调用 populateReviewModelEffort()，
    每次工作树/审稿开关变化（包括跟 runner 无关的场景）都会清空重建两个
    下拉，把用户已经选好的非默认模型/档位打回第一个选项。修复后
    syncReviewUI() 只管 hidden/disabled/文案，不碰选项内容。"""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    body = _extract_js_function_body(js, "syncReviewUI")
    assert "populateReviewModelEffort" not in body


def test_review_model_effort_only_populated_at_three_legitimate_entry_points():
    """真正需要重建选项的三处——新建默认值、编辑回填、用户手动切换审稿
    runner——各自显式调用，且编辑回填先 populate 后设置 .value（不会被
    后续任何调用覆盖）。"""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    create_body = _extract_js_function_body(js, "enterCreate")
    edit_body = _extract_js_function_body(js, "enterEdit")
    assert 'populateReviewModelEffort("claude")' in create_body
    assert "populateReviewModelEffort(reviewRunner)" in edit_body
    # 编辑回填：populate 必须先于设值，否则会覆盖掉刚设好的 .value
    populate_pos = edit_body.index("populateReviewModelEffort(reviewRunner)")
    set_model_pos = edit_body.index('$("f-review-model").value = review.model')
    assert populate_pos < set_model_pos
    assert 'r.addEventListener("change", function () { populateReviewModelEffort(currentReviewRunner()); });' in js


def test_review_worktree_toggle_round_trip_preserves_non_default_selection():
    """用一次真实的"关工作树再开"序列核对：populateReviewModelEffort 只在
    三个合法入口出现，不在 syncWorktreeUI/syncReviewUI 调用链路上——静态
    核对调用图，浏览器交互回归见监理真机复核记录。"""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    sync_wt_body = _extract_js_function_body(js, "syncWorktreeUI")
    assert "populateReviewModelEffort" not in sync_wt_body
    assert "syncReviewUI()" in sync_wt_body  # 仍然要调用，只是它自己不再重建选项


def test_after_task_dropdown_hides_ended_chains_and_lists_newest_first():
    """9/1 工头要求：前置任务下拉不列已结束的链（终态或等合并），新建的排最上。

    字符串级锁：过滤常量由 TERMINAL_STATES 派生并补 awaiting_merge；填充前先
    filter 再按 root 倒排；编辑态当前选中的前置即使已结束也保留。
    """
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'var AFTER_HIDDEN_STATES = TERMINAL_STATES.concat(["awaiting_merge"]);' in js
    body = js[js.index("function refreshTriggerChoices()"):]
    body = body[:body.index("\n}\n")]
    assert "AFTER_HIDDEN_STATES.indexOf(st) < 0 || chain.root === want" in body
    assert "chains.sort(function (a, b) { return a.root < b.root ? 1 : (a.root > b.root ? -1 : 0); });" in body
    assert body.index(".filter(") < body.index("chains.sort(") < body.index("chains.forEach(")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "前置任务（最新在前；已结束的链不列）" in html


# ---------- 9/1 Fable 审查 C 组：前端六处 ----------

import re as _re


def _js_fn(name: str) -> str:
    js = (WEB / "app.js").read_text(encoding="utf-8")
    m = _re.search(rf"\nfunction {name}\(.*?\n}}\n", js, _re.S)
    assert m, name
    return m.group(0)


def test_submit_button_disabled_while_request_in_flight():
    """手机双击「建任务」不能建出两个任务（工作树任务互不排斥，两个都会起）。"""
    body = _js_fn("submitNewForm")
    assert "SUBMIT_INFLIGHT" in body
    assert _re.search(r'\$\("new-submit"\)\.disabled\s*=\s*true', body)
    assert "rescheduled" in body  # 编辑 failed/cancelled 后如实说有没有重排


def test_enter_create_resets_form_left_over_from_edit():
    body = _js_fn("enterCreate")
    assert "reset()" in body and "FORM_STALE" in body
    assert "FORM_STALE = true" in _js_fn("enterEdit")


def test_task_card_uses_runner_aware_model_limit():
    body = _js_fn("taskCard")
    assert "modelLimit(task.model" in body and "CFG.models[task.model]" not in body


def test_pipeline_detail_cache_invalidated_when_members_change():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function pipelineSignature(" in js
    body = _js_fn("pipelineDetailPanel")
    assert "PIPELINE_DETAIL_CACHE[chain.root] = null" in body


def test_refresh_tasks_guards_stale_response_and_reports_failure():
    body = _js_fn("refreshTasks")
    assert "TASKS_SEQ" in body
    assert _re.search(r"catch\(function \(err\)\s*\{[^}]*banner\(", body, _re.S)
