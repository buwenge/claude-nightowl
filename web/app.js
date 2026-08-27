/* 夜班单页前端：任务 / 新建 / 模板 三个视图 + 屏幕快照遮罩。
   纯原生 JS，无框架无构建；所有请求带 X-Requested-With: nightshift，
   401 一律跳登录页。 */
"use strict";

var CSRF = { "X-Requested-With": "nightshift" };
var CFG = null;            // /api/config 的内容
var PROMPT_EDITED = false; // 用户手改过最终提示词
var WARN_EDITED = false;   // 用户手改过警戒线 tokens
var HIGHLIGHT_ID = null;   // 新建成功后要高亮的任务
var SCREEN_TASK = null;    // 正在看屏幕的任务 {id, title}
var screenTimer = null;
var previewTimer = null;

/* ---------- 小工具 ---------- */

function $(id) { return document.getElementById(id); }

function el(tag, attrs, children) {
  var node = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach(function (key) {
      if (key === "class") node.className = attrs[key];
      else if (key === "text") node.textContent = attrs[key];
      else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), attrs[key]);
      else node.setAttribute(key, attrs[key]);
    });
  }
  (children || []).forEach(function (child) { node.appendChild(child); });
  return node;
}

var bannerTimer = null;
function banner(text) {
  var node = $("banner");
  node.textContent = text;
  node.classList.add("show");
  if (bannerTimer) clearTimeout(bannerTimer);
  bannerTimer = setTimeout(function () { node.classList.remove("show"); }, 3000);
}

