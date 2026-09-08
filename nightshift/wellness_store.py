"""健康数据的本地 JSON 仓库。

每份文档都在 ``NIGHTSHIFT_HOME/wellness`` 下保存。写入使用同目录临时文件、
flush/fsync 和 os.replace；读改写在独立锁文件的 flock 内完成。损坏文档只报
错，不会被默认值悄悄覆盖。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date as calendar_date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

__all__ = [
    "CorruptDataError", "WellnessStore", "WellnessStoreError",
    "data_dir", "get_store", "load_profile", "save_profile",
    "update_profile", "load_preferences", "save_preferences",
    "update_preferences", "load_day", "save_day", "load_entries",
    "save_entries", "create_entry", "get_entry", "update_entry",
    "delete_entry", "load_plan", "save_plan", "update_plan",
    "range_summary", "report", "range_report",
]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class WellnessStoreError(Exception):
    """健康数据仓库错误。"""


class CorruptDataError(WellnessStoreError):
    """JSON 存在但无法解析或结构不正确。"""


def data_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """返回健康数据目录，不创建目录。"""
    if root is not None:
        return Path(root).expanduser() / "wellness"
    home = os.environ.get("NIGHTSHIFT_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".nightshift") / "wellness"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _valid_date(value: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError("日期必须是 YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期不是有效的日历日期") from exc
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # 目录 fsync 让 rename 在断电后也尽量可见；某些平台不支持时不影响替换。
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class WellnessStore:
    """单用户健康数据仓库。"""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = data_dir(root)
        self.entries_dir = self.root / "entries"
        self.plans_dir = self.root / "plans"
        self._lock_path = self.root / ".wellness.lock"

    @contextmanager
    def _lock(self, *, exclusive: bool = False) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CorruptDataError(f"健康数据 JSON 损坏：{path}：{exc}") from exc
        return value

    def _read_locked(self, path: Path, default: Any) -> Any:
        with self._lock():
            return self._read(path, default)

    def _write_document(self, path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock(exclusive=True):
            _atomic_json(path, dict(document))
        return dict(document)

    # ---------- profile / preferences ----------

    def load_profile_document(self) -> dict[str, Any] | None:
        value = self._read_locked(self.root / "profile.json", None)
        if value is not None and not isinstance(value, dict):
            raise CorruptDataError("profile.json 顶层必须是对象")
        return value

    def load_profile(self) -> dict[str, Any] | None:
        """读取档案内容；没有档案时返回 None。"""
        document = self.load_profile_document()
        if document is None:
            return None
        profile = document.get("profile", document)
        if not isinstance(profile, dict):
            raise CorruptDataError("profile.json 的 profile 必须是对象")
        return dict(profile)

    get_profile = load_profile
    read_profile = load_profile

    def save_profile(self, profile: Mapping[str, Any], *, goals: Mapping[str, Any] | None = None, calculation_preferences: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(profile, Mapping):
            raise ValueError("profile 必须是对象")
        document: dict[str, Any] = {
            "version": 1, "profile": dict(profile), "updated_at": _now(),
        }
        if goals is not None:
            document["goals"] = dict(goals)
        if calculation_preferences is not None:
            document["calculation_preferences"] = dict(calculation_preferences)
        self._write_document(self.root / "profile.json", document)
        return dict(profile)

    write_profile = save_profile

    def update_profile(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, Mapping):
            raise ValueError("profile patch 必须是对象")
        path = self.root / "profile.json"
        with self._lock(exclusive=True):
            current = self._read(path, {"version": 1, "profile": {}})
            if not isinstance(current, dict):
                raise CorruptDataError("profile.json 顶层必须是对象")
            profile = current.get("profile", current)
            if not isinstance(profile, dict):
                raise CorruptDataError("profile.json 的 profile 必须是对象")
            # 旧的直接档案格式升级为规范 envelope，不丢未知字段。
            profile = dict(profile)
            profile.update(patch)
            document = dict(current)
            document.update({"version": 1, "profile": profile, "updated_at": _now()})
            _atomic_json(path, document)
        return profile

    def load_preferences_document(self) -> dict[str, Any] | None:
        value = self._read_locked(self.root / "preferences.json", None)
        if value is not None and not isinstance(value, dict):
            raise CorruptDataError("preferences.json 顶层必须是对象")
        return value

    def load_preferences(self) -> dict[str, Any] | None:
        document = self.load_preferences_document()
        if document is None:
            return None
        value = document.get("preferences", document)
        if not isinstance(value, dict):
            raise CorruptDataError("preferences.json 的 preferences 必须是对象")
        return dict(value)

    get_preferences = load_preferences
    read_preferences = load_preferences

    def save_preferences(self, preferences: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(preferences, Mapping):
            raise ValueError("preferences 必须是对象")
        document = {"version": 1, "preferences": dict(preferences), "updated_at": _now()}
        self._write_document(self.root / "preferences.json", document)
        return dict(preferences)

    write_preferences = save_preferences

    def update_preferences(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, Mapping):
            raise ValueError("preferences patch 必须是对象")
        path = self.root / "preferences.json"
        with self._lock(exclusive=True):
            current = self._read(path, {"version": 1, "preferences": {}})
            if not isinstance(current, dict):
                raise CorruptDataError("preferences.json 顶层必须是对象")
            value = current.get("preferences", current)
            if not isinstance(value, dict):
                raise CorruptDataError("preferences.json 的 preferences 必须是对象")
            value = dict(value)
            value.update(patch)
            document = dict(current)
            document.update({"version": 1, "preferences": value, "updated_at": _now()})
            _atomic_json(path, document)
        return value

    # ---------- entries ----------

    def _entry_path(self, day: str) -> Path:
        return self.entries_dir / f"{_valid_date(day)}.json"

    def _empty_entries(self, day: str) -> dict[str, Any]:
        return {"version": 1, "date": day, "records": [], "meals": [], "exercises": [], "weights": [], "water": []}

    def load_entries_document(self, day: str) -> dict[str, Any]:
        day = _valid_date(day)
        value = self._read_locked(self._entry_path(day), self._empty_entries(day))
        if not isinstance(value, dict):
            raise CorruptDataError(f"{day} entries 顶层必须是对象")
        return value

    def _records_from_document(self, document: Mapping[str, Any]) -> list[dict[str, Any]]:
        if isinstance(document.get("records"), list):
            records = document["records"]
        elif isinstance(document.get("entries"), list):
            # 与 HTTP 层早期版本兼容；新写入同时保留这个直观别名。
            records = document["entries"]
        else:
            records = []
            for key in ("meals", "exercises", "weights", "water"):
                value = document.get(key, [])
                if isinstance(value, list):
                    records.extend(value)
        if not all(isinstance(record, dict) for record in records):
            raise CorruptDataError("entries 的 records 必须是对象数组")
        return [dict(record) for record in records]

    def list_entries(self, day: str) -> list[dict[str, Any]]:
        return self._records_from_document(self.load_entries_document(day))

    read_entries = list_entries
    get_entries = list_entries

    def save_entries(self, day: str, entries: Mapping[str, Any] | list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        day = _valid_date(day)
        if isinstance(entries, Mapping):
            document = dict(entries)
            records = self._records_from_document(document)
        elif isinstance(entries, list) and all(isinstance(item, Mapping) for item in entries):
            records = [dict(item) for item in entries]
            document = {}
        else:
            raise ValueError("entries 必须是对象或对象数组")
        document.update({"version": 1, "date": day, "records": records, "updated_at": _now()})
        document["entries"] = records
        for key in ("meals", "exercises", "weights", "water"):
            document[key] = []
        for record in records:
            kind = str(record.get("type", record.get("kind", "meal"))).lower()
            category = {"meal": "meals", "food": "meals", "饮食": "meals", "exercise": "exercises", "workout": "exercises", "运动": "exercises", "weight": "weights", "体重": "weights", "water": "water", "饮水": "water"}.get(kind)
            if category:
                document[category].append(record)
        self._write_document(self._entry_path(day), document)
        return records

    write_entries = save_entries

    def load_day(self, day: str) -> list[dict[str, Any]]:
        """兼容 API 层的按日读取别名。"""
        return self.list_entries(day)

    get_day = load_day

    def save_day(self, day: str, document: Mapping[str, Any]) -> list[dict[str, Any]]:
        """兼容 API 层的按日保存别名。"""
        return self.save_entries(day, document)

    write_day = save_day

    def create_entry(self, day: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        day = _valid_date(day)
        if not isinstance(entry, Mapping):
            raise ValueError("entry 必须是对象")
        path = self._entry_path(day)
        with self._lock(exclusive=True):
            document = self._read(path, self._empty_entries(day))
            if not isinstance(document, dict):
                raise CorruptDataError(f"{day} entries 顶层必须是对象")
            records = self._records_from_document(document)
            now = _now()
            record = dict(entry)
            record.setdefault("id", str(uuid.uuid4()))
            record.setdefault("created_at", now)
            record["updated_at"] = now
            if any(item.get("id") == record["id"] for item in records):
                raise WellnessStoreError("entry id 已存在")
            records.append(record)
            document = self._document_with_records(day, document, records)
            _atomic_json(path, document)
        return record

    add_entry = create_entry

    def _document_with_records(self, day: str, old: Mapping[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
        document = dict(old)
        document.update({"version": 1, "date": day, "records": records, "updated_at": _now()})
        document["entries"] = records
        for key in ("meals", "exercises", "weights", "water"):
            document[key] = []
        for record in records:
            kind = str(record.get("type", record.get("kind", "meal"))).lower()
            category = {"meal": "meals", "food": "meals", "饮食": "meals", "exercise": "exercises", "workout": "exercises", "运动": "exercises", "weight": "weights", "体重": "weights", "water": "water", "饮水": "water"}.get(kind)
            if category:
                document[category].append(record)
        return document

    def get_entry(self, day: str, entry_id: str) -> dict[str, Any] | None:
        for record in self.list_entries(day):
            if record.get("id") == entry_id:
                return record
        return None

    def update_entry(self, day: str, entry_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        day = _valid_date(day)
        if not isinstance(patch, Mapping):
            raise ValueError("entry patch 必须是对象")
        path = self._entry_path(day)
        with self._lock(exclusive=True):
            document = self._read(path, self._empty_entries(day))
            if not isinstance(document, dict):
                raise CorruptDataError(f"{day} entries 顶层必须是对象")
            records = self._records_from_document(document)
            for index, current in enumerate(records):
                if current.get("id") == entry_id:
                    updated = dict(current)
                    updated.update(patch)
                    updated["id"] = entry_id
                    updated["updated_at"] = _now()
                    records[index] = updated
                    document = self._document_with_records(day, document, records)
                    _atomic_json(path, document)
                    return updated
        raise KeyError(f"entry 不存在：{entry_id}")

    edit_entry = update_entry

    def delete_entry(self, day: str, entry_id: str) -> bool:
        day = _valid_date(day)
        path = self._entry_path(day)
        with self._lock(exclusive=True):
            document = self._read(path, self._empty_entries(day))
            if not isinstance(document, dict):
                raise CorruptDataError(f"{day} entries 顶层必须是对象")
            records = self._records_from_document(document)
            remaining = [record for record in records if record.get("id") != entry_id]
            if len(remaining) == len(records):
                return False
            _atomic_json(path, self._document_with_records(day, document, remaining))
            return True

    remove_entry = delete_entry

    # ---------- plans ----------

    def _plan_path(self, day: str) -> Path:
        return self.plans_dir / f"{_valid_date(day)}.json"

    def load_plan_document(self, day: str) -> dict[str, Any] | None:
        day = _valid_date(day)
        value = self._read_locked(self._plan_path(day), None)
        if value is not None and not isinstance(value, dict):
            raise CorruptDataError(f"{day} plan 顶层必须是对象")
        return value

    def load_plan(self, day: str) -> dict[str, Any] | None:
        document = self.load_plan_document(day)
        if document is None:
            return None
        value = document.get("plan", document)
        if not isinstance(value, dict):
            raise CorruptDataError(f"{day} plan 必须是对象")
        return dict(value)

    read_plan = load_plan
    get_plan = load_plan

    def save_plan(self, day: str, plan: Mapping[str, Any]) -> dict[str, Any]:
        day = _valid_date(day)
        if not isinstance(plan, Mapping):
            raise ValueError("plan 必须是对象")
        document = {"version": 1, "date": day, "plan": dict(plan), "updated_at": _now()}
        self._write_document(self._plan_path(day), document)
        return dict(plan)

    write_plan = save_plan

    def update_plan(self, day: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        day = _valid_date(day)
        if not isinstance(patch, Mapping):
            raise ValueError("plan patch 必须是对象")
        path = self._plan_path(day)
        with self._lock(exclusive=True):
            document = self._read(path, {"version": 1, "date": day, "plan": {}})
            if not isinstance(document, dict):
                raise CorruptDataError(f"{day} plan 顶层必须是对象")
            plan = document.get("plan", document)
            if not isinstance(plan, dict):
                raise CorruptDataError(f"{day} plan 必须是对象")
            plan = dict(plan)
            plan.update(patch)
            document.update({"version": 1, "date": day, "plan": plan, "updated_at": _now()})
            _atomic_json(path, document)
        return plan

    # ---------- 范围汇总与报告 ----------

    def range_summary(self, from_day: str, to_day: str, *, target: float | None = None,
                      profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """按日读取并聚合闭区间数据。

        聚合发生在读时，按天 JSON 的存档格式无需迁移。体重移动平均会额外
        读取区间起点前六天，确保跨月和区间首日的七日窗口完整。
        """
        start = _as_calendar_date(from_day)
        finish = _as_calendar_date(to_day)
        if start > finish:
            raise ValueError("from 日期不能晚于 to 日期")
        budget = _target_from_profile(target, profile)
        days: list[dict[str, Any]] = []
        records_by_day: dict[str, list[dict[str, Any]]] = {}
        cursor = start - timedelta(days=6)
        while cursor <= finish:
            key = cursor.isoformat()
            records_by_day[key] = self.list_entries(key)
            cursor += timedelta(days=1)

        cursor = start
        while cursor <= finish:
            key = cursor.isoformat()
            records = records_by_day[key]
            summary = _daily_summary(records, budget, key)
            summary["foods"] = _food_list(records)
            summary["weight_7d_average"] = _moving_weight(records_by_day, cursor)
            days.append(summary)
            cursor += timedelta(days=1)

        numeric_keys = (
            "calories_in", "calories_out", "net_calories", "calorie_target",
            "meals_count", "exercise_count", "record_count",
        )
        totals: dict[str, Any] = {
            key: round(sum(float(day.get(key) or 0) for day in days), 1)
            for key in numeric_keys
        }
        macro_totals = {
            key: round(sum(float(day.get("macros", {}).get(key) or 0) for day in days), 1)
            for key in ("protein_g", "carbs_g", "fat_g", "fiber_g")
        }
        totals["macros"] = macro_totals
        totals.update(macro_totals)
        totals["days"] = len(days)
        averages: dict[str, Any] = {
            key: round(totals[key] / len(days), 1) if days else 0.0
            for key in numeric_keys
        }
        averages["macros"] = {
            key: round(value / len(days), 1) if days else 0.0
            for key, value in macro_totals.items()
        }
        averages.update(averages["macros"])
        weights = [day["weight_kg"] for day in days if day.get("weight_kg") is not None]
        averages["weight_kg"] = round(sum(weights) / len(weights), 1) if weights else None
        moving = [day["weight_7d_average"] for day in days if day.get("weight_7d_average") is not None]
        averages["weight_7d_average"] = round(sum(moving) / len(moving), 1) if moving else None
        return {
            "from": from_day,
            "to": to_day,
            "days": days,
            "totals": totals,
            "averages": averages,
            "weight_7d_average": [
                {"date": day["date"], "weight_kg": day["weight_7d_average"]}
                for day in days
            ],
        }

    aggregate_range = range_summary
    summarize_range = range_summary

    def report(self, period: str = "week", end: str | None = None,
               format: str = "json", *, target: float | None = None,
               profile: Mapping[str, Any] | None = None) -> dict[str, Any] | str:
        """生成七日或三十日 JSON/Markdown 报告。"""
        if period not in {"week", "month"}:
            raise ValueError("period 必须是 week 或 month")
        if format not in {"json", "md"}:
            raise ValueError("format 必须是 json 或 md")
        end_day = _as_calendar_date(end or calendar_date.today().isoformat())
        count = 7 if period == "week" else 30
        start_day = end_day - timedelta(days=count - 1)
        result = self.range_summary(start_day.isoformat(), end_day.isoformat(), target=target, profile=profile)
        return _markdown_report(result) if format == "md" else result

    range_report = report


def _as_calendar_date(value: str) -> calendar_date:
    """校验日期并返回标准库日期对象。"""
    normalized = _valid_date(value)
    return calendar_date.fromisoformat(normalized)


def _target_from_profile(target: float | None,
                         profile: Mapping[str, Any] | None) -> float | None:
    """从显式目标或档案计算每日目标；档案不完整时保持空值。"""
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        return float(target) if target > 0 else None
    if not isinstance(profile, Mapping):
        return None
    value = profile.get("calorie_target")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    try:
        from .wellness_calc import calculate_goal
        result = calculate_goal(profile)
    except (ImportError, KeyError, TypeError, ValueError):
        return None
    value = result.get("calorie_target") if isinstance(result, Mapping) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _daily_summary(records: list[dict[str, Any]], target: float | None,
                   day: str) -> dict[str, Any]:
    """调用计算模块，集中处理报告层的日期参数。"""
    from .wellness_calc import daily_summary
    return daily_summary(records, target, date_value=day)


def _record_number(record: Mapping[str, Any], *names: str) -> float:
    for name in names:
        value = record.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _food_list(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """提取报告中人和模型都容易阅读的每日食物清单。"""
    foods: list[dict[str, Any]] = []
    for record in records:
        if str(record.get("type", record.get("kind", "meal"))).lower() not in {"meal", "food", "饮食", "餐"}:
            continue
        name = record.get("name", record.get("text", "未命名食物"))
        item: dict[str, Any] = {"name": str(name)}
        for key in ("portion", "unit", "calories", "calories_kcal", "protein_g", "carbs_g", "fat_g"):
            if key in record and record[key] is not None:
                item[key] = record[key]
        if "calories" not in item and "calories_kcal" in item:
            item["calories"] = item["calories_kcal"]
        foods.append(item)
    return foods


def _moving_weight(records_by_day: Mapping[str, list[dict[str, Any]]], day: calendar_date) -> float | None:
    """计算当天及之前六个自然日内每天最新体重的等权平均值。"""
    values: list[float] = []
    for offset in range(7):
        records = records_by_day.get((day - timedelta(days=offset)).isoformat(), [])
        weights = []
        for record in records:
            kind = str(record.get("type", record.get("kind", "meal"))).lower()
            if kind in {"weight", "体重"}:
                value = _record_number(record, "weight_kg", "weight")
                if value > 0:
                    stamp = str(record.get("recorded_at", record.get("created_at", record.get("timestamp", ""))))
                    weights.append((stamp, value))
        if weights:
            values.append(max(weights, key=lambda item: item[0])[1])
    return round(sum(values) / len(values), 1) if values else None


def _markdown_report(result: Mapping[str, Any]) -> str:
    """把范围汇总渲染为稳定、无需额外解析器的 Markdown。"""
    start, finish = result["from"], result["to"]
    lines = [f"# 健康记录报告（{start} 至 {finish}）", "", "## 每日汇总", "",
             "| 日期 | 摄入 | 运动 | 净值 | 目标 | 体重 | 记录数 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for day in result.get("days", []):
        weight = "-" if day.get("weight_kg") is None else f"{day['weight_kg']:.1f}"
        target = "-" if day.get("calorie_target") is None else f"{day['calorie_target']:.1f}"
        lines.append(
            f"| {day['date']} | {day['calories_in']:.1f} | {day['calories_out']:.1f} | "
            f"{day['net_calories']:.1f} | {target} | {weight} | {day['record_count']} |"
        )
    lines.extend(["", "## 每日食物", ""])
    for day in result.get("days", []):
        foods = day.get("foods", [])
        if foods:
            details = "；".join(
                f"{str(item.get('name', '未命名'))}"
                + (f"（{item['portion']}）" if item.get("portion") else "")
                + (f" {item['calories']} 千卡" if item.get("calories") is not None else "")
                for item in foods
            )
        else:
            details = "无饮食记录"
        lines.append(f"- {day['date']}：{details}")
    totals = result.get("totals", {})
    averages = result.get("averages", {})
    lines.extend(["", "## 小结", "", f"- 区间合计摄入：{totals.get('calories_in', 0):.1f} 千卡",
                  f"- 区间合计运动：{totals.get('calories_out', 0):.1f} 千卡",
                  f"- 每日平均摄入：{averages.get('calories_in', 0):.1f} 千卡",
                  f"- 每日平均净值：{averages.get('net_calories', 0):.1f} 千卡"])
    if averages.get("weight_7d_average") is not None:
        lines.append(f"- 体重七日移动平均（区间均值）：{averages['weight_7d_average']:.1f} 千克")
    return "\n".join(lines) + "\n"


def get_store(root: str | os.PathLike[str] | None = None) -> WellnessStore:
    """构造使用当前 NIGHTSHIFT_HOME 的仓库。"""
    return WellnessStore(root)


# 模块级薄包装让 HTTP 路由和脚本无需管理仓库实例；每次调用都会重新读取
# NIGHTSHIFT_HOME，因此测试和单进程切换数据目录也不会复用旧路径。
def load_profile() -> dict[str, Any] | None:
    return get_store().load_profile()


def save_profile(profile: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    return get_store().save_profile(profile, **kwargs)


def update_profile(patch: Mapping[str, Any]) -> dict[str, Any]:
    return get_store().update_profile(patch)


def load_preferences() -> dict[str, Any] | None:
    return get_store().load_preferences()


def save_preferences(preferences: Mapping[str, Any]) -> dict[str, Any]:
    return get_store().save_preferences(preferences)


def update_preferences(patch: Mapping[str, Any]) -> dict[str, Any]:
    return get_store().update_preferences(patch)


def load_day(day: str) -> list[dict[str, Any]]:
    return get_store().load_day(day)


def save_day(day: str, document: Mapping[str, Any]) -> list[dict[str, Any]]:
    return get_store().save_day(day, document)


def load_entries(day: str) -> list[dict[str, Any]]:
    return get_store().list_entries(day)


def save_entries(day: str, entries: Mapping[str, Any] | list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return get_store().save_entries(day, entries)


def create_entry(day: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    return get_store().create_entry(day, entry)


def get_entry(day: str, entry_id: str) -> dict[str, Any] | None:
    return get_store().get_entry(day, entry_id)


def update_entry(day: str, entry_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    return get_store().update_entry(day, entry_id, patch)


def delete_entry(day: str, entry_id: str) -> bool:
    return get_store().delete_entry(day, entry_id)


def load_plan(day: str) -> dict[str, Any] | None:
    return get_store().load_plan(day)


def save_plan(day: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    return get_store().save_plan(day, plan)


def update_plan(day: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    return get_store().update_plan(day, patch)


def range_summary(from_day: str, to_day: str, *, target: float | None = None,
                  profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return get_store().range_summary(from_day, to_day, target=target, profile=profile)


def report(period: str = "week", end: str | None = None, format: str = "json",
           *, target: float | None = None,
           profile: Mapping[str, Any] | None = None) -> dict[str, Any] | str:
    return get_store().report(period, end, format, target=target, profile=profile)


range_report = report
