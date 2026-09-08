"""食物热量估算适配器。

本模块只负责把文字或图片交给所选的后端，并把后端响应收敛成同一种
JSON 结构。它不负责保存日记录，也不保存上传的原图；调用方应在估算前
把图片压缩到临时目录，并在请求完成后自行清理。
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "DEFAULT_SETTINGS", "EstimatorError", "SettingsError", "MAX_IMAGE_BYTES",
    "ESTIMATE_JSON_SCHEMA", "ESTIMATION_RESULT_SCHEMA", "CODEX_OUTPUT_SCHEMA",
    "settings_path", "load_settings", "save_settings", "update_settings",
    "public_settings", "validate_result", "build_prompt", "estimate",
    "estimate_food", "ClaudeEstimator", "CodexEstimator",
    "OpenAICompatibleEstimator", "MockEstimator",
]


MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT = 120.0

DEFAULT_SETTINGS: dict[str, Any] = {
    "provider": "mock",
    "retain_photos": False,
    "providers": {
        "claude": {"model": "", "timeout_s": DEFAULT_TIMEOUT},
        "codex": {"model": "gpt-5.6-luna", "timeout_s": DEFAULT_TIMEOUT},
        "openai_compatible": {
            "model": "", "timeout_s": DEFAULT_TIMEOUT, "base_url": "", "api_key": "",
        },
        "mock": {"model": "mock", "timeout_s": DEFAULT_TIMEOUT},
    },
}

_PROVIDERS = frozenset(DEFAULT_SETTINGS["providers"])
_PROVIDER_FIELDS = {
    "claude": frozenset({"model", "timeout_s"}),
    "codex": frozenset({"model", "timeout_s"}),
    "openai_compatible": frozenset({"model", "timeout_s", "base_url", "api_key"}),
    "mock": frozenset({"model", "timeout_s"}),
}
_ITEM_FIELDS = (
    "name", "portion", "unit", "kcal_low", "kcal_high", "kcal_best",
    "protein_g", "carbs_g", "fat_g", "confidence", "note",
)

# Claude CLI 的结构化输出契约。provider、model 和 elapsed_ms 由本地适配器
# 补齐，模型只需生成 items/questions；这样 CLI 不会把说明文字包装成结果。
ESTIMATE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "portion": {"type": "string"},
                    "unit": {"type": "string"},
                    "kcal_low": {"type": "number", "minimum": 0},
                    "kcal_high": {"type": "number", "minimum": 0},
                    "kcal_best": {"type": "number", "minimum": 0},
                    "protein_g": {"type": "number", "minimum": 0},
                    "carbs_g": {"type": "number", "minimum": 0},
                    "fat_g": {"type": "number", "minimum": 0},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "note": {"type": "string"},
                },
                "required": list(_ITEM_FIELDS),
            },
        },
        "questions": {"type": "array", "items": {"type": "string"}},
        "provider": {"type": "string"},
        "model": {"type": "string"},
        "elapsed_ms": {"type": "integer", "minimum": 0},
    },
    "required": ["items", "questions"],
}

# Codex 的严格输出契约只让模型负责 items/questions；本地补齐其余元数据。
# Codex CLI 要求顶层 properties 中的每个键都出现在 required 中。
CODEX_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": ESTIMATE_JSON_SCHEMA["properties"]["items"],
        "questions": ESTIMATE_JSON_SCHEMA["properties"]["questions"],
    },
    "required": ["items", "questions"],
}

# 便于调用方按“结果 schema”语义发现同一份不可变契约。
ESTIMATION_RESULT_SCHEMA = ESTIMATE_JSON_SCHEMA

_ERROR_DETAIL_LIMIT = 300
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w])/(?:[^ \t\r\n\"'`<>]+)")
_WINDOWS_PATH_RE = re.compile(r"(?<![\w])(?:[A-Za-z]:\\|\\\\)[^ \t\r\n\"'`<>]+")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_SESSION_ID_RE = re.compile(
    r"(?i)(\b(?:session|thread|turn|request|trace|conversation|run)[ _-]?id)\b"
    r"\s*[\"']?\s*(?:[:=]\s*|\s+)(?:[\"'][^\"']*[\"']|[A-Za-z0-9._:-]+)"
)


class EstimatorError(Exception):
    """估算后端不可用或返回内容不符合约定。"""


class SettingsError(EstimatorError):
    """估算设置文件无效。"""


def _home(root: str | os.PathLike[str] | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser()
    value = os.environ.get("NIGHTSHIFT_HOME")
    return Path(value).expanduser() if value else Path.home() / ".nightshift"


def settings_path(root: str | os.PathLike[str] | None = None) -> Path:
    """返回 ``NIGHTSHIFT_HOME/wellness/settings.json`` 的路径。"""
    return _home(root) / "wellness" / "settings.json"


def _copy_defaults() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False))


def _merge_settings(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SettingsError("settings 顶层必须是对象")
    unknown_top = set(value) - {"provider", "providers", "retain_photos"}
    if unknown_top:
        raise SettingsError(f"settings 包含未知字段：{', '.join(sorted(unknown_top))}")
    result = _copy_defaults()
    if "provider" in value:
        result["provider"] = value["provider"]
    if "retain_photos" in value:
        if not isinstance(value["retain_photos"], bool):
            raise SettingsError("settings.retain_photos 必须是布尔值")
        result["retain_photos"] = value["retain_photos"]
    providers = value.get("providers", {})
    if not isinstance(providers, Mapping):
        raise SettingsError("settings.providers 必须是对象")
    for name, config in providers.items():
        if name not in _PROVIDERS:
            raise SettingsError(f"不支持的估算 provider：{name}")
        if not isinstance(config, Mapping):
            raise SettingsError(f"settings.providers.{name} 必须是对象")
        cleaned = {key: item for key, item in config.items()
                   if key in _PROVIDER_FIELDS[name]}
        unknown = set(config) - _PROVIDER_FIELDS[name] - {"has_api_key"}
        if unknown:
            raise SettingsError(
                f"settings.providers.{name} 包含未知字段：{', '.join(sorted(unknown))}"
            )
        for key in ("model", "base_url", "api_key"):
            if key in cleaned and not isinstance(cleaned[key], str):
                raise SettingsError(f"settings.providers.{name}.{key} 必须是字符串")
        if "timeout_s" in cleaned:
            timeout = cleaned["timeout_s"]
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(float(timeout)) or not 1 <= float(timeout) <= 600):
                raise SettingsError(
                    f"settings.providers.{name}.timeout_s 必须在 1 到 600 秒之间"
                )
            cleaned["timeout_s"] = float(timeout)
        result["providers"][name].update(cleaned)
    if result["provider"] not in _PROVIDERS:
        raise SettingsError(f"不支持的估算 provider：{result['provider']}")
    return result


def load_settings(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """读取估算设置；文件不存在时返回不含私人信息的默认设置。"""
    path = settings_path(root)
    if not path.exists():
        return _copy_defaults()
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"估算设置 JSON 损坏：{exc}") from exc
    return _merge_settings(value)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    """在目标目录内临时写入后原子替换设置文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_settings(settings: Mapping[str, Any], root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """校验并原子保存设置，返回完整内部设置（调用者不要直接回传）。"""
    normalized = _merge_settings(settings)
    _atomic_json(settings_path(root), normalized)
    return normalized


def update_settings(patch: Mapping[str, Any], root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """合并保存设置；未提交 api_key 时保留现有密钥。"""
    if not isinstance(patch, Mapping):
        raise SettingsError("settings patch 必须是对象")
    current = load_settings(root)
    merged: dict[str, Any] = dict(current)
    merged["providers"] = {key: dict(value) for key, value in current["providers"].items()}
    for key, value in patch.items():
        if key == "providers":
            if not isinstance(value, Mapping):
                raise SettingsError("settings.providers 必须是对象")
            for provider, config in value.items():
                if provider not in _PROVIDERS or not isinstance(config, Mapping):
                    raise SettingsError(f"settings.providers.{provider} 无效")
                merged["providers"][provider].update(dict(config))
        else:
            merged[key] = value
    return save_settings(merged, root)


def public_settings(settings: Mapping[str, Any] | None = None, root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """生成可返回给网页的设置，永远不暴露 api_key 内容。"""
    value = _merge_settings(settings) if settings is not None else load_settings(root)
    result = {
        "provider": value["provider"],
        "retain_photos": value["retain_photos"],
        "providers": {},
    }
    for provider, config in value["providers"].items():
        item = dict(config)
        key = item.pop("api_key", "")
        item["has_api_key"] = bool(key)
        result["providers"][provider] = item
    return result


def _number(value: Any, field: str, *, minimum: float = 0.0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EstimatorError(f"估算结果字段 {field} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise EstimatorError(f"估算结果字段 {field} 数值无效")
    return result


def validate_result(value: Mapping[str, Any], *, provider: str | None = None, model: str | None = None,
                    elapsed_ms: int | None = None) -> dict[str, Any]:
    """校验并规范后端 JSON；缺字段或错误类型会抛出 ``EstimatorError``。"""
    if not isinstance(value, Mapping):
        raise EstimatorError("估算结果必须是 JSON 对象")
    items = value.get("items")
    questions = value.get("questions", [])
    if not isinstance(items, list):
        raise EstimatorError("估算结果缺少 items 数组")
    if not isinstance(questions, list) or not all(isinstance(q, str) for q in questions):
        raise EstimatorError("估算结果 questions 必须是字符串数组")
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise EstimatorError(f"估算结果 items[{index}] 必须是对象")
        missing = [field for field in _ITEM_FIELDS if field not in item]
        if missing:
            raise EstimatorError(f"估算结果 items[{index}] 缺少字段：{', '.join(missing)}")
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise EstimatorError(f"估算结果 items[{index}] 的食物名称无效")
        if not all(isinstance(item[field], str) for field in ("portion", "unit", "note")):
            raise EstimatorError(f"估算结果 items[{index}] 的文字字段无效")
        result_item = {field: item[field] for field in ("name", "portion", "unit", "note")}
        for field in ("kcal_low", "kcal_high", "kcal_best", "protein_g", "carbs_g", "fat_g"):
            result_item[field] = _number(item[field], field)
        result_item["confidence"] = _number(item["confidence"], "confidence", maximum=1.0)
        if result_item["kcal_low"] > result_item["kcal_high"]:
            raise EstimatorError(f"估算结果 items[{index}] 热量区间反向")
        if not result_item["kcal_low"] <= result_item["kcal_best"] <= result_item["kcal_high"]:
            raise EstimatorError(f"估算结果 items[{index}] kcal_best 不在区间内")
        normalized_items.append(result_item)
    result: dict[str, Any] = {
        "items": normalized_items,
        "questions": list(questions),
        "provider": provider if provider is not None else value.get("provider", ""),
        "model": model if model is not None else value.get("model", ""),
        "elapsed_ms": elapsed_ms if elapsed_ms is not None else value.get("elapsed_ms", 0),
    }
    if not isinstance(result["provider"], str) or not isinstance(result["model"], str):
        raise EstimatorError("估算结果 provider/model 必须是字符串")
    result["elapsed_ms"] = int(_number(result["elapsed_ms"], "elapsed_ms"))
    if "error" in value and value["error"]:
        result["error"] = str(value["error"])
    return result


def _failure(provider: str, model: str, elapsed_ms: int, message: str) -> dict[str, Any]:
    return {"items": [], "questions": [message], "provider": provider, "model": model,
            "elapsed_ms": max(0, int(elapsed_ms)), "error": message}


def _image_path(path: str | os.PathLike[str] | None) -> Path | None:
    if path is None:
        return None
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise EstimatorError(f"图片路径无效：{path}") from exc
    if not resolved.is_file():
        raise EstimatorError("图片路径不是普通文件")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise EstimatorError("无法读取图片大小") from exc
    if size > MAX_IMAGE_BYTES:
        raise EstimatorError("图片超过 10 MB 大小上限")
    return resolved


def build_prompt(text: str | None = None, image_path: str | os.PathLike[str] | None = None,
                 profile: Mapping[str, Any] | None = None) -> str:
    """构造要求中文、区间估算并严格返回 JSON 的提示词。"""
    if not text and image_path is None:
        raise EstimatorError("文字或图片至少提供一个")
    lines = [
        "你是中国饮食记录助手。请只输出一个 JSON 对象，不要 Markdown 围栏或解释。",
        "JSON 必须含 items 数组和 questions 数组；items 每项必须含 name、portion、unit、",
        "kcal_low、kcal_high、kcal_best、protein_g、carbs_g、fat_g、confidence、note。",
        "按中国常见份量（碗、份、杯、两）估算，给热量区间而非伪精确值；不确定内容放入 questions。",
        "食物名称使用中文，confidence 为 0 到 1 之间的小数。",
    ]
    if profile:
        allowed = {key: profile[key] for key in ("weight_kg", "target_weight_kg", "goal", "sex") if key in profile}
        if allowed:
            lines.append("用户档案摘要：" + json.dumps(allowed, ensure_ascii=False, separators=(",", ":")))
    if text:
        lines.append("用户描述：" + text.strip())
    if image_path is not None:
        lines.append("待识别图片绝对路径：" + str(Path(image_path).expanduser().resolve()))
    return "\n".join(lines)


def _json_candidate(raw: str) -> Any:
    """从直接 JSON、Claude 包装响应或围栏/夹杂文本中提取 JSON。"""
    raw = raw.strip()
    candidates = [raw]
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidates.insert(0, "\n".join(lines).strip())
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            for marker in ("{", "["):
                start = candidate.find(marker)
                if start < 0:
                    continue
                try:
                    value, _ = decoder.raw_decode(candidate[start:])
                    return value
                except json.JSONDecodeError:
                    continue
    raise EstimatorError("模型没有返回可解析的 JSON")


def _model_json(raw: str) -> Mapping[str, Any]:
    # 常见 CLI 会在最终 JSON 前打印进度行；从每个对象起点尝试，优先选出
    # 真正带 items 的对象，避免把进度事件误当成估算结果。
    candidates = [raw]
    for marker in ("{", "["):
        offset = 0
        while True:
            offset = raw.find(marker, offset)
            if offset < 0:
                break
            candidates.append(raw[offset:])
            offset += 1
    def unwrap(value: Any, depth: int = 0) -> Mapping[str, Any]:
        if depth > 4:
            raise EstimatorError("模型 JSON 外壳嵌套过深")
        if isinstance(value, str):
            return unwrap(_json_candidate(value), depth + 1)
        if not isinstance(value, Mapping):
            raise EstimatorError("模型 JSON 顶层必须是对象")
        if "items" in value:
            return value
        if value.get("is_error") is True:
            detail = value.get("result", value.get("error", value.get("message", "未知错误")))
            raise EstimatorError("估算程序返回错误：" + str(detail)[:500])
        nested_error: EstimatorError | None = None
        nested_text = ""
        for key in ("result", "output", "structured_output"):
            if key in value and isinstance(value[key], (str, Mapping)):
                try:
                    return unwrap(value[key], depth + 1)
                except EstimatorError as exc:
                    nested_error = exc
                    if isinstance(value[key], str):
                        nested_text = value[key]
                    continue
        content = value.get("content")
        if isinstance(content, list):
            texts = [str(part.get("text", "")) for part in content if isinstance(part, Mapping)]
            if any(texts):
                return unwrap("".join(texts), depth + 1)
        if nested_error is not None:
            if nested_text.strip():
                raise EstimatorError(
                    "模型没有返回结构化 JSON；原文摘要：" + nested_text.strip()[:500]
                )
            raise nested_error
        raise EstimatorError("模型 JSON 顶层缺少 items")

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            value = _json_candidate(candidate)
            return unwrap(value)
        except EstimatorError as exc:
            if "估算程序返回错误" in str(exc):
                raise
            last_error = exc
    raise EstimatorError(str(last_error or "模型 JSON 顶层必须是对象"))


def _safe_error_detail(detail: Any, input_text: str | None = None) -> str:
    """截取并脱敏子进程错误摘要，避免回显提示词和本机标识。"""
    text = str(detail or "")
    prompt = (input_text or "").strip()
    if prompt:
        text = text.replace(prompt, "<提示词已省略>")
    text = _SESSION_ID_RE.sub(r"\1=<会话标识已省略>", text)
    text = _UUID_RE.sub("<会话标识已省略>", text)
    text = _ABSOLUTE_PATH_RE.sub("<路径已省略>", text)
    text = _WINDOWS_PATH_RE.sub("<路径已省略>", text).strip()
    if len(text) > _ERROR_DETAIL_LIMIT:
        text = "…" + text[-(_ERROR_DETAIL_LIMIT - 1):]
    return text


def _run(command: list[str], timeout: float, env: Mapping[str, str] | None = None,
         input_text: str | None = None) -> str:
    child_env = dict(os.environ)
    child_env.pop("CLAUDECODE", None)
    if env:
        child_env.update(env)
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            check=False, env=child_env, input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        detail = exc.stderr or exc.stdout or ""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        safe_detail = _safe_error_detail(detail, input_text)
        suffix = f"；输出摘要：{safe_detail}" if safe_detail else ""
        raise EstimatorError(f"估算超时（超过 {timeout:g} 秒）{suffix}") from exc
    except OSError as exc:
        raise EstimatorError(f"无法启动估算程序：{_safe_error_detail(exc)}") from exc
    output = completed.stdout or ""
    if completed.returncode != 0 and not output.strip():
        detail = _safe_error_detail(completed.stderr, input_text)
        raise EstimatorError(f"估算程序失败：{detail or f'退出码 {completed.returncode}'}")
    return output


class ClaudeEstimator:
    """通过非交互 ``claude -p`` 估算。"""

    provider = "claude"

    def __init__(self, config: Mapping[str, Any] | None = None):
        self.config = dict(config or {})

    def run(self, prompt: str, image_path: str | os.PathLike[str] | None = None) -> Mapping[str, Any]:
        image = _image_path(image_path)
        model = str(self.config.get("model") or "")
        timeout = float(self.config.get("timeout_s", DEFAULT_TIMEOUT))
        if image is not None and str(image) not in prompt:
            prompt = prompt.rstrip() + "\n待识别图片绝对路径：" + str(image)
        command = [
            os.environ.get("NIGHTSHIFT_CLAUDE_BIN", "claude"), "-p",
            "--output-format", "json", "--permission-mode", "dontAsk",
            "--permission-prompts", "none", "--safe-mode",
            "--restricted", "--no-session-persistence", "--no-chrome",
            "--strict-mcp-config", "--input-format", "text",
            "--json-schema", json.dumps(ESTIMATE_JSON_SCHEMA, ensure_ascii=False, separators=(",", ":")),
        ]
        if model:
            command.extend(["--model", model])
        if image is not None:
            # 上传图在数据目录的 tmp/ 中，通常不在当前工作目录下。
            # 只额外授权该图所在目录，可用工具仍只有 Read。
            command.extend(["--add-dir", str(image.parent)])
        command.extend(["--tools", "Read" if image is not None else ""])
        # 提示词从 stdin 读，避免 --add-dir/--tools 的可变参数
        # 把提示词误当成目录或工具名。
        return _model_json(_run(command, timeout, input_text=prompt))


class CodexEstimator:
    """通过 ``codex exec`` 的只读沙箱估算。"""

    provider = "codex"

    def __init__(self, config: Mapping[str, Any] | None = None):
        self.config = dict(config or {})

    def run(self, prompt: str, image_path: str | os.PathLike[str] | None = None) -> Mapping[str, Any]:
        image = _image_path(image_path)
        model = str(self.config.get("model") or "gpt-5.6-luna")
        timeout = float(self.config.get("timeout_s", DEFAULT_TIMEOUT))
        schema_file = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".codex-output-schema-", suffix=".json",
            delete=False,
        )
        schema_path = Path(schema_file.name)
        try:
            # schema 文件只写公开的结果契约，不混入提示词、图片路径或其他运行数据。
            with schema_file:
                json.dump(CODEX_OUTPUT_SCHEMA, schema_file, ensure_ascii=False, separators=(",", ":"))
                schema_file.write("\n")
                schema_file.flush()
                os.fsync(schema_file.fileno())
            command = [
                os.environ.get("NIGHTSHIFT_CODEX_BIN", "codex"), "exec",
                "--sandbox", "read-only", "--ephemeral", "--color", "never",
                "--skip-git-repo-check", "-C",
                str(image.parent if image is not None else Path(tempfile.gettempdir())),
            ]
            if model:
                command.extend(["-m", model])
            command.extend(["--output-schema", str(schema_path)])
            # 提示词固定从 stdin 读，避免 -i 的可变文件参数把末尾
            # 提示词误解为另一张图。
            command.append("-")
            if image is not None:
                command.extend(["-i", str(image)])
            return _model_json(_run(command, timeout, input_text=prompt))
        finally:
            # 无论模型成功、返回异常 JSON 还是超时，都不留下 schema 临时文件。
            try:
                schema_path.unlink()
            except FileNotFoundError:
                pass


class OpenAICompatibleEstimator:
    """调用 Chat Completions 兼容接口，图片以内联 data URL 发送。"""

    provider = "openai_compatible"

    def __init__(self, config: Mapping[str, Any] | None = None):
        self.config = dict(config or {})

    def run(self, prompt: str, image_path: str | os.PathLike[str] | None = None) -> Mapping[str, Any]:
        image = _image_path(image_path)
        base_url = str(self.config.get("base_url") or "").rstrip("/")
        if not base_url:
            raise EstimatorError("openai_compatible 缺少 base_url")
        if not base_url.endswith("/chat/completions"):
            base_url += "/chat/completions" if base_url.endswith("/v1") else "/v1/chat/completions"
        model = str(self.config.get("model") or "")
        if not model:
            raise EstimatorError("openai_compatible 缺少 model")
        content: Any = prompt
        if image is not None:
            path = image
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}]
        payload = {"model": model, "messages": [{"role": "user", "content": content}],
                   "temperature": 0, "response_format": {"type": "json_object"}}
        headers = {"Content-Type": "application/json"}
        api_key = str(self.config.get("api_key", ""))
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = urllib.request.Request(
            base_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=float(self.config.get("timeout_s", DEFAULT_TIMEOUT))) as response:
                outer = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise EstimatorError(f"估算接口返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EstimatorError(f"估算接口连接失败：{exc}") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise EstimatorError("估算接口返回了无效 JSON") from exc
        try:
            content_value = outer["choices"][0]["message"]["content"]
            if isinstance(content_value, list):
                content_value = "".join(str(part.get("text", "")) if isinstance(part, Mapping) else str(part) for part in content_value)
            return _model_json(str(content_value))
        except (KeyError, IndexError, TypeError) as exc:
            raise EstimatorError("估算接口响应缺少 choices.message.content") from exc


class MockEstimator:
    """固定回放驱动；用于离线测试和网页人工冒烟。"""

    provider = "mock"

    def __init__(self, config: Mapping[str, Any] | None = None):
        self.config = dict(config or {})

    def run(self, prompt: str, image_path: str | os.PathLike[str] | None = None) -> Mapping[str, Any]:
        return {
            "items": [{"name": "米饭", "portion": "一碗", "unit": "碗", "kcal_low": 220,
                       "kcal_high": 300, "kcal_best": 260, "protein_g": 5.0,
                       "carbs_g": 58.0, "fat_g": 0.5, "confidence": 0.75, "note": "按常见家用饭碗估算"}],
            "questions": [],
        }


def estimate(text: str | None = None, image_path: str | os.PathLike[str] | None = None,
             profile: Mapping[str, Any] | None = None, *, provider: str | None = None,
             settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """运行估算并始终返回统一结构；异常转为可读 ``error``，不会向网页炸出堆栈。"""
    started = time.monotonic()
    chosen = provider or ""
    model = ""
    try:
        image = _image_path(image_path)
        config = _merge_settings(settings) if settings is not None else load_settings()
        chosen = chosen or str(config["provider"])
        if chosen not in _PROVIDERS:
            raise EstimatorError(f"不支持的估算 provider：{chosen}")
        provider_config = config["providers"].get(chosen, {})
        model = str(provider_config.get("model") or ("gpt-5.6-luna" if chosen == "codex" else chosen))
        prompt = build_prompt(text, image, profile)
        drivers = {"claude": ClaudeEstimator, "codex": CodexEstimator,
                   "openai_compatible": OpenAICompatibleEstimator, "mock": MockEstimator}
        raw = drivers[chosen](provider_config).run(prompt, image)
        elapsed = int((time.monotonic() - started) * 1000)
        return validate_result(raw, provider=chosen, model=model, elapsed_ms=elapsed)
    except (EstimatorError, ValueError, OSError, UnicodeError) as exc:
        # 已读到设置或已经选定驱动时保留 provider/model，便于网页明确提示
        # 是哪一个后端失败；不得因为走默认 settings 就把诊断元数据清空。
        if not model and settings and isinstance(settings, Mapping):
            try:
                chosen = chosen or str(settings.get("provider", ""))
                model = str(settings.get("providers", {}).get(chosen, {}).get("model", ""))
            except AttributeError:
                pass
        return _failure(chosen, model, int((time.monotonic() - started) * 1000), str(exc))


estimate_food = estimate
