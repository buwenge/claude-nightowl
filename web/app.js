/* 夜班单页前端：任务 / 新建 / 模板 三个视图 + 屏幕快照遮罩。
   纯原生 JS，无框架无构建；所有请求带 X-Requested-With: nightshift，
   401 一律跳登录页。 */
"use strict";

var CSRF = { "X-Requested-With": "nightshift" };
var UI_OPEN = {};          // 用户展开过的东西（最后一句/最近事件），列表重画时保持
var CFG = null;            // /api/config 的内容
var PROMPT_EDITED = false; // 用户手改过最终提示词
var WARN_EDITED = false;   // 用户手改过警戒线 tokens
var HIGHLIGHT_ID = null;   // 新建成功后要高亮的任务
var SCREEN_TASK = null;    // 正在看屏幕的任务 {id, title}
var EDIT_TASK = null;      // 正在编辑的任务 {id, item, active}；null = 新建模式
var MSG_TASK = null;       // 正在捎话的任务 {id, title}
var screenTimer = null;
var previewTimer = null;
var LAST_ITEMS = [];       // 上一次 /api/tasks 的原始结果，供日期筛选本地重画用
var SELECTED_DAY = null;   // 选中的日期 "YYYY-MM-DD"（本地时区），null = 不筛选
var CAL_MONTH = new Date();  // 日历当前显示的月份（任意一天即可，只取年月）
var CAL_OPEN = false;      // 日期面板是否展开

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

// 本地时区的 "YYYY-MM-DD"，用于日历分天（跟 fmtLocal 一样按浏览器本地时区走）
function dayKey(d) {
  var y = d.getFullYear(), m = d.getMonth() + 1, day = d.getDate();
  return y + "-" + (m < 10 ? "0" : "") + m + "-" + (day < 10 ? "0" : "") + day;
}
function dayKeyFromIso(iso) {
  var d = new Date(iso);
  return isNaN(d) ? null : dayKey(d);
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

function copyText(text) {
  // 剪贴板 API 优先（要 https/localhost），不行退回 execCommand 兜底
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise(function (resolve, reject) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    if (ok) resolve(); else reject(new Error("copy failed"));
  });
}

var STATE_TEXT = {
  scheduled: "已排班", postponed: "已推迟", launching: "正在启动", working: "干活中",
  waiting_background: "等背景任务", waiting_wakeup: "等闹钟", idle: "一轮干完", chained: "已续班",
  exited: "已退出", finished: "已完成", failed: "已失败",
  cancelled: "已取消", needs_attention: "需要人工", chain_exhausted: "班次用尽",
  awaiting_merge: "等你合并", merged: "已合并", discarded: "已丢弃"
};

var ACTIVE_STATES = ["launching", "working", "waiting_background", "waiting_wakeup", "idle"];
var RUNNOW_STATES = ["scheduled", "postponed", "failed", "cancelled"];
var CANCEL_STATES = ["scheduled", "postponed"];
var TERMINAL_STATES = ["exited", "finished", "failed", "cancelled",
  "chain_exhausted", "needs_attention", "chained", "merged", "discarded"];
// 有树且等人工的状态：卡片给"合并进主线 / 丢弃 / 先留着"
var MERGE_BUTTON_STATES = ["awaiting_merge", "needs_attention"];

function groupOf(state) {
  if (state === "awaiting_merge") return 0;  // 等合并：最前面的待处理组
  if (ACTIVE_STATES.indexOf(state) >= 0) return 1;
  if (state === "scheduled" || state === "postponed") return 2;
  return 3;
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
  if (name === "tasks") { refreshTasks(); refreshQuota(); window.scrollTo(0, 0); }
  if (name === "new") refreshTriggerChoices();
  if (name === "tpl") loadTemplatesView();
}

/* ---------- 任务页 ---------- */

// 终态组按 run_at 降序那段依赖 groupOf 的编号，awaiting_merge 在 0、终态在 3
function refreshTasks() {
  api("GET", "./api/tasks").then(function (items) {
    renderTasks(items || []);
  }).catch(function () { /* banner 已提示 */ });
  api("GET", "./api/worktrees").then(function (orphans) {
    renderOrphans(orphans || []);
  }).catch(function () { /* 拉不到就不显示，不拦任务列表 */ });
}

function renderOrphans(orphans) {
  var box = $("orphan-box");
  box.textContent = "";
  if (!orphans.length) { box.hidden = true; return; }
  box.hidden = false;
  var det = el("details", { class: "orphan-details" }, [
    el("summary", { text: "发现 " + orphans.length + " 棵孤儿工作树（只提示，不会自动删）" })
  ]);
  orphans.forEach(function (o) {
    det.appendChild(el("div", { class: "orphan-item" }, [
      el("div", { text: "项目 " + (o.project || "-") + " · 分支 " + (o.branch || "-") }),
      el("div", { class: "orphan-path mono", text: o.path || "-" }),
      el("div", { class: "hint", text: o.reason || "" })
    ]));
  });
  box.appendChild(det);
}

function refreshQuota() {
  api("GET", "./api/quota").then(function (data) {
    renderQuota(data || {});
  }).catch(function () { /* banner 已提示 */ });
  api("GET", "./api/config").then(function (cfg) { CFG = cfg; renderWarmup(cfg); }).catch(function () {});
}

// /usage 的 "Aug 27, 6:40pm (UTC)" → Date（按 UTC 解析；年份取当前年，明显过去就算下一年）
var MONTHS = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };
function parseResets(text) {
  if (!text) return null;
  var m = /([A-Z][a-z]{2}) (\d{1,2}), (\d{1,2})(?::(\d{2}))?(am|pm)\s*\(UTC\)/.exec(text);
  if (!m || !(m[1] in MONTHS)) return null;
  var hour = Number(m[3]) % 12 + (m[5] === "pm" ? 12 : 0);
  var now = new Date();
  var d = new Date(Date.UTC(now.getUTCFullYear(), MONTHS[m[1]], Number(m[2]), hour, Number(m[4] || 0)));
  if (d.getTime() < now.getTime() - 86400000) d = new Date(Date.UTC(now.getUTCFullYear() + 1, MONTHS[m[1]], Number(m[2]), hour, Number(m[4] || 0)));
  return d;
}

