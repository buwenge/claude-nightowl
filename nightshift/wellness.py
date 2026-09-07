"""健康记录服务：独立的静态页与 JSON API。

路由层只负责 HTTP、校验和响应封装。持久化与营养计算优先委托给
``wellness_store``/``wellness_calc``；在模块尚未安装或接口升级期间，使用
本文件的同目录 JSON 兼容层，保证手动记录仍可用。
"""

from __future__ import annotations

import datetime as _dt
import importlib
import inspect
import json
import math
import os
import re
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import store

__all__ = ["MAX_BODY_BYTES", "WELLNESS_DIR", "make_server", "serve_http"]

MAX_BODY_BYTES = 256 * 1024
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DAY_PATH = re.compile(r"^/health/api/days/(\d{4}-\d{2}-\d{2})/entries(?:/([^/]+))?$")
_SUMMARY_PATH = re.compile(r"^/health/api/days/(\d{4}-\d{2}-\d{2})/summary$")
_PLAN_PATH = re.compile(r"^/health/api/days/(\d{4}-\d{2}-\d{2})/plan$")
_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_ALLOWED_ENTRY_TYPES = {"meal", "exercise", "weight", "water"}
_PREFERENCE_LEVELS = {"avoid", "reluctant", "neutral", "love"}

# 只允许前端需要的固定文件。目录名和文件名均来自常量，不拼接用户输入。
WELLNESS_DIR = Path(__file__).resolve().parent.parent / "wellness"
_STATIC_FILES = {
    "index.html": "index.html",
    "app.js": "app.js",
    "style.css": "style.css",
}
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class WellnessError(Exception):
    """可直接返回给客户端的输入或存储错误。"""

    def __init__(self, message: str, code: str = "invalid_request", status: int = 400,
                 fields: dict[str, str] | None = None):
        super().__init__(message)
        self.message, self.code, self.status, self.fields = message, code, status, fields


def _envelope(data: Any) -> dict:
    return {"ok": True, "data": data}


def _error(exc: WellnessError) -> dict:
    result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    if exc.fields:
        result["error"]["fields"] = exc.fields
    return result