function api(method, path, body) {
  var headers = Object.assign({}, CSRF);
  var opts = { method: method, headers: headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  return fetch(path, opts).then(function (resp) {
    if (resp.status === 401) {
      location.href = "./login.html";
      throw new Error("未登录");
    }
    return resp.text().then(function (text) {
      var data = {};
      if (text) { try { data = JSON.parse(text); } catch (e) { /* 非 JSON */ } }
      if (!resp.ok) {
        var msg = data && data.error ? data.error : "请求失败（" + resp.status + "）";
        if (method !== "GET") banner(msg);
        throw new Error(msg);
      }
      return data;
    });
  }, function () {
    banner("网络错误：连不上服务器");
    throw new Error("网络错误");
  });
}

function fmtLocal(iso) {
  if (!iso) return "-";
  var d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit" });
}

function fmtDelta(ms) {
  if (ms < 0) ms = 0;
  var s = Math.floor(ms / 1000);
  if (s < 60) return s + " 秒";
  var m = Math.floor(s / 60);
  if (m < 60) return m + " 分钟";
  var h = Math.floor(m / 60);
  m = m % 60;
  if (h < 24) return h + " 小时 " + m + " 分";
  return Math.floor(h / 24) + " 天 " + (h % 24) + " 小时";
}

var STATE_TEXT = {
  scheduled: "已排班", postponed: "已推迟", launching: "正在启动", working: "干活中",
  waiting_background: "等背景任务", idle: "一轮干完", chained: "等续班",
  exited: "已退出", finished: "已完成", failed: "已失败",
  cancelled: "已取消", needs_attention: "需要人工", chain_exhausted: "班次用尽"
};

var ACTIVE_STATES = ["launching", "working", "waiting_background", "idle"];
var RUNNOW_STATES = ["scheduled", "postponed", "failed", "cancelled"];
var CANCEL_STATES = ["scheduled", "postponed"];
var TERMINAL_STATES = ["exited", "finished", "failed", "cancelled",
  "chain_exhausted", "needs_attention"];

function groupOf(state) {
  if (ACTIVE_STATES.indexOf(state) >= 0) return 0;
  if (state === "scheduled" || state === "postponed" || state === "chained") return 1;
  return 2;
}

/* ---------- 顶栏分段切换 ---------- */

var VIEWS = ["tasks", "new", "tpl"];
var currentView = "tasks";

function showView(name) {
  currentView = name;
  VIEWS.forEach(function (v) {
    $("view-" + v).hidden = v !== name;
    $("tab-" + v).setAttribute("aria-selected", v === name ? "true" : "false");
  });
  if (name === "tasks") { refreshTasks(); refreshQuota(); }
  if (name === "tpl") loadTemplatesView();
}

/* ---------- 任务页 ---------- */

function refreshTasks() {
  api("GET", "./api/tasks").then(function (items) {
    renderTasks(items || []);
  }).catch(function () { /* banner 已提示 */ });
}

function refreshQuota() {
  api("GET", "./api/quota").then(function (data) {
    renderQuota(data || {});
  }).catch(function () { /* banner 已提示 */ });
}

function barRow(label, pct) {
  var cls = pct < 60 ? "" : (pct < 80 ? "mid" : "high");
  var fill = el("i", { class: cls, style: "width:" + Math.min(100, Math.max(0, pct)) + "%" });
  return el("div", null, [
    el("div", { class: "bar-label" }, [
      el("span", { text: label }),
      el("span", { text: pct + "%" })
    ]),
    el("div", { class: "bar" }, [fill])
  ]);
}

function renderQuota(data) {
  var box = $("quota-body");
  box.textContent = "";
  if (!data || (!data.usage && !data.error)) {
    box.appendChild(el("p", { class: "hint", text: "还没查过（有任务跑起来才查）" }));
    return;
  }
  if (data.error) {
    box.appendChild(el("p", { class: "warn-reason", text: "上次查询失败：" + data.error }));
    return;
  }
  var usage = data.usage;
  if (typeof usage.session_pct === "number") box.appendChild(barRow("五小时", usage.session_pct));
  if (typeof usage.week_all_pct === "number") box.appendChild(barRow("七日（全部模型）", usage.week_all_pct));
  var per = usage.per_model || {};
  Object.keys(per).forEach(function (name) {
    box.appendChild(barRow("七日（" + name + "）", per[name]));
  });
  var agoText = "";
  if (typeof data.age_seconds === "number") {
    var minutes = Math.max(0, Math.round(data.age_seconds / 60));
    agoText = minutes === 0 ? "刚刚查的" : minutes + " 分钟前查的";
  } else if (data.fetched_at) {
    agoText = "查询时间 " + fmtLocal(data.fetched_at);
  }
  box.appendChild(el("p", { class: "quota-line", text: agoText }));
}

function taskActions(item) {
  var task = item.task, status = item.status;
  var state = status.state;
  var box = el("div", { class: "actions" });
  function add(text, cls, handler) {
    box.appendChild(el("button", { type: "button", class: cls, text: text, onclick: handler }));
  }
  if (RUNNOW_STATES.indexOf(state) >= 0) {
    add("现在就跑", "primary", function () {
      api("POST", "./api/tasks/" + task.id + "/run-now")
        .then(function () { refreshTasks(); banner("已改到现在，等调度器下一轮预检"); })
        .catch(function () {});
    });
  }
  if (CANCEL_STATES.indexOf(state) >= 0) {
    add("取消", "danger", function () {
      if (!confirm("确定取消「" + task.title + "」？")) return;
      api("POST", "./api/tasks/" + task.id + "/cancel")
        .then(function () { refreshTasks(); })
        .catch(function () {});
    });
  }
  if (TERMINAL_STATES.indexOf(state) >= 0) {
    add("删除", "danger", function () {
      if (!confirm("删除「" + task.title + "」？任务目录（含事件日志）会一并清掉。")) return;
      if (!confirm("再确认一次：真的删除？")) return;
      api("DELETE", "./api/tasks/" + task.id)
        .then(function () { refreshTasks(); })
        .catch(function () {});
    });
  }
  if (status.window_id) {
    add("看屏幕", "", function () { openScreen(task.id, task.title); });
  }
  return box.childNodes.length ? box : null;
}

function renderTasks(items) {
  var list = $("task-list");
  list.textContent = "";
  if (!items.length) {
    list.appendChild(el("p", { class: "hint", text: "还没有任务。去「新建」页排一个夜班吧。" }));
    return;
  }
  items.sort(function (a, b) {
    var ga = groupOf(a.status.state), gb = groupOf(b.status.state);
    if (ga !== gb) return ga - gb;
    return a.task.run_at < b.task.run_at ? -1 : 1;
  });
  var now = Date.now();
  items.forEach(function (item) {
    list.appendChild(taskCard(item, now));
  });
}

function taskCard(item, now) {
  var task = item.task, status = item.status;
  var state = status.state || "-";
  var card = el("article", { class: "card" + (task.id === HIGHLIGHT_ID ? " flash" : "") });

  card.appendChild(el("div", { class: "task-head" }, [
    el("span", { class: "chip st-" + state, text: STATE_TEXT[state] || state }),
    el("span", { class: "task-title", text: task.title }),
    el("span", { class: "task-id", text: task.id })
  ]));
  card.appendChild(el("div", { class: "task-meta", text:
    "项目 " + task.project + " · 模型 " + task.model + " · 档位 " + task.effort +
    (task.shift > 1 ? " · 第 " + task.shift + " 班" : "") }));

  // 计划时间与倒计时 / 已跑时长
  var whenText;
  if (state === "postponed" && status.next_attempt_at) {
    var due = new Date(status.next_attempt_at) - now;
    whenText = "下次尝试 " + fmtLocal(status.next_attempt_at) + "（" +
      (due > 0 ? "还有 " + fmtDelta(due) : "已到点，等下一轮调度") + "）";
  } else if (ACTIVE_STATES.indexOf(state) >= 0 && status.launched_at) {
    whenText = "开跑于 " + fmtLocal(status.launched_at) +
      "（已跑 " + fmtDelta(now - new Date(status.launched_at)) + "）";
  } else {
    var left = new Date(task.run_at) - now;
    whenText = "计划 " + fmtLocal(task.run_at) + "（" +
      (left > 0 ? "还有 " + fmtDelta(left) : "已到点") + "）";
  }
  card.appendChild(el("div", { class: "task-when", text: whenText }));

  // 上下文水位条
  var limit = (task.guards && task.guards.context_limit_tokens) ||
    (CFG && CFG.models && CFG.models[task.model] && CFG.models[task.model].context_limit);
  var pct = (typeof status.context_pct === "number") ? status.context_pct :
    (status.context_tokens && limit ? Math.round(100 * status.context_tokens / limit) : null);
  if (pct !== null) {
    var tokText = status.context_tokens ?
      (Math.round(status.context_tokens / 1000) + "k" + (limit ? " / " + Math.round(limit / 1000) + "k" : "")) : "";
    card.appendChild(barRow("上下文 " + tokText, pct));
  }

  if (status.quota_at_launch) {
    var q = status.quota_at_launch;
    var parts = [];
    if (typeof q.session_pct === "number") parts.push("五小时 " + q.session_pct + "%");
    if (typeof q.week_all_pct === "number") parts.push("七日 " + q.week_all_pct + "%");
    Object.keys(q.per_model || {}).forEach(function (name) {
      parts.push(name + " " + q.per_model[name] + "%");
    });
    if (parts.length) {
      card.appendChild(el("div", { class: "quota-line",
        text: "起跑时额度：" + parts.join(" · ") }));
    }
  }

  if (status.last_message) {
    var msg = el("div", {
      class: "lastmsg clamp", text: status.last_message,
      onclick: function () { msg.classList.toggle("clamp"); }
    });
    card.appendChild(msg);
  }
  var reason = status.postpone_reason || status.error;
  if (reason) card.appendChild(el("p", { class: "warn-reason", text: reason }));

  if (item.events_tail && item.events_tail.length) {
    var pre = el("pre", { text: item.events_tail.join("\n") });
    card.appendChild(el("details", { class: "events" }, [
      el("summary", { text: "最近事件（" + item.events_tail.length + " 条）" }),
      pre
    ]));
  }

  var actions = taskActions(item);
  if (actions) card.appendChild(actions);
  return card;
}

/* ---------- 屏幕快照 ---------- */

function loadScreen() {
  if (!SCREEN_TASK) return;
  api("GET", "./api/tasks/" + SCREEN_TASK.id + "/screen?lines=200")
    .then(function (data) {
      $("screen-text").textContent = data.text || "（窗口没有输出）";
    })
    .catch(function (err) {
      $("screen-text").textContent = "抓取失败：" + err.message;
    });
}

function openScreen(id, title) {
  SCREEN_TASK = { id: id, title: title };
  $("screen-title").textContent = "屏幕快照 · " + title;
  $("screen-text").textContent = "加载中…";
  $("screen-overlay").classList.add("show");
  loadScreen();
  if (screenTimer) clearInterval(screenTimer);
  screenTimer = setInterval(function () {
    if (!document.hidden) loadScreen();
  }, 5000);
}

function closeScreen() {
  SCREEN_TASK = null;
  $("screen-overlay").classList.remove("show");
  if (screenTimer) { clearInterval(screenTimer); screenTimer = null; }
}

/* ---------- 新建页 ---------- */

function modelLimit(model) {
  return CFG && CFG.models && CFG.models[model] ?
    CFG.models[model].context_limit : null;
}

function currentModel() {
  return $("f-model").value === "__custom__" ?
    $("f-model-custom").value.trim() : $("f-model").value;
}

function recalcWarnDefault() {
  if (WARN_EDITED) return;
  var ratio = CFG && CFG.guards ? CFG.guards.context_warn_ratio : null;
  var limit = modelLimit(currentModel());
  if (ratio && limit) $("f-warntokens").value = Math.round(ratio * limit);
}

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = setTimeout(function () {
    if (PROMPT_EDITED) return;
    var title = $("f-title").value;
    var project = $("f-project").value;
    var model = currentModel();
    var text = $("f-text").value;
    if (!project || !text) return;
    api("POST", "./api/preview", { title: title, project: project, model: model, task_text: text })
      .then(function (data) {
        if (PROMPT_EDITED) return;
        $("f-prompt").value = data.prompt_final;
      })
      .catch(function () {});
  }, 400);
}