function resetsLine(text) {
  var d = parseResets(text);
  if (!d) return text ? "刷新：" + text : "";
  var left = d.getTime() - Date.now();
  var local = d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  return "刷新：" + local + "（" + (left > 0 ? "还有 " + fmtDelta(left) : "已到，下次查询会更新") + "）";
}

// 额度条一律显示"剩余"：传进来的是已用百分比，这里换算；颜色按剩余多少（少于 20% 红）
function remainRow(label, usedPct, resetsText) {
  // 条按"已用"增长（跟 Claude 自己的 /usage 一致），文字标"剩多少"；用超 80% 变红
  var used = Math.min(100, Math.max(0, usedPct));
  var left = 100 - used;
  var cls = used < 60 ? "" : (used < 80 ? "mid" : "high");
  var fill = el("i", { class: cls, style: "width:" + used + "%" });
  var kids = [
    el("div", { class: "bar-label" }, [
      el("span", { text: label }),
      el("span", { text: "剩 " + left + "%" })
    ]),
    el("div", { class: "bar" }, [fill])
  ];
  var line = resetsLine(resetsText);
  if (line) kids.push(el("div", { class: "bar-resets", text: line }));
  return el("div", { class: "bar-block" }, kids);
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

// S6⑤：一家的额度块（Claude 或 Codex）——数据未知就写"查不到/未提供"，
// 绝不画成 0%；一家失败不牵连另一家（各自独立调用，见 renderQuota）。
function renderQuotaRunner(box, label, entry) {
  var section = el("div", { class: "quota-runner" }, [el("h3", { text: label })]);
  entry = entry || {};
  if (!entry.usage && !entry.error) {
    section.appendChild(el("p", { class: "hint", text: "还没查过（有任务跑起来才查）" }));
    box.appendChild(section);
    return;
  }
  if (entry.error) {
    section.appendChild(el("p", { class: "warn-reason", text: "上次查询失败：" + entry.error }));
    box.appendChild(section);
    return;
  }
  var usage = entry.usage;
  var hasSession = typeof usage.session_pct === "number";
  var hasWeekAll = typeof usage.week_all_pct === "number";
  if (hasSession) section.appendChild(remainRow("五小时", usage.session_pct, usage.session_resets));
  if (hasWeekAll) section.appendChild(remainRow("七日（全部模型）", usage.week_all_pct, usage.week_all_resets));
  // S6.1 C3：两个窗口都没数（比如 Codex 账号返回的字段认不出）不能只剩一句
  // "几分钟前查的"，那看起来像正常、只是没数据——要明说查不到/认不出。
  if (!hasSession && !hasWeekAll) {
    section.appendChild(el("p", { class: "hint", text: "窗口数据未提供/认不出" }));
  }
  var per = usage.per_model || {};
  var perResets = usage.per_model_resets || {};
  Object.keys(per).forEach(function (name) {
    section.appendChild(remainRow("七日（" + name + "）", per[name], perResets[name]));
  });
  if (typeof usage.reset_credits_available === "number") {
    section.appendChild(el("p", { class: "quota-line",
      text: "免费重置券：" + usage.reset_credits_available + " 张（不自动兑换）" }));
  }
  var agoText = "";
  if (typeof entry.age_seconds === "number") {
    var minutes = Math.max(0, Math.round(entry.age_seconds / 60));
    agoText = minutes === 0 ? "刚刚查的" : minutes + " 分钟前查的";
  } else if (entry.fetched_at) {
    agoText = "查询时间 " + fmtLocal(entry.fetched_at);
  }
  if (agoText) section.appendChild(el("p", { class: "quota-line", text: agoText }));
  box.appendChild(section);
}

function renderQuota(data) {
  var box = $("quota-body");
  box.textContent = "";
  data = data || {};
  renderQuotaRunner(box, "Claude Code", data.claude);
  renderQuotaRunner(box, "Codex", data.codex);
}

var WARMUP_DIRTY = false;  // 用户动过预热控件、还没保存成功：轮询不许覆盖

function warmupTimesFromDom() {
  var out = [];
  Array.prototype.forEach.call(document.querySelectorAll("#w-times input[type=time]"), function (inp) {
    if (inp.value) out.push(inp.value);
  });
  return out;
}

function addWarmupRow(value) {
  var inp = el("input", { type: "time", value: value || "" });
  inp.addEventListener("change", function () { WARMUP_DIRTY = true; saveWarmup(); });
  var row = el("div", { class: "row" }, [
    inp,
    el("button", { type: "button", class: "ghost x", text: "×", onclick: function () {
      row.remove(); WARMUP_DIRTY = true; saveWarmup();
    } })
  ]);
  $("w-times").appendChild(row);
  return inp;
}

function renderWarmup(cfg) {
  var w = cfg.warmup || {};
  if (!WARMUP_DIRTY) {
    $("w-enabled").checked = !!w.enabled;
    var box = $("w-times");
    box.textContent = "";
    var times = (w.times && w.times.length) ? w.times : (w.time_local ? [w.time_local] : []);
    times.forEach(function (t) { addWarmupRow(t); });
  }
  var st = cfg.warmup_state || {};
  var text = "";
  if (st.last_run_at) {
    text = "上次预热：" + fmtLocal(st.last_run_at) + (st.slot ? "（" + st.slot + " 那次）" : "") + (st.ok ? "（成功，" + (st.model || "") + " 回了「" + (st.reply || "") + "」）" : "（失败：" + (st.error || "") + "）");
  } else if (w.enabled) {
    text = "还没预热过，今天 " + ((w.times && w.times.join("、")) || w.time_local || "") + " 到点自动发";
  }
  $("warmup-state").textContent = text;
}

function saveWarmup() {
  var enabled = $("w-enabled").checked, times = warmupTimesFromDom();
  if (enabled && !times.length) { banner("先选至少一个时刻"); return; }
  api("PUT", "./api/warmup", { enabled: enabled, times: times })
    .then(function () { WARMUP_DIRTY = false; banner("预热设置已保存"); return api("GET", "./api/config"); })
    .then(function (cfg) { CFG = cfg; renderWarmup(cfg); })
    .catch(function () {});
}

function taskActions(item, chainIds) {
  var task = item.task, status = item.status;
  var state = status.state;
  var hasTree = !!status.worktree_path;
  var box = el("div", { class: "actions" });
  function add(text, cls, handler) {
    box.appendChild(el("button", { type: "button", class: cls, text: text, onclick: handler }));
  }
  chainIds = chainIds || [task.id];
  if (RUNNOW_STATES.indexOf(state) >= 0) {
    add("现在就跑", "primary", function () {
      api("POST", "./api/tasks/" + task.id + "/run-now")
        .then(function () { refreshTasks(); banner("已改到现在，等调度器下一轮预检"); })
        .catch(function () {});
    });
  }
  if (RUNNOW_STATES.indexOf(state) >= 0 || ACTIVE_STATES.indexOf(state) >= 0) {
    add("编辑", "", function () { enterEdit(item); });
  }
  if (CANCEL_STATES.indexOf(state) >= 0) {
    add("取消", "danger", function () {
      if (!confirm("确定取消「" + task.title + "」？")) return;
      api("POST", "./api/tasks/" + task.id + "/cancel")
        .then(function () { refreshTasks(); })
        .catch(function () {});
    });
  }
  if (TERMINAL_STATES.indexOf(state) >= 0 && !hasTree) {
    var many = chainIds.length > 1;
    add(many ? "删除（整条链 " + chainIds.length + " 班）" : "删除", "danger", function () {
      if (!confirm("删除「" + task.title + "」" + (many ? "的全部 " + chainIds.length + " 班" : "") + "？任务目录（含事件日志）会一并清掉。")) return;
      if (!confirm("再确认一次：真的删除？")) return;
      // 逐个删，删完再刷；某一班不是终态会被服务器 409 挡住，其余照删
      chainIds.reduce(function (p, id) {
        return p.then(function () { return api("DELETE", "./api/tasks/" + id).catch(function () {}); });
      }, Promise.resolve()).then(function () { refreshTasks(); });
    });
  }
  var live = status.window_id && !status.session_ended_at;  // 会话已关的窗口动不了
  if (live) {
    add("看屏幕", "", function () { openScreen(task.id, task.title); });
    add("捎话", "", function () { openMsg(task.id, task.title, item.draft); });
  }
  // S5③：工作树收口——等合并 / 清完主线重试 / failed·cancelled 有树都能处理
  function addKeep() {
    add("先留着", "", function () {
      banner("工作树和分支已保留，之后还能回来处理");  // 纯前端 no-op，不打 API
    });
  }
  function addDiscard() {
    add("丢弃", "danger", function () {
      if (!confirm("确定丢弃「" + task.title + "」的工作树？")) return;
      if (!confirm("再确认一次：会永久删除这棵工作树和 ns 分支，未合并内容无法从页面恢复。")) return;
      api("POST", "./api/tasks/" + task.id + "/discard")
        .then(function () { banner("已丢弃"); refreshTasks(); })
        .catch(function () {});
    });
  }
  if (MERGE_BUTTON_STATES.indexOf(state) >= 0 && hasTree) {
    add("合并进主线", "primary", function () {
      if (!confirm("把「" + task.title + "」的工作树分支合并进主线？（成功后自动清理工作树与分支）")) return;
      api("POST", "./api/tasks/" + task.id + "/merge")
        .then(function (data) { banner(data && data.note ? data.note : "已合并进主线"); refreshTasks(); })
        .catch(function () { refreshTasks(); });  // 主线脏/冲突：错误留在卡片红字，不显示假成功
    });
    addDiscard();
    addKeep();
  } else if (hasTree && ["failed", "cancelled", "chain_exhausted", "exited"].indexOf(state) >= 0) {
    addDiscard();
    addKeep();
  }
  if (ACTIVE_STATES.indexOf(state) >= 0 && live) {
    add("中止", status.stuck ? "danger solid" : "danger", function () {
      if (!confirm("往窗口按一下 Esc？它会停下当前这轮，等你看了屏幕再说。")) return;
      api("POST", "./api/tasks/" + task.id + "/interrupt")
        .then(function () { banner("已发出，看屏幕确认"); refreshTasks(); })
        .catch(function () {});
    });
    add("停后台", "", function () {
      if (!confirm("往窗口敲停后台指令？它会被要求立刻停掉后台任务和子 agent 并停下。")) return;
      api("POST", "./api/tasks/" + task.id + "/stop-background")
        .then(function () { banner("已发出，看屏幕确认"); refreshTasks(); })
        .catch(function () {});
    });
  }
  return box.childNodes.length ? box : null;
}

// 一个任务的几班（root_id 相同）合成一条链：卡片以最新一班为准，里面列各班状态
function groupChains(items) {
  var byRoot = {};
  items.forEach(function (item) {
    var root = item.task.root_id || item.task.id;
    (byRoot[root] = byRoot[root] || []).push(item);
  });
  return Object.keys(byRoot).map(function (root) {
    var shifts = byRoot[root].sort(function (a, b) { return (a.task.shift || 1) - (b.task.shift || 1); });
    return { root: root, shifts: shifts, latest: shifts[shifts.length - 1], first: shifts[0] };
  });
}

var DOW_LABELS = ["日", "一", "二", "三", "四", "五", "六"];
var UNSTARTED_STATES = ["scheduled", "postponed"];

// 哪些天"跑过任务"：按 run_at 分天，状态不在"还没起跑"里就算跑过
function computeDayMap(items) {
  var map = {};
  items.forEach(function (it) {
    if (UNSTARTED_STATES.indexOf(it.status.state) >= 0) return;
    var key = dayKeyFromIso(it.task.run_at);
    if (key) map[key] = true;
  });
  return map;
}

function renderCalendar() {
  var dayMap = computeDayMap(LAST_ITEMS);
  var y = CAL_MONTH.getFullYear(), m = CAL_MONTH.getMonth();
  $("cal-title").textContent = y + " 年 " + (m + 1) + " 月";

  var grid = $("cal-grid");
  grid.textContent = "";
  DOW_LABELS.forEach(function (label) {
    grid.appendChild(el("div", { class: "cal-dow", text: label }));
  });

  var firstDow = new Date(y, m, 1).getDay();
  var daysInMonth = new Date(y, m + 1, 0).getDate();
  var today = dayKey(new Date());
  for (var i = 0; i < firstDow; i++) {
    grid.appendChild(el("div", { class: "cal-day pad" }));
  }
  for (var d = 1; d <= daysInMonth; d++) {
    var key = dayKey(new Date(y, m, d));
    var hasTask = !!dayMap[key];
    var cls = "cal-day " + (hasTask ? "has-task" : "no-task");
    if (key === today) cls += " today";
    if (key === SELECTED_DAY) cls += " selected";
    var attrs = { type: "button", class: cls, text: String(d) };
    if (hasTask) {
      attrs.onclick = (function (dayKeyClosure) {
        return function () {
          SELECTED_DAY = SELECTED_DAY === dayKeyClosure ? null : dayKeyClosure;
          renderTasks(LAST_ITEMS);
        };
      })(key);
    } else {
      attrs.disabled = "disabled";
    }
    grid.appendChild(el("button", attrs));
  }
}

function renderTasks(items) {
  LAST_ITEMS = items;
  renderCalendar();
  var list = $("task-list");
  list.textContent = "";
  var hintBox = $("cal-filter-hint");
  if (SELECTED_DAY) {
    hintBox.hidden = false;
    hintBox.textContent = "";
    hintBox.appendChild(el("span", { text: "只看 " + SELECTED_DAY }));
    hintBox.appendChild(el("button", {
      type: "button", class: "ghost", text: "显示全部",
      onclick: function () { SELECTED_DAY = null; renderTasks(LAST_ITEMS); }
    }));
  } else {
    hintBox.hidden = true;
  }
  var shown = SELECTED_DAY
    ? items.filter(function (it) { return dayKeyFromIso(it.task.run_at) === SELECTED_DAY; })
    : items;
  if (!items.length) {
    list.appendChild(el("p", { class: "hint", text: "还没有任务。去「新建」页排一个夜班吧。" }));
    return;
  }
  if (!shown.length) {
    list.appendChild(el("p", { class: "hint", text: SELECTED_DAY + " 没有任务。" }));
    return;
  }
  var chains = groupChains(shown);
  chains.sort(function (a, b) {
    var ga = groupOf(a.latest.status.state), gb = groupOf(b.latest.status.state);
    if (ga !== gb) return ga - gb;
    // 终态组按 run_at 降序（最新在前）；其余组保持升序（早的先跑）
    if (ga === 3) return a.first.task.run_at > b.first.task.run_at ? -1 : 1;
    return a.first.task.run_at < b.first.task.run_at ? -1 : 1;
  });
  var now = Date.now();
  chains.forEach(function (chain) {
    list.appendChild(chainCard(chain, now));
  });
}

function chainCard(chain, now) {
  var ids = chain.shifts.map(function (it) { return it.task.id; });
  var card = taskCard(chain.latest, now, ids);
  if (chain.shifts.length > 1) {
    var row = el("div", { class: "shifts" });
    chain.shifts.forEach(function (it, i) {
      var st = it.status.state || "-";
      row.appendChild(el("span", { class: "shift-item" }, [
        el("span", { text: "第 " + (it.task.shift || i + 1) + " 班 " }),
        el("span", { class: "chip st-" + st, text: STATE_TEXT[st] || st })
      ]));
    });
    // 插在标题行之后
    card.insertBefore(row, card.children[1]);
  }
  return card;
}

function taskCard(item, now, chainIds) {
  var task = item.task, status = item.status;
  var state = status.state || "-";
  var runner = task.runner || "claude";
  var runnerLabel = runner === "codex" ? "Codex" : "Claude Code";
  var card = el("article", { class: "card" + (task.id === HIGHLIGHT_ID ? " flash" : "") });

  var headKids = [
    el("span", { class: "chip st-" + state, text: STATE_TEXT[state] || state }),
    el("span", { class: "chip runner-chip", text: "施工：" + runnerLabel + " · " + task.model }),
    el("span", { class: "task-title", text: task.title }),
    el("span", { class: "task-id", text: task.id })
  ];
  if (status.stuck) {  // S4①：疑似卡住——黄色徽章 + 中止按钮高亮（见 taskActions）
    var mins = status.stuck_since ?
      Math.max(0, Math.round((now - new Date(status.stuck_since)) / 60000)) : 0;
    headKids.push(el("span", { class: "chip stuck-chip", text: "疑似卡住 " + mins + " 分钟" }));
  }
  card.appendChild(el("div", { class: "task-head" }, headKids));
  var metaText = "项目 " + task.project + " · 模型 " + task.model + " · 档位 " + task.effort +
    (chainIds && chainIds.length > 1 ? " · 共 " + chainIds.length + " 班" : "");
  // 有 thread_id 只展示短后缀，不把本机 rollout 路径/完整会话 id 露给网页
  if (runner === "codex" && status.thread_id) {
    metaText += " · 会话 …" + String(status.thread_id).slice(-8);
  }
  card.appendChild(el("div", { class: "task-meta", text: metaText }));

  // S6⑤：Codex 额度到线等刷新的具体时间点；waiting_wakeup 对 Claude 走
  // ScheduleWakeup（会话自己缓存闹钟），不落 quota_paused_until，这条只对
  // 设了这个字段的 Codex 任务显示
  if (state === "waiting_wakeup" && status.quota_paused_until) {
    card.appendChild(el("div", { class: "quota-line",
      text: "等 Codex 额度刷新，" + fmtLocal(status.quota_paused_until) + " 自动叫醒" }));
  }

  // S6⑤：F12 后台任务摘要——运行中 / 已完成待读取的数量
  if (item.background_summary) {
    var bs = item.background_summary;
    var bgParts = [];
    if (bs.running) bgParts.push(bs.running + " 个运行中");
    if (bs.finished_pending) bgParts.push(bs.finished_pending + " 个已完成待读取");
    if (bgParts.length) {
      card.appendChild(el("div", { class: "quota-line", text: "后台任务：" + bgParts.join("，") }));
    }
  }

  // S5③：工作树任务的分支与施工目录（分支可点复制）
  if (status.worktree_path || status.branch) {
    var branchText = status.branch || "-";
    card.appendChild(el("div", { class: "task-wt" }, [
      el("span", {
        class: "wt-branch", text: "🌿 " + branchText, title: "点击复制分支名",
        onclick: function () {
          copyText(branchText).then(
            function () { banner("分支名已复制：" + branchText); },
            function () { banner("复制失败，分支名：" + branchText); }
          );
        }
      }),
      el("span", { class: "wt-path", text: status.worktree_path || "" })
    ]));
  }

  // 计划时间与倒计时 / 等前置 / 已跑时长
  var whenText;
  if (state === "postponed" && status.next_attempt_at) {
    var due = new Date(status.next_attempt_at) - now;
    whenText = "下次尝试 " + fmtLocal(status.next_attempt_at) + "（" +
      (due > 0 ? "还有 " + fmtDelta(due) : "已到点，等下一轮调度") + "）";
  } else if (item.trigger_text && item.trigger_text !== "按时间") {
    whenText = item.trigger_text;  // after 任务：不按时间起跑，没有"计划/还有"
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
    if (typeof q.session_pct === "number") parts.push("五小时剩 " + (100 - q.session_pct) + "%");
    if (typeof q.week_all_pct === "number") parts.push("七日剩 " + (100 - q.week_all_pct) + "%");
    Object.keys(q.per_model || {}).forEach(function (name) {
      parts.push(name + " 剩 " + (100 - q.per_model[name]) + "%");
    });
    if (parts.length) {
      card.appendChild(el("div", { class: "quota-line",
        text: "起跑时额度：" + parts.join(" · ") }));
    }
  }

  if (status.last_message) {
    var msg = el("div", {
      class: "lastmsg" + (UI_OPEN["msg:" + task.id] ? "" : " clamp"), text: status.last_message,
      onclick: function () {
        msg.classList.toggle("clamp");
        UI_OPEN["msg:" + task.id] = !msg.classList.contains("clamp");  // 5 秒重画时保持展开
      }
    });
    card.appendChild(msg);
  }
  var reason = status.postpone_reason || status.error;
  if (reason) card.appendChild(el("p", { class: "warn-reason", text: reason }));

  if (item.draft) {  // S4②：有一条捎话草稿待发，点它打开弹层
    var preview = item.draft.replace(/\s+/g, " ").trim().slice(0, 40);
    card.appendChild(el("div", {
      class: "draft-line",
      text: "📝 有一条待发的话：" + preview,
      onclick: function () { openMsg(task.id, task.title, item.draft); }
    }));
  }

  if (item.events_tail && item.events_tail.length) {
    var pre = el("pre", { text: item.events_tail.join("\n") });
    var det = el("details", { class: "events" }, [
      el("summary", { text: "最近事件（" + item.events_tail.length + " 条）" }),
      pre
    ]);
    if (UI_OPEN["ev:" + task.id]) det.open = true;
    det.addEventListener("toggle", function () { UI_OPEN["ev:" + task.id] = det.open; });
    card.appendChild(det);
  }

  var actions = taskActions(item, chainIds);
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

function modelLimit(model, runner) {
  runner = runner || "claude";
  if (CFG && CFG.runners && CFG.runners[runner]) {
    var rmodels = CFG.runners[runner].models || {};
    if (rmodels[model]) return rmodels[model].context_limit;
  }
  // 旧后端没有 CFG.runners 时退回顶层 Claude 兼容表
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
  var limit = modelLimit(currentModel(), currentRunner());
  // S6.1 C2：Codex 模型没有稳定水位来源（limit 是 null）时清空这个字段，
  // 不能让切换 runner 前自动带出来的 Claude token 数留在表单里——那个数字
  // 对 Codex 任务永远不会生效，暗带着容易让人以为已经设了警戒线。
  $("f-warntokens").value = (ratio && limit) ? Math.round(ratio * limit) : "";
}

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = setTimeout(function () {
    if (PROMPT_EDITED) return;
    if (EDIT_TASK && EDIT_TASK.active) return;  // 这一班的提示词改不了，不预览
    var title = $("f-title").value;
    var project = $("f-project").value;
    var model = currentModel();
    var text = $("f-text").value;
    if (!project || !text) return;
    api("POST", "./api/preview", { title: title, project: project, model: model,
      task_text: text, worktree: $("f-worktree").checked })
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

/* ---------- S5③：工作树开关与完工后选择 ---------- */

function syncWorktreeUI() {
  var on = $("f-worktree").checked;
  $("f-worktree-off-hint").hidden = on;
  $("merge-row").hidden = !on;
  $("f-mergepolicy").disabled = !on || !!(EDIT_TASK && EDIT_TASK.active);
  schedulePreview();
}

// 表单 → {worktree, review}；S5 只接受 review.enabled=false（审稿 S7 才有）
function worktreeFromForm() {
  return {
    worktree: $("f-worktree").checked,
    review: { enabled: false, merge_policy: $("f-mergepolicy").value || "manual" }
  };
}

/* ---------- 编辑模式与触发方式（S4③） ---------- */

function toLocalInput(d) {
  var p = function (n) { return (n < 10 ? "0" : "") + n; };
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    "T" + p(d.getHours()) + ":" + p(d.getMinutes());
}

function setTriggerMode(mode) {
  var after = mode === "after";
  document.querySelector('input[name="f-trigger"][value="time"]').checked = !after;
  document.querySelector('input[name="f-trigger"][value="after"]').checked = after;
  $("after-box").hidden = !after;
  // after 模式不看时间；活跃编辑本来就不让改时间
  $("time-box").hidden = after || !!(EDIT_TASK && EDIT_TASK.active);
  $("f-runat").required = !after && !(EDIT_TASK && EDIT_TASK.active);
}

function applyEditMode() {
  var editing = !!EDIT_TASK;
  var active = editing && EDIT_TASK.active;
  $("form-title").textContent = editing ? "编辑任务" : "新建任务";
  $("new-submit").textContent = editing ? "保存修改" : "建任务";
  $("edit-hint").hidden = !active;
  $("only-create").hidden = active;
  $("prompt-box").hidden = active;
  $("f-prompt").required = !active;
  // S5③：活跃编辑不许改工作树开关（这一班已经在某个目录里干了）
  $("f-worktree").disabled = active;
  setTriggerMode(document.querySelector('input[name="f-trigger"]:checked').value);
  syncWorktreeUI();
}

function enterCreate() {
  EDIT_TASK = null;
  applyEditMode();
  setTriggerMode("time");
  unmarkPromptEdited();
  WARN_EDITED = false;
  document.querySelector('input[name="f-runner"][value="claude"]').checked = true;
  populateModelEffortForRunner("claude");
  $("f-worktree").checked = true;   // 新建默认建树
  $("f-mergepolicy").value = "manual";
  syncWorktreeUI();
  showView("new");
}

function enterEdit(item) {
  var task = item.task, status = item.status, guards = task.guards || {};
  EDIT_TASK = { id: task.id, item: item, active: ACTIVE_STATES.indexOf(status.state) >= 0 };
  $("f-title").value = task.title || "";
  if (task.project) $("f-project").value = task.project;
  var runner = task.runner || "claude";
  var runnerRadio = document.querySelector('input[name="f-runner"][value="' + runner + '"]');
  if (runnerRadio) runnerRadio.checked = true;
  populateModelEffortForRunner(runner);
  var rc = runnerModelsEfforts(runner);
  var modelSel = $("f-model");
  if (rc.models && rc.models[task.model]) {
    modelSel.value = task.model;
    $("f-model-custom").hidden = true;
  } else {
    modelSel.value = "__custom__";
    $("f-model-custom").hidden = false;
    $("f-model-custom").value = task.model || "";
  }
  if ((rc.efforts || []).indexOf(task.effort) >= 0) $("f-effort").value = task.effort;

  var trigger = task.trigger || { type: "time" };
  if (trigger.type === "after") $("f-after-when").value = trigger.when || "finished";
  setTriggerMode(trigger.type === "after" ? "after" : "time");

  var runat = task.run_at ? new Date(task.run_at) : null;
  $("f-runat").value = runat && !isNaN(runat) ? toLocalInput(runat) : "";
  $("f-text").value = task.task_text || "";

  // S5③：编辑旧任务缺 worktree 字段必须显示关，不能因新建默认而误翻成开
  $("f-worktree").checked = task.worktree === true;
  $("f-mergepolicy").value = (task.review && task.review.merge_policy) || "manual";

  $("f-warntokens").value = guards.context_warn_tokens != null ? guards.context_warn_tokens : "";
  $("f-warntext").value = guards.context_warn_text != null ?
    guards.context_warn_text : (CFG.context_warn_text || "");
  WARN_EDITED = guards.context_warn_tokens != null;
  $("f-sessionleft").value = typeof guards.session_pct_max === "number" ? 100 - guards.session_pct_max : "";
  $("f-weekleft").value = typeof guards.weekly_pct_max === "number" ? 100 - guards.weekly_pct_max : "";
  $("f-modelleft").value = typeof guards.model_weekly_pct_max === "number" ? 100 - guards.model_weekly_pct_max : "";
  $("f-autointerrupt").value = guards.auto_interrupt_minutes != null ? guards.auto_interrupt_minutes : "";
  $("f-chainmax").value = (task.chain && typeof task.chain.max_windows === "number") ? task.chain.max_windows : "";
  $("f-nohandover").value = (task.chain && task.chain.on_no_handover) ||
    (CFG.chain && CFG.chain.on_no_handover) || "continue";

  unmarkPromptEdited();
  $("f-prompt").value = task.prompt_final || "";
  applyEditMode();
  showView("new");  // 里面会刷新前置任务下拉（并保持当前选中）
}

// 前置任务下拉：按链只列根任务，显示"标题（id 后 4 位）"
function refreshTriggerChoices() {
  api("GET", "./api/tasks").then(function (items) {
    var sel = $("f-after-task");
    var want = EDIT_TASK ?
      String((EDIT_TASK.item.task.trigger || {}).task || "") : "";
    sel.textContent = "";
    groupChains(items || []).forEach(function (chain) {
      var rep = chain.first.task;  // 展示用：链根任务的标题
      sel.appendChild(el("option", {
        value: chain.root,
        text: rep.title + "（" + chain.root.slice(-4) + "）"
      }));
    });
    var found = Array.prototype.some.call(sel.options, function (o) { return o.value === want; });
    if (want && !found)  // 当前前置不在列表里（比如不是根任务），原样列出来免得静默丢
      sel.appendChild(el("option", { value: want, text: "（id " + want.slice(-4) + "）" }));
    if (want) sel.value = want;
  }).catch(function () { /* 列表拉不到就不填，提交时会校验 */ });
}

// 表单 → guards：编辑时在任务已有 guards 上覆盖（keepalive 等表单外键不动）
function guardsFromForm(base) {
  var guards = base ? Object.assign({}, base) : {};
  var cfgText = CFG.context_warn_text || "";
  var warn = $("f-warntokens").value;
  if (warn !== "") guards.context_warn_tokens = Number(warn);
  else delete guards.context_warn_tokens;
  var wtext = $("f-warntext").value;
  if (wtext !== cfgText) guards.context_warn_text = wtext;
  else delete guards.context_warn_text;
  var sl = $("f-sessionleft").value;
  if (sl !== "") guards.session_pct_max = 100 - Number(sl);
  else if (base) delete guards.session_pct_max;
  var wl = $("f-weekleft").value;
  if (wl !== "") guards.weekly_pct_max = 100 - Number(wl);
  else if (base) delete guards.weekly_pct_max;
  var ml = $("f-modelleft").value;
  if (ml !== "") guards.model_weekly_pct_max = 100 - Number(ml);
  else if (base) delete guards.model_weekly_pct_max;
  var ai = $("f-autointerrupt").value;
  if (ai !== "") guards.auto_interrupt_minutes = Number(ai);
  else delete guards.auto_interrupt_minutes;
  return guards;
}

function chainFromForm(base) {
  var chain = base ? Object.assign({}, base) : {};
  var max = $("f-chainmax").value;
  if (max !== "") chain.max_windows = Number(max);
  else if (base) delete chain.max_windows;
  var nh = $("f-nohandover").value;
  var cfgNh = (CFG.chain && CFG.chain.on_no_handover) || "continue";
  if (nh !== cfgNh) chain.on_no_handover = nh;
  else if (base) delete chain.on_no_handover;
  return chain;
}

/* ---------- 捎话弹层（S4③） ---------- */

function openMsg(id, title, draft) {
  MSG_TASK = { id: id, title: title };
  $("msg-title").textContent = "捎话 · " + title;
  $("msg-text").value = draft || "";
  $("btn-msg-delete").hidden = !draft;
  $("msg-overlay").classList.add("show");
  $("msg-text").focus();
}

function closeMsg() {
  MSG_TASK = null;
  $("msg-overlay").classList.remove("show");
  refreshTasks();  // 草稿行/按钮状态以服务器为准
}

function sendMessage() {
  if (!MSG_TASK) return;
  var text = $("msg-text").value;
  if (!text.trim()) { banner("先写点什么再发"); return; }
  api("POST", "./api/tasks/" + MSG_TASK.id + "/message", { text: text, send: true })
    .then(function () { closeMsg(); banner("已发出，看屏幕确认"); })
    .catch(function () {});
}

function saveDraft() {
  if (!MSG_TASK) return;
  var text = $("msg-text").value;
  if (!text.trim()) { banner("先写点什么再存"); return; }
  api("POST", "./api/tasks/" + MSG_TASK.id + "/message", { text: text, send: false })
    .then(function () { closeMsg(); banner("草稿已存，还没发"); })
    .catch(function () {});
}

function deleteDraft() {
  if (!MSG_TASK) return;
  api("DELETE", "./api/tasks/" + MSG_TASK.id + "/message")
    .then(function () { closeMsg(); banner("草稿已删"); })
    .catch(function () {});
}

// S6⑤：runner 决定这次能选哪些模型/档位——CFG.runners 是 commit①/S6 新键，
// 旧后端没有这个键时只有 claude 能用（退回旧顶层 CFG.models/CFG.efforts）。
function runnerModelsEfforts(runner) {
  if (CFG.runners && CFG.runners[runner]) return CFG.runners[runner];
  return runner === "claude" ?
    { models: CFG.models || {}, efforts: CFG.efforts || [] } :
    { models: {}, efforts: [] };
}

function currentRunner() {
  var r = document.querySelector('input[name="f-runner"]:checked');
  return r ? r.value : "claude";
}

function populateModelEffortForRunner(runner) {
  var rc = runnerModelsEfforts(runner);
  var model = $("f-model");
  model.textContent = "";
  Object.keys(rc.models || {}).forEach(function (name) {
    model.appendChild(el("option", { value: name, text: name }));
  });
  model.appendChild(el("option", { value: "__custom__", text: "自定义…" }));
  var effort = $("f-effort");
  effort.textContent = "";
  (rc.efforts || []).forEach(function (name) {
    effort.appendChild(el("option", { value: name, text: name }));
  });
  if ((rc.efforts || []).indexOf("high") >= 0) effort.value = "high";
}

function populateNewForm() {
  var project = $("f-project");
  project.textContent = "";
  Object.keys(CFG.projects).forEach(function (name) {
    project.appendChild(el("option", { value: name, text: name }));
  });
  populateModelEffortForRunner(currentRunner());
  $("f-warntext").value = CFG.context_warn_text || "";
  if (CFG.guards) {
    if (typeof CFG.guards.session_pct_max === "number") $("f-sessionleft").value = 100 - CFG.guards.session_pct_max;
    if (typeof CFG.guards.weekly_pct_max === "number") $("f-weekleft").value = 100 - CFG.guards.weekly_pct_max;
    var mw = typeof CFG.guards.model_weekly_pct_max === "number" ? CFG.guards.model_weekly_pct_max : CFG.guards.weekly_pct_max;
    if (typeof mw === "number") $("f-modelleft").value = 100 - mw;
  }
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
  var editing = !!EDIT_TASK;
  var active = editing && EDIT_TASK.active;
  var title = $("f-title").value.trim();
  var project = $("f-project").value;
  var model = currentModel();
  var effort = $("f-effort").value;
  var runAtRaw = $("f-runat").value;
  var text = $("f-text").value;
  var prompt = $("f-prompt").value;

  if (!title) { errBox.textContent = "标题不能为空"; return; }
  if (!active) {
    if (!project) { errBox.textContent = "项目不能为空"; return; }
    if (!model) { errBox.textContent = "模型不能为空（选一个或手输）"; return; }
  }
  if (!text.trim()) { errBox.textContent = "任务内容不能为空"; return; }

  // 触发方式：after 模式不发 run_at（run_at 只是排序用）
  var mode = document.querySelector('input[name="f-trigger"]:checked').value;
  var trigger = null;
  var runAtIso;
  if (mode === "after") {
    var pre = $("f-after-task").value;
    if (!pre) { errBox.textContent = "先选一个前置任务"; return; }
    trigger = { type: "after", task: pre, when: $("f-after-when").value };
  } else if (!active) {
    if (!runAtRaw) { errBox.textContent = "开跑时间不能为空——没有默认时间，请自己选"; return; }
    try { runAtIso = new Date(runAtRaw).toISOString(); }
    catch (e) { errBox.textContent = "开跑时间认不出来"; return; }
  }

  var guards = guardsFromForm(editing ? (EDIT_TASK.item.task.guards || {}) : null);
  var chain = chainFromForm(editing ? (EDIT_TASK.item.task.chain || {}) : null);
  var wt = worktreeFromForm();  // S5③：worktree + review 占位形状

  var body;
  if (active) {
    // 这一班正在跑：服务器只认这几个键，其他发了也是白发（工作树不许改）
    body = { title: title, task_text: text, guards: guards, chain: chain,
             trigger: trigger || { type: "time" } };
  } else {
    body = {
      title: title, project: project, runner: currentRunner(), model: model, effort: effort,
      task_text: text, prompt_final: prompt, guards: guards, chain: chain,
      trigger: trigger || { type: "time" },
      worktree: wt.worktree, review: wt.review
    };
    if (mode !== "after") body.run_at = runAtIso;  // after 模式不发
  }

  var req = editing ?
    api("PUT", "./api/tasks/" + EDIT_TASK.id, body) :
    api("POST", "./api/tasks", body);
  req.then(function (data) {
    if (editing) HIGHLIGHT_ID = EDIT_TASK.id;
    else HIGHLIGHT_ID = data.id;
    EDIT_TASK = null;
    showView("tasks");
    banner(editing ? "已保存修改" : "任务已建：" + HIGHLIGHT_ID);
  }).catch(function () { /* 错误已显示 */ });
}

/* ---------- 模板页 ---------- */

function loadTemplatesView() {
  api("GET", "./api/config").then(function (cfg) {
    CFG = cfg;
    $("t-prompt").value = cfg.prompt_template || "";
    $("t-warntext").value = cfg.context_warn_text || "";
    $("t-quotapause").value = cfg.quota_pause_text || "";
    $("t-quotawrap").value = cfg.quota_wrapup_text || "";
    $("t-quotaother").value = cfg.quota_other_model_text || "";
    $("t-chain").value = cfg.chain_template || "";
    $("t-stopbg").value = cfg.stop_background_text || "";
    $("t-stuckinterrupt").value = cfg.stuck_interrupt_text || "";
    $("t-review").value = cfg.review_template || "";
    $("t-reviewfix").value = cfg.review_fix_template || "";
    $("t-reviewcriteria").value = cfg.review_criteria_text || "";
    $("t-reviewwrapup").value = cfg.review_wrapup_text || "";
    $("t-reviewstopbuild").value = cfg.review_stop_build_text || "";
    $("t-hold").value = cfg.hold_text || "";
    $("t-resume").value = cfg.resume_text || "";
  }).catch(function () {});
}

function saveTemplates() {
  var note = $("tpl-note");
  note.textContent = "";
  api("PUT", "./api/templates", {
    prompt_template: $("t-prompt").value,
    context_warn_text: $("t-warntext").value,
    quota_pause_text: $("t-quotapause").value,
    quota_wrapup_text: $("t-quotawrap").value,
    quota_other_model_text: $("t-quotaother").value,
    chain_template: $("t-chain").value,
    stop_background_text: $("t-stopbg").value,
    stuck_interrupt_text: $("t-stuckinterrupt").value,
    review_template: $("t-review").value,
    review_fix_template: $("t-reviewfix").value,
    review_criteria_text: $("t-reviewcriteria").value,
    review_wrapup_text: $("t-reviewwrapup").value,
    review_stop_build_text: $("t-reviewstopbuild").value,
    hold_text: $("t-hold").value,
    resume_text: $("t-resume").value
  }).then(function () {
    note.textContent = "已保存";
    setTimeout(function () { note.textContent = ""; }, 3000);
  }).catch(function () { /* banner 已提示 */ });
}

/* ---------- 启动 ---------- */

function start() {
  $("tab-tasks").addEventListener("click", function () { showView("tasks"); });
  $("tab-new").addEventListener("click", enterCreate);
  $("tab-tpl").addEventListener("click", function () { showView("tpl"); });
  // 触发方式单选：切显示"按时间/等前置"
  Array.prototype.forEach.call(
    document.querySelectorAll('input[name="f-trigger"]'),
    function (radio) {
      radio.addEventListener("change", function () { setTriggerMode(radio.value); });
    }
  );
  // 捎话弹层
  $("btn-msg-close").addEventListener("click", closeMsg);
  $("btn-msg-send").addEventListener("click", sendMessage);
  $("btn-msg-save").addEventListener("click", saveDraft);
  $("btn-msg-delete").addEventListener("click", deleteDraft);
  $("btn-logout").addEventListener("click", function () {
    if (!confirm("退出登录？之后打开夜班页要重新输口令。要回主站首页请用左上角的链接。")) return;
    api("POST", "./api/logout").catch(function () {}).then(function () {
      location.href = "./login.html";
    });
  });
  $("btn-screen-close").addEventListener("click", closeScreen);
  // 一改就存：勾选/时刻变化立刻 PUT，不给轮询插队的机会
  $("w-enabled").addEventListener("change", function () { WARMUP_DIRTY = true; saveWarmup(); });
  $("btn-warmup-add").addEventListener("click", function () {
    WARMUP_DIRTY = true;  // 新行还没选时间，别让轮询把空行冲掉
    addWarmupRow("").focus();
  });
  $("btn-refresh").addEventListener("click", function () {
    refreshTasks(); refreshQuota(); banner("已刷新");
  });
  $("cal-toggle").addEventListener("click", function () {
    CAL_OPEN = !CAL_OPEN;
    $("cal-panel").hidden = !CAL_OPEN;
    $("cal-toggle").textContent = CAL_OPEN ? "收起日期" : "展开日期";
  });
  $("cal-prev").addEventListener("click", function () {
    CAL_MONTH = new Date(CAL_MONTH.getFullYear(), CAL_MONTH.getMonth() - 1, 1);
    renderCalendar();
  });
  $("cal-next").addEventListener("click", function () {
    CAL_MONTH = new Date(CAL_MONTH.getFullYear(), CAL_MONTH.getMonth() + 1, 1);
    renderCalendar();
  });
  // 原生日期输入：手机上是滚轮选年月日，一步跳到任意天（不限于有任务的天）
  $("cal-jump").addEventListener("change", function (e) {
    var v = e.target.value;  // "YYYY-MM-DD"
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v || "");
    if (!m) return;
    CAL_MONTH = new Date(Number(m[1]), Number(m[2]) - 1, 1);
    SELECTED_DAY = v;
    renderTasks(LAST_ITEMS);
  });
  $("btn-requery").addEventListener("click", function () {
    var btn = $("btn-requery");
    btn.disabled = true; btn.textContent = "查询中…（约 10 秒）";
    api("POST", "./api/quota/refresh").then(function (data) {
      renderQuota(data || {}); banner("额度已重新查询");
    }).catch(function () {}).then(function () {
      btn.disabled = false; btn.textContent = "都刷新";
    });
  });
  // 手机切回来（WebView 的 visibilitychange 不一定可靠）也拉一次
  window.addEventListener("focus", function () {
    if (currentView === "tasks") { refreshTasks(); refreshQuota(); }
  });
  window.addEventListener("pageshow", function () {
    if (currentView === "tasks") { refreshTasks(); refreshQuota(); }
  });

  $("new-form").addEventListener("submit", submitNewForm);
  $("f-worktree").addEventListener("change", syncWorktreeUI);
  $("f-title").addEventListener("input", schedulePreview);
  $("f-project").addEventListener("change", schedulePreview);
  Array.prototype.forEach.call(document.querySelectorAll('input[name="f-runner"]'), function (r) {
    r.addEventListener("change", function () {
      populateModelEffortForRunner(currentRunner());
      recalcWarnDefault();
      schedulePreview();
    });
  });
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
    if (cfg.home_link && cfg.home_link.href) {
      var link = $("link-home");
      link.textContent = cfg.home_link.text || "← 主站";
      link.setAttribute("href", cfg.home_link.href);
      link.hidden = false;
    }
    populateNewForm();
    renderWarmup(cfg);
    showView("tasks");
  }).catch(function () {
    // 401 已跳登录；其余情况仍试着渲染空界面
    showView("tasks");
  });
}

document.addEventListener("DOMContentLoaded", start);