def _finite_number(value: Any, name: str, low: float | None = None,
                   high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WellnessError(f"{name} 必须是数字", fields={name: "number"})
    number = float(value)
    if not math.isfinite(number) or (low is not None and number < low) or (high is not None and number > high):
        raise WellnessError(f"{name} 超出允许范围", fields={name: "range"})
    return number


def _validate_date(value: str) -> str:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise WellnessError("日期必须是 YYYY-MM-DD", fields={"date": "date"})
    try:
        _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise WellnessError("日期不是有效日历日期", fields={"date": "date"}) from exc
    return value


def _jsonable(data: Any) -> Any:
    """拒绝 NaN/Infinity，避免把不可移植的 JSON 写进数据目录。"""
    try:
        json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WellnessError("请求中含有不可保存的 JSON 值") from exc
    return data


def _mod(name: str):
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ModuleNotFoundError:
        return None


def _home() -> Path:
    return store.home() / "wellness"


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise WellnessError("健康数据文件损坏，请先备份后修复", "storage_corrupt", 500) from exc


def _call(mod: Any, names: tuple[str, ...], *args, **kwargs) -> Any:
    if mod is None:
        return None
    target = mod
    # wellness_store 以 WellnessStore 对象提供实现，模块本身只暴露 get_store。
    factory = getattr(mod, "get_store", None)
    if callable(factory):
        target = factory()
    for name in names:
        fn = getattr(target, name, None)
        if callable(fn):
            # 适配层只允许显式列出的别名；函数内部 TypeError 必须暴露，不能
            # 静默换另一个实现并把真正的 bug 伪装成“没有数据”。
            return fn(*args, **kwargs)
    return None


def _profile() -> dict:
    result = _call(_mod("wellness_store"), ("load_profile", "get_profile", "read_profile"))
    if result is not None:
        return result if isinstance(result, dict) else {}
    return _read_json(_home() / "profile.json", {})


def _save_profile(data: dict) -> dict:
    mod = _mod("wellness_store")
    result = _call(mod, ("save_profile", "write_profile", "put_profile", "update_profile"), data)
    if result is not None:
        return result if isinstance(result, dict) else data
    _atomic_json(_home() / "profile.json", data)
    return data


def _preferences() -> dict:
    result = _call(_mod("wellness_store"), ("load_preferences", "get_preferences", "read_preferences"))
    if result is not None:
        return result if isinstance(result, dict) else {}
    return _read_json(_home() / "preferences.json", {})


def _save_preferences(data: dict) -> dict:
    result = _call(_mod("wellness_store"), ("save_preferences", "write_preferences", "put_preferences", "update_preferences"), data)
    if result is not None:
        return result if isinstance(result, dict) else data
    _atomic_json(_home() / "preferences.json", data)
    return data


def _day_path(day: str) -> Path:
    # day 已在路由层校验；再用 datetime 转回字符串，阻断任何路径片段。
    return _home() / "entries" / f"{_validate_date(day)}.json"


def _entries(day: str) -> list[dict]:
    mod = _mod("wellness_store")
    result = _call(mod, ("load_day", "get_day", "get_entries", "read_entries"), day)
    if result is not None:
        if isinstance(result, dict):
            result = result.get("entries", [])
        return result if isinstance(result, list) else []
    data = _read_json(_day_path(day), {"entries": []})
    return data.get("entries", []) if isinstance(data, dict) else []


def _save_entries(day: str, entries: list[dict]) -> None:
    mod = _mod("wellness_store")
    result = _call(mod, ("save_entries", "write_entries"), day, entries)
    if result is not None:
        return
    _atomic_json(_day_path(day), {"version": 1, "date": day, "entries": entries})


def _delete_entry(day: str, entry_id: str) -> bool:
    mod = _mod("wellness_store")
    value = _call(mod, ("delete_entry", "remove_entry"), day, entry_id)
    if value is not None:
        return bool(value)
    entries = _entries(day)
    kept = [entry for entry in entries if str(entry.get("id")) != entry_id]
    if len(kept) == len(entries):
        return False
    _save_entries(day, kept)
    return True


def _summary(day: str, entries: list[dict]) -> dict:
    calc = _mod("wellness_calc")
    result = None
    if calc is not None and callable(getattr(calc, "daily_summary", None)):
        profile = _profile()
        target = _safe_budget(profile)
        try:
            result = calc.daily_summary(entries, target, date=day)
        except (TypeError, ValueError):
            result = None
    if result is None:
        result = _call(calc, ("calculate_summary", "summary_for_day"), day, entries, _profile())
    if isinstance(result, dict):
        # 对外保留计划书里的简短字段名，同时兼容计算模块的 calories_* 命名。
        result = dict(result)
        result.setdefault("consumed_kcal", result.get("intake_kcal", result.get("calories_in", 0)))
        result.setdefault("net_kcal", result.get("net_calories"))
        result.setdefault("budget_kcal", result.get("target_calories"))
        result.setdefault("remaining_kcal", result.get("remaining_calories"))
        result.setdefault("entry_count", result.get("record_count", 0))
        return result
    consumed = 0.0
    exercise = 0.0
    for entry in entries:
        kind = entry.get("type")
        if kind == "meal":
            consumed += float(entry.get("calories", entry.get("kcal", 0)) or 0)
        elif kind == "exercise":
            exercise += float(entry.get("calories_burned", entry.get("calories", entry.get("kcal", 0))) or 0)
    profile = _profile()
    budget = _safe_budget(profile)
    if not isinstance(budget, (int, float)) or isinstance(budget, bool):
        budget = None
    return {
        "date": day,
        "consumed_kcal": round(consumed, 1),
        "exercise_kcal": round(exercise, 1),
        "net_kcal": round(consumed - exercise, 1),
        "budget_kcal": budget,
        "remaining_kcal": round(budget - consumed, 1) if budget is not None else None,
        "entry_count": len(entries),
    }


def _safe_budget(profile: dict) -> float | None:
    """返回计算模块给出的权威目标，残旧手填预算只在档案不完整时兜底。"""
    calc = _mod("wellness_calc")
    required = ("birth_year", "height_cm", "weight_kg", "sex", "activity_level")
    if calc is not None and all(key in profile for key in required):
        try:
            goal = calc.calculate_goal(profile)
            target = goal.get("target_calories")
            if isinstance(target, (int, float)) and not isinstance(target, bool) and math.isfinite(float(target)):
                return float(target)
        except (TypeError, ValueError, KeyError):
            pass
    raw = profile.get("daily_target_kcal", profile.get("calorie_budget"))
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(float(raw)):
        return None
    floor = 1500.0 if profile.get("sex") in {"male", "m"} else 1200.0
    return float(raw) if float(raw) >= floor else None


def _plan(day: str, payload: dict) -> Any:
    calc = _mod("wellness_calc")
    mod = _mod("wellness_store")
    profile = _profile()
    preferences = _preferences()
    if isinstance(preferences, dict) and isinstance(preferences.get("categories"), dict):
        # 前端的四栏数组格式转换成 calc 的食材->档位格式，保留原始偏好文档不变。
        normalized = dict(preferences)
        items = dict(normalized.get("items") or {})
        for level in ("avoid", "reluctant", "neutral", "love"):
            values = preferences["categories"].get(level, [])
            if isinstance(values, str):
                values = re.split(r"[，,、\n]", values)
            for food in values if isinstance(values, list) else []:
                if isinstance(food, str) and food.strip():
                    items[food.strip()] = level
        normalized["items"] = items
        preferences = normalized
    result = None
    generator = getattr(calc, "generate_plan", None) if calc else None
    if callable(generator):
        # 核心计算模块的契约以参数名为准，显式映射避免吞掉函数内部 TypeError。
        options = dict(payload)
        options["date"] = day
        safe_budget = _safe_budget(profile)
        if safe_budget is not None:
            options["target_calories"] = safe_budget
        else:
            for key in ("calorie_target", "target_calories", "daily_target_kcal"):
                if isinstance(options.get(key), (int, float)) and options[key] < 1200:
                    options.pop(key, None)
        values = {
            "profile": profile, "preferences": preferences, "options": options,
            "payload": options, "date": day, "day": day,
            "target_kcal": payload.get("target_kcal"),
        }
        signature = inspect.signature(generator)
        kwargs = {
            name: values[name]
            for name, parameter in signature.parameters.items()
            if name in values and parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
            and (values[name] is not None or parameter.default is parameter.empty)
        }
        result = generator(**kwargs)
    if result is None:
        # 未启用食谱数据库时仍返回可确认的三餐草案；避免项永远不进入草案。
        avoided = set()
        categories = preferences.get("categories", {}) if isinstance(preferences, dict) else {}
        for item in categories.get("avoid", []) if isinstance(categories, dict) else []:
            if isinstance(item, str) and item.strip():
                avoided.add(item.strip().lower())
        items = preferences.get("items", {}) if isinstance(preferences, dict) else {}
        if isinstance(items, dict):
            for values in items.values():
                if isinstance(values, dict):
                    avoided.update(str(name).lower() for name, level in values.items() if level == "avoid")
        candidates = [
            {"meal": "早餐", "name": "燕麦酸奶水果", "calories": 420},
            {"meal": "午餐", "name": "米饭鸡肉时蔬", "calories": 650},
            {"meal": "晚餐", "name": "土豆鱼肉沙拉", "calories": 580},
        ]
        meals = [meal for meal in candidates if not any(word in meal["name"].lower() for word in avoided)]
        result = {"date": day, "meals": meals, "total_kcal": sum(meal["calories"] for meal in meals),
                  "notice": "这是可调整的生活记录草案，不是医疗或营养处方。"}
    if result is None:
        result = {"date": day, "meals": [], "notice": "尚未配置本地食谱数据，请手动填写餐单。"}
    if mod is not None:
        _call(mod, ("save_plan", "write_plan"), day, result)
    return result


def _profile_view(profile: dict) -> dict:
    """附带可用时的 BMI/目标估算，但不因档案尚未填完而阻断保存。"""
    calc = _mod("wellness_calc")
    if calc is None or not profile:
        return profile
    try:
        summary = calc.profile_summary(profile)
    except (TypeError, ValueError):
        return profile
    view = dict(profile)
    view["bmi"] = summary.get("bmi")
    # calculate_goal 已处理 deficit、BMI 与最低摄入护栏；这里仅展示，不二次改写。
    view["calculation"] = summary.get("goal")
    return view


def _validate_profile(data: Any) -> dict:
    if not isinstance(data, dict):
        raise WellnessError("档案必须是 JSON 对象")
    allowed = {"birth_year", "height_cm", "weight_kg", "target_weight_kg", "sex", "biological_sex", "gender",
               "activity_level", "activity", "waist_cm", "target_date", "weekly_exercise", "daily_target_kcal",
               "calorie_budget", "deficit_kcal", "pregnant_or_breastfeeding", "eating_disorder_risk", "medical_condition"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise WellnessError("档案包含未知字段", fields={key: "unknown" for key in unknown})
    out = dict(data)
    for key, low, high in (("height_cm", 80, 260), ("weight_kg", 20, 500), ("target_weight_kg", 20, 500),
                           ("waist_cm", 20, 300), ("daily_target_kcal", 0, 10000), ("calorie_budget", 0, 10000),
                           ("deficit_kcal", 0, 5000)):
        if key in out:
            if out[key] is None:
                out.pop(key)
                continue
            out[key] = _finite_number(out[key], key, low, high)
            if out[key].is_integer():
                out[key] = int(out[key])
    if "birth_year" in out:
        year = out["birth_year"]
        if year is None:
            out.pop("birth_year")
            year = None
        if year is None:
            pass
        elif isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= _dt.date.today().year:
            raise WellnessError("birth_year 超出允许范围", fields={"birth_year": "range"})
    if out.get("sex") is None:
        out.pop("sex", None)
    if "sex" in out and out["sex"] not in {"female", "male", "other", "unspecified"}:
        raise WellnessError("sex 取值不受支持", fields={"sex": "choice"})
    if "activity_level" in out and out["activity_level"] is None:
        out.pop("activity_level")
    if "activity_level" in out and out["activity_level"] not in {"sedentary", "light", "moderate", "active", "very_active"}:
        raise WellnessError("activity_level 取值不受支持", fields={"activity_level": "choice"})
    if "target_date" in out and out["target_date"] is not None:
        _validate_date(out["target_date"])
    return _jsonable(out)


def _validate_preferences(data: Any) -> dict:
    if not isinstance(data, dict):
        raise WellnessError("偏好必须是 JSON 对象")
    out = dict(data)
    if "items" in out:
        if not isinstance(out["items"], dict):
            raise WellnessError("items 必须是对象", fields={"items": "object"})
        for category, values in out["items"].items():
            if not isinstance(category, str) or not isinstance(values, dict):
                raise WellnessError("偏好分类必须映射到对象")
            for food, level in values.items():
                if not isinstance(food, str) or not isinstance(level, str) or level not in _PREFERENCE_LEVELS:
                    raise WellnessError("食材偏好档位无效", fields={str(food): "choice"})
    for key in ("allergies", "dietary_rules", "equipment"):
        if key in out and not isinstance(out[key], (list, dict)):
            raise WellnessError(f"{key} 必须是数组或对象", fields={key: "array_or_object"})
    return _jsonable(out)


def _validate_entry(data: Any) -> dict:
    if not isinstance(data, dict):
        raise WellnessError("记录必须是 JSON 对象")
    out = dict(data)
    kind = out.get("type")
    if kind not in _ALLOWED_ENTRY_TYPES:
        raise WellnessError("记录 type 必须是 meal、exercise、weight 或 water", fields={"type": "choice"})
    if kind == "meal":
        if not isinstance(out.get("name", out.get("text")), str) or not out.get("name", out.get("text")).strip():
            raise WellnessError("meal 需要 name", fields={"name": "required"})
        if "calories" in out and out["calories"] is not None:
            _finite_number(out["calories"], "calories", 0, 100000)
    elif kind == "exercise":
        if not isinstance(out.get("name", out.get("text")), str) or not out.get("name", out.get("text")).strip():
            raise WellnessError("exercise 需要 name", fields={"name": "required"})
        if "duration_min" in out:
            _finite_number(out["duration_min"], "duration_min", 0, 1440)
        for key in ("calories_burned", "calories"):
            if key in out and out[key] is not None:
                _finite_number(out[key], key, 0, 100000)
    elif kind == "weight":
        if "weight_kg" not in out:
            raise WellnessError("weight 需要 weight_kg", fields={"weight_kg": "required"})
        _finite_number(out["weight_kg"], "weight_kg", 2, 500)
    elif kind == "water":
        if "ml" not in out:
            raise WellnessError("water 需要 ml", fields={"ml": "required"})
        _finite_number(out["ml"], "ml", 0, 100000)
    return _jsonable(out)


class _Handler(BaseHTTPRequestHandler):
    server_version = "nightshift-wellness/1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, payload: dict, content_type: str = "application/json; charset=utf-8") -> None:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send_raw(status, raw, content_type)

    def _send_raw(self, status: int, raw: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _fail(self, exc: WellnessError) -> None:
        self._send(exc.status, _error(exc))

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise WellnessError("Content-Length 无效", "invalid_body", 400) from exc
        if length > MAX_BODY_BYTES:
            raise WellnessError("请求正文过大", "body_too_large", 413)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise WellnessError("请求正文不完整", "invalid_body", 400)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WellnessError("请求正文必须是 UTF-8 JSON", "invalid_json", 400) from exc
        if not isinstance(data, dict):
            raise WellnessError("请求正文必须是 JSON 对象", fields={"body": "object"})
        return data

    def _static(self, name: str) -> None:
        relative = _STATIC_FILES.get(name)
        if not relative:
            return self._send(404, _error(WellnessError("没有这个路径", "not_found", 404)))
        target = WELLNESS_DIR / relative
        try:
            # 固定白名单后仍使用 resolve，防止部署目录被意外替换成越界链接。
            if target.is_symlink() or target.resolve().parent != WELLNESS_DIR.resolve() or not target.is_file():
                raise OSError
            raw = target.read_bytes()
        except OSError:
            return self._send(404, _error(WellnessError("静态文件不存在", "not_found", 404)))
        self._send_raw(200, raw, _CONTENT_TYPES[target.suffix])

    def _route(self, method: str) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            return self._static("index.html")
        if path.startswith("/wellness/"):
            return self._static(path[len("/wellness/"):])
        if path.startswith("/health/wellness/"):
            return self._static(path[len("/health/wellness/"):])
        if path.startswith("/health/") and path[len("/health/"):] in _STATIC_FILES:
            return self._static(path[len("/health/"):])
        if path == "/health/api/profile":
            if method == "GET":
                return self._send(200, _envelope(_profile_view(_profile())))
            if method == "PUT":
                return self._put_profile()
        if path == "/health/api/preferences":
            if method == "GET":
                return self._send(200, _envelope(_preferences()))
            if method == "PUT":
                return self._put_preferences()
        match = _DAY_PATH.fullmatch(path)
        if match:
            day, entry_id = match.groups()
            _validate_date(day)
            if entry_id and not _ID.fullmatch(entry_id):
                raise WellnessError("记录 id 无效", fields={"id": "format"})
            if entry_id and method == "DELETE":
                if not _delete_entry(day, entry_id):
                    raise WellnessError("记录不存在", "not_found", 404)
                return self._send(200, _envelope({"deleted": entry_id}))
            if entry_id:
                raise WellnessError("没有这个路径", "not_found", 404)
            if method == "GET":
                return self._send(200, _envelope({"date": day, "entries": _entries(day)}))
            if method == "POST":
                return self._post_entry(day)
        match = _SUMMARY_PATH.fullmatch(path)
        if match and method == "GET":
            day = _validate_date(match.group(1))
            return self._send(200, _envelope(_summary(day, _entries(day))))
        match = _PLAN_PATH.fullmatch(path)
        if match and method == "POST":
            day = _validate_date(match.group(1))
            return self._post_plan(day)
        raise WellnessError("没有这个路径", "not_found", 404)

    def _put_profile(self) -> None:
        data = _validate_profile(self._body())
        data.setdefault("updated_at", _dt.datetime.now(_dt.timezone.utc).isoformat())
        return self._send(200, _envelope(_profile_view(_save_profile(data))))

    def _put_preferences(self) -> None:
        return self._send(200, _envelope(_save_preferences(_validate_preferences(self._body()))))

    def _post_entry(self, day: str) -> None:
        entry = _validate_entry(self._body())
        entry.setdefault("id", str(uuid.uuid4()))
        entry.setdefault("date", day)
        entry.setdefault("created_at", _dt.datetime.now(_dt.timezone.utc).isoformat())
        created = _call(_mod("wellness_store"), ("create_entry", "add_entry"), day, entry)
        if isinstance(created, dict):
            return self._send(201, _envelope(created))
        entries = _entries(day)
        entries.append(entry)
        _save_entries(day, entries)
        return self._send(201, _envelope(entry))

    def _post_plan(self, day: str) -> None:
        payload = self._body()
        # 计划参数允许由计算模块自行扩展，但必须先拒绝明显错误的日期/类型。
        for key in ("target_kcal", "target_calories", "calorie_target", "daily_target_kcal"):
            if key in payload:
                _finite_number(payload[key], key, 0, 10000)
        return self._send(200, _envelope(_plan(day, payload)))

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except WellnessError as exc:
            self._fail(exc)
        except (OSError, ValueError, TypeError) as exc:
            self._fail(WellnessError("健康服务暂时无法处理请求", "storage_error", 500))
        except Exception as exc:
            # 存储层的自定义错误也必须返回稳定 envelope，不能让线程直接断开连接。
            self._fail(WellnessError("健康服务暂时无法处理请求", "storage_error", 500))


def make_server(config: dict | None = None) -> ThreadingHTTPServer:
    """按 config.wellness/http 配置创建服务器；测试可用 port=0。"""
    config = config or {}
    # 健康服务不能回退到 nightshift 主站的 config.http（通常是 8190）。
    section = config.get("wellness")
    if not isinstance(section, dict):
        section = config.get("health")
    cfg = section.get("http") if isinstance(section, dict) else None
    if not isinstance(cfg, dict):
        cfg = {}
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 8191))
    return ThreadingHTTPServer((host, port), _Handler)


def serve_http(config: dict | None = None) -> None:
    server = make_server(config)
    try:
        server.serve_forever()
    finally:
        server.server_close()