function markPromptEdited() {
  PROMPT_EDITED = true;
  $("tag-edited").classList.add("show");
  $("btn-regen").hidden = false;
}

function unmarkPromptEdited() {
  PROMPT_EDITED = false;
  $("tag-edited").classList.remove("show");
  $("btn-regen").hidden = true;
}

function populateNewForm() {
  var project = $("f-project");
  project.textContent = "";
  Object.keys(CFG.projects).forEach(function (name) {
    project.appendChild(el("option", { value: name, text: name }));
  });
  var model = $("f-model");
  model.textContent = "";
  Object.keys(CFG.models).forEach(function (name) {
    model.appendChild(el("option", { value: name, text: name }));
  });
  model.appendChild(el("option", { value: "__custom__", text: "自定义…" }));
  var effort = $("f-effort");
  effort.textContent = "";
  (CFG.efforts || []).forEach(function (name) {
    effort.appendChild(el("option", { value: name, text: name }));
  });
  if ((CFG.efforts || []).indexOf("high") >= 0) effort.value = "high";
  $("f-warntext").value = CFG.context_warn_text || "";
  if (CFG.chain) {
    if (typeof CFG.chain.max_windows === "number") $("f-chainmax").value = CFG.chain.max_windows;
    $("f-nohandover").value = CFG.chain.on_no_handover || "continue";
  }
  recalcWarnDefault();
}

