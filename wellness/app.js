/* 日日有益前端：只用原生 DOM 与同源 JSON API。 */
(function () {
  "use strict";

  var API = "./api";
  var state = { date: localDate(), profile: {}, preferences: {}, entries: [], summary: {} };
  var $ = function (id) { return document.getElementById(id); };
  var text = function (node, value) { node.textContent = value == null ? "" : String(value); };
  var value = function (id) { return $(id).value.trim(); };
  var numberOrNull = function (id) { var raw = value(id); return raw === "" ? null : Number(raw); };

  function localDate() {
    var d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  function humanDate(date) {
    var parts = date.split("-");
    return parts.length === 3 ? Number(parts[1]) + "月" + Number(parts[2]) + "日" : date;
  }
  function endpoint(path) { return API + path; }
  function api(method, path, body) {
    var options = { method: method, headers: { Accept: "application/json" } };
    if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    return fetch(endpoint(path), options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok || payload.ok === false) {
          var err = new Error((payload.error && payload.error.message) || "请求没有完成");
          err.payload = payload; throw err;
        }
        return payload.data === undefined ? payload : payload.data;
      });
    });
  }
  function setMessage(message, isError) {
    text($("global-message"), message || "");
    $("global-message").className = isError ? "message error" : "message";
  }
  function clearErrors() { ["entry-error", "profile-error", "preferences-error", "plan-error"].forEach(function (id) { text($(id), ""); }); }
  function asObject(data) { return data && typeof data === "object" ? data : {}; }
  function listFrom(data, key) { if (Array.isArray(data)) return data; if (data && Array.isArray(data[key])) return data[key]; return []; }
  function calories(entry) { return Number(entry.calories || entry.kcal || entry.calories_kcal || 0); }
  function exerciseCalories(entry) {
    var raw = entry.calories_burned != null ? entry.calories_burned : (entry.burned_kcal != null ? entry.burned_kcal : (entry.exercise_kcal != null ? entry.exercise_kcal : entry.calories));
    return raw == null || raw === "" ? null : Number(raw);
  }
  function kindOf(entry) { return entry.type || entry.kind || entry.entry_type || "meal"; }
  function displayNumber(num) { return Number.isFinite(Number(num)) ? String(Math.round(Number(num) * 10) / 10) : "未记录"; }

  function loadAll() {
    clearErrors(); setMessage("正在读取今天的数据…");
    return Promise.all([
      api("GET", "/profile"), api("GET", "/preferences"),
      api("GET", "/days/" + state.date + "/entries"), api("GET", "/days/" + state.date + "/summary")
    ]).then(function (results) {
      state.profile = asObject(results[0]); state.preferences = asObject(results[1]);
      state.entries = listFrom(results[2], "entries"); state.summary = asObject(results[3]);
      renderProfile(); renderPreferences(); renderEntries(); renderSummary(); setMessage("");
    }).catch(function (error) {
      setMessage(error.message || "暂时无法读取数据，请稍后再试。", true);
      renderEntries();
    });
  }

  function renderSummary() {
    var data = state.summary;
    var intake = data.intake_kcal != null ? data.intake_kcal : (data.calories_in != null ? data.calories_in : (data.consumed_kcal != null ? data.consumed_kcal : data.calories));
    var exercise = data.exercise_kcal != null ? data.exercise_kcal : data.exercise;
    var weight = data.weight_kg != null ? data.weight_kg : data.weight;
    var budget = data.budget_kcal != null ? data.budget_kcal : data.target_kcal;
    var remaining = data.remaining_kcal != null ? data.remaining_kcal : (data.remaining_calories != null ? data.remaining_calories : (budget != null && intake != null ? Number(budget) - Number(intake) : null));
    text($("summary-calories-label"), intake == null ? "未记录" : displayNumber(intake) + " 千卡");
    text($("summary-exercise"), exercise == null ? "未记录" : displayNumber(exercise));
    text($("summary-weight"), weight == null ? "未记录" : displayNumber(weight));
    text($("summary-remaining"), remaining == null ? "未记录" : displayNumber(remaining));
    text($("summary-calories-note"), budget == null ? "填写档案后会显示每日预算。" : "每日预算约 " + displayNumber(budget) + " 千卡（估算）");
    var progress = budget && intake != null ? Math.max(0, Math.min(100, Number(intake) / Number(budget) * 100)) : 0;
    $("calorie-progress").style.width = progress + "%";
  }

  function renderEntries() {
    var box = $("entries-list"); box.textContent = "";
    text($("entry-count"), state.entries.length ? state.entries.length + " 条" : "");
    if (!state.entries.length) { var empty = document.createElement("p"); empty.className = "empty muted"; text(empty, "今天还没有记录，先从一件小事开始吧。"); box.appendChild(empty); return; }
    state.entries.forEach(function (entry) {
      var row = document.createElement("div"); row.className = "entry";
      var details = document.createElement("div");
      var title = document.createElement("div"); title.className = "entry-title";
      var kind = kindOf(entry); text(title, kind === "exercise" ? "运动" : kind === "weight" ? "体重" : (entry.meal_type_label || entry.meal_type || "饮食"));
      var meta = document.createElement("div"); meta.className = "entry-meta";
      var description = entry.text || entry.name || entry.description || entry.note || "手动记录";
      if (kind === "weight") description = displayNumber(entry.weight_kg != null ? entry.weight_kg : entry.weight) + " 公斤";
      text(meta, description + (entry.created_at ? " · " + String(entry.created_at).slice(11, 16) : "")); details.appendChild(title); details.appendChild(meta);
      var right = document.createElement("div");
      if (kind === "weight") { text(right, ""); } else { right.className = "entry-value"; if (kind === "exercise") { var burned = exerciseCalories(entry); text(right, burned == null || !Number.isFinite(burned) ? "未填写消耗" : displayNumber(burned) + " 千卡"); } else { text(right, calories(entry) ? displayNumber(calories(entry)) + " 千卡" : "未填写热量"); } }
      var remove = document.createElement("button"); remove.type = "button"; remove.className = "delete-entry"; text(remove, "删除"); remove.setAttribute("aria-label", "删除" + description); remove.addEventListener("click", function () { deleteEntry(entry); });
      right.appendChild(remove); row.appendChild(details); row.appendChild(right); box.appendChild(row);
    });
  }
  function deleteEntry(entry) {
    var id = entry.id || entry.uuid; if (!id) return;
    api("DELETE", "/days/" + state.date + "/entries/" + encodeURIComponent(id)).then(function () { setMessage("记录已删除"); return loadAll(); }).catch(function (error) { setMessage(error.message, true); });
  }

  function renderProfile() {
    var p = state.profile.profile || state.profile;
    [["birth-year", "birth_year"], ["height", "height_cm"], ["profile-weight", "weight_kg"], ["target-weight", "target_weight_kg"], ["deficit-kcal", "deficit_kcal"]].forEach(function (pair) { if (p[pair[1]] != null) $(pair[0]).value = p[pair[1]]; });
    if (p.sex != null) $("profile-sex").value = p.sex; if (p.activity_level != null) $("activity").value = p.activity_level;
    var calc = state.profile.calculation || state.profile.estimate || state.profile.goal;
    if (calc) {
      var details = [];
      if (calc.bmr != null) details.push("基础代谢约 " + displayNumber(calc.bmr) + " 千卡");
      if (calc.tdee != null) details.push("每日消耗约 " + displayNumber(calc.tdee) + " 千卡");
      var target = calc.target_kcal != null ? calc.target_kcal : (calc.target_calories != null ? calc.target_calories : calc.calorie_target);
      if (target != null) details.push("建议摄入约 " + displayNumber(target) + " 千卡");
      if (calc.safe_to_cut === false) details.push("当前先以维护热量为主，请咨询专业人士");
      if (Array.isArray(calc.warnings)) details = details.concat(calc.warnings);
      text($("profile-calculation"), details.join(" · "));
    } else { text($("profile-calculation"), ""); }
  }
  function renderPreferences() {
    var p = state.preferences.preferences || state.preferences; var categories = p.categories || p;
    if (p.items && typeof p.items === "object") {
      categories = { avoid: [], reluctant: [], neutral: [], love: [] };
      Object.keys(p.items).forEach(function (category) { var foods = p.items[category]; if (!foods || typeof foods !== "object") return; Object.keys(foods).forEach(function (food) { if (categories[foods[food]]) categories[foods[food]].push(food); }); });
    }
    ["avoid", "reluctant", "neutral", "love"].forEach(function (key) {
      var items = categories[key] || []; if (typeof items === "string") items = items.split(/[，,、\n]/);
      $("pref-" + key).value = Array.isArray(items) ? items.join("、") : "";
    });
  }
  function splitItems(id) { return value(id).split(/[，,、\n]/).map(function (item) { return item.trim(); }).filter(Boolean).slice(0, 100); }

  function submitEntry(event) {
    event.preventDefault(); text($("entry-error"), ""); var kind = value("entry-type"); var body = { type: kind };
    if (kind === "meal") { body.name = value("meal-text"); body.meal_type = value("meal-kind"); body.calories = numberOrNull("meal-calories"); if (!body.name) { text($("entry-error"), "请写下吃了什么。"); $("meal-text").focus(); return; } }
    if (kind === "exercise") { body.name = value("exercise-text"); body.calories_burned = numberOrNull("exercise-calories"); if (!body.name) { text($("entry-error"), "请写下做了什么运动。"); $("exercise-text").focus(); return; } }
    if (kind === "weight") { body.weight_kg = numberOrNull("weight-value"); if (!(body.weight_kg > 0)) { text($("entry-error"), "请输入有效的体重。"); $("weight-value").focus(); return; } }
    api("POST", "/days/" + state.date + "/entries", body).then(function () { $("entry-form").reset(); toggleEntryFields(); setMessage("记录好了"); return loadAll(); }).catch(function (error) { text($("entry-error"), error.message); });
  }
  function toggleEntryFields() { var kind = value("entry-type"); $("meal-fields").hidden = kind !== "meal"; $("exercise-fields").hidden = kind !== "exercise"; $("weight-fields").hidden = kind !== "weight"; }

  function submitProfile(event) {
    event.preventDefault(); text($("profile-error"), ""); var body = { birth_year:numberOrNull("birth-year"), height_cm:numberOrNull("height"), weight_kg:numberOrNull("profile-weight"), target_weight_kg:numberOrNull("target-weight"), activity_level:value("activity") };
    var sex = value("profile-sex"); var deficit = numberOrNull("deficit-kcal"); if (sex) body.sex = sex; if (deficit != null) body.deficit_kcal = deficit;
    if (!(body.height_cm > 0) || !(body.weight_kg > 0) || !body.birth_year || !sex) { text($("profile-error"), "请填写出生年份、性别、身高和当前体重。"); return; }
    api("PUT", "/profile", body).then(function (data) { state.profile = asObject(data); renderProfile(); setMessage("档案已保存"); return loadAll(); }).catch(function (error) { text($("profile-error"), error.message); });
  }
  function submitPreferences(event) {
    event.preventDefault(); text($("preferences-error"), ""); var items = {}; [["avoid", "pref-avoid"], ["reluctant", "pref-reluctant"], ["neutral", "pref-neutral"], ["love", "pref-love"]].forEach(function (pair) { splitItems(pair[1]).forEach(function (food) { items[food] = pair[0]; }); }); var body = { items: { general: items } };
    api("PUT", "/preferences", body).then(function (data) { state.preferences = asObject(data); setMessage("偏好已保存"); }).catch(function (error) { text($("preferences-error"), error.message); });
  }
  function appendPlanMessages(box, plan, source) {
    var notice = plan.notice || source.notice;
    if (notice) { var note = document.createElement("p"); note.className = "plan-notice"; text(note, notice); box.appendChild(note); }
    var warnings = [];
    [plan.warnings, source.warnings].forEach(function (items) { if (Array.isArray(items)) items.forEach(function (item) { if (warnings.indexOf(item) < 0) warnings.push(item); }); });
    warnings.forEach(function (warning) { var item = document.createElement("p"); item.className = "plan-warning"; text(item, "提醒：" + warning); box.appendChild(item); });
  }
  function appendIngredients(article, ingredients) {
    if (!Array.isArray(ingredients) || !ingredients.length) return;
    var list = document.createElement("ul"); list.className = "ingredients";
    ingredients.forEach(function (ingredient) {
      var item = document.createElement("li");
      if (typeof ingredient === "string") { text(item, ingredient); }
      else {
        ingredient = asObject(ingredient);
        var name = ingredient.name || ingredient.food || ingredient.ingredient || "食材";
        var portion = ingredient.portion || ingredient.amount || ingredient.quantity || (ingredient.grams != null ? ingredient.grams + " 克" : "");
        text(item, name + (portion ? " · " + portion : ""));
      }
      list.appendChild(item);
    });
    article.appendChild(list);
  }
  function renderPlan(data) {
    var plan = asObject(data); var source = plan.plan && typeof plan.plan === "object" ? plan.plan : plan; var meals = listFrom(source, "meals");
    var box = $("plan-result"); box.textContent = ""; box.hidden = false;
    appendPlanMessages(box, plan, source);
    if (!meals.length) { var empty = document.createElement("p"); empty.className = "muted"; text(empty, "后端暂未返回具体餐别，请稍后再试。"); box.appendChild(empty); return; }
    meals.forEach(function (meal) { var article = document.createElement("article"); article.className = "meal-plan"; var heading = document.createElement("h3"); text(heading, meal.meal || meal.meal_type || meal.title || "一餐"); var desc = document.createElement("p"); var kcal = meal.calories || meal.kcal || meal.calories_kcal; text(desc, (meal.name || meal.text || meal.description || "按偏好搭配") + (kcal != null ? " · 约 " + displayNumber(kcal) + " 千卡" : "")); article.appendChild(heading); article.appendChild(desc); appendIngredients(article, meal.ingredients); box.appendChild(article); });
  }
  function makePlan() { text($("plan-error"), ""); text($("plan-status"), "生成中…"); $("plan-button").disabled = true; api("POST", "/days/" + state.date + "/plan", {}).then(function (data) { renderPlan(data); text($("plan-status"), "已生成"); }).catch(function (error) { text($("plan-error"), error.message); text($("plan-status"), ""); }).finally(function () { $("plan-button").disabled = false; }); }

  $("today-label").textContent = humanDate(state.date); $("entry-form").addEventListener("submit", submitEntry); $("entry-type").addEventListener("change", toggleEntryFields); $("profile-form").addEventListener("submit", submitProfile); $("preferences-form").addEventListener("submit", submitPreferences); $("plan-button").addEventListener("click", makePlan); $("refresh-button").addEventListener("click", loadAll); toggleEntryFields(); loadAll();
}());