function submitNewForm(ev) {
  ev.preventDefault();
  var errBox = $("new-err");
  errBox.textContent = "";
  var title = $("f-title").value.trim();
  var project = $("f-project").value;
  var model = currentModel();
  var effort = $("f-effort").value;
  var runAtRaw = $("f-runat").value;
  var text = $("f-text").value;
  var prompt = $("f-prompt").value;

  if (!title) { errBox.textContent = "标题不能为空"; return; }
  if (!project) { errBox.textContent = "项目不能为空"; return; }
  if (!model) { errBox.textContent = "模型不能为空（选一个或手输）"; return; }
  if (!runAtRaw) { errBox.textContent = "开跑时间不能为空——没有默认时间，请自己选"; return; }
  if (!text.trim()) { errBox.textContent = "任务内容不能为空"; return; }
  if (!prompt.trim()) { errBox.textContent = "最终提示词不能为空"; return; }

  var runAtIso;
  try { runAtIso = new Date(runAtRaw).toISOString(); }
  catch (e) { errBox.textContent = "开跑时间认不出来"; return; }

  var guards = {};
  var warnTokens = $("f-warntokens").value;
  if (warnTokens !== "") guards.context_warn_tokens = Number(warnTokens);
  if ($("f-warntext").value !== (CFG.context_warn_text || ""))
    guards.context_warn_text = $("f-warntext").value;
  var chain = {};
  if ($("f-chainmax").value !== "")
    chain.max_windows = Number($("f-chainmax").value);
  if (CFG.chain && $("f-nohandover").value !== CFG.chain.on_no_handover)
    chain.on_no_handover = $("f-nohandover").value;

  api("POST", "./api/tasks", {
    title: title, project: project, model: model, effort: effort,
    run_at: runAtIso, task_text: text, prompt_final: prompt,
    guards: guards, chain: chain
  }).then(function (data) {
    HIGHLIGHT_ID = data.id;
    showView("tasks");
    banner("任务已建：" + data.id);
  }).catch(function () { /* 错误已显示 */ });
}

/* ---------- 模板页 ---------- */

function loadTemplatesView() {
  api("GET", "./api/config").then(function (cfg) {
    CFG = cfg;
    $("t-prompt").value = cfg.prompt_template || "";
    $("t-warntext").value = cfg.context_warn_text || "";
    $("t-chain").value = cfg.chain_template || "";
  }).catch(function () {});
}

function saveTemplates() {
  var note = $("tpl-note");
  note.textContent = "";
  api("PUT", "./api/templates", {
    prompt_template: $("t-prompt").value,
    context_warn_text: $("t-warntext").value,
    chain_template: $("t-chain").value
  }).then(function () {
    note.textContent = "已保存";
    setTimeout(function () { note.textContent = ""; }, 3000);
  }).catch(function () { /* banner 已提示 */ });
}

/* ---------- 启动 ---------- */

function start() {
  $("tab-tasks").addEventListener("click", function () { showView("tasks"); });
  $("tab-new").addEventListener("click", function () { showView("new"); });
  $("tab-tpl").addEventListener("click", function () { showView("tpl"); });
  $("btn-logout").addEventListener("click", function () {
    api("POST", "./api/logout").catch(function () {}).then(function () {
      location.href = "./login.html";
    });
  });
  $("btn-screen-close").addEventListener("click", closeScreen);

  $("new-form").addEventListener("submit", submitNewForm);
  $("f-title").addEventListener("input", schedulePreview);
  $("f-project").addEventListener("change", schedulePreview);
  $("f-model").addEventListener("change", function () {
    $("f-model-custom").hidden = $("f-model").value !== "__custom__";
    recalcWarnDefault();
    schedulePreview();
  });
  $("f-model-custom").addEventListener("input", function () {
    recalcWarnDefault();
    schedulePreview();
  });
  $("f-text").addEventListener("input", schedulePreview);
  $("f-warntokens").addEventListener("input", function () { WARN_EDITED = true; });
  $("f-prompt").addEventListener("input", markPromptEdited);
  $("btn-regen").addEventListener("click", function () {
    unmarkPromptEdited();
    schedulePreview();
  });
  $("btn-save-tpl").addEventListener("click", saveTemplates);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && currentView === "tasks") { refreshTasks(); refreshQuota(); }
  });
  setInterval(function () {
    if (currentView === "tasks" && !document.hidden) { refreshTasks(); refreshQuota(); }
  }, 5000);

  api("GET", "./api/config").then(function (cfg) {
    CFG = cfg;
    populateNewForm();
    showView("tasks");
  }).catch(function () {
    // 401 已跳登录；其余情况仍试着渲染空界面
    showView("tasks");
  });
}

document.addEventListener("DOMContentLoaded", start);
