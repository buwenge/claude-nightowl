"""读 transcript 算上下文 token。

Claude 方法（设计稿 §4.3，调研 §2.6 已验证）：找 transcript JSONL 里最后一条
`type == "assistant"` 且 `message.usage` 存在的记录，
input_tokens + cache_read_input_tokens + cache_creation_input_tokens
即该轮实际携带的上下文量。

Codex 方法（总review三 H1，工头 9/2 核实）：Codex 的 rollout JSONL 每一轮
结束都会落一条 `{"type": "event_msg", "payload": {"type": "token_count",
"info": {...}}}`，`info.last_token_usage.total_tokens` 就是这一轮实际携带
的上下文量，`info.model_context_window` 是这个模型的上限——跟 Claude 不同，
Codex 没有稳定的模型表可查（`context_limit_for` 对它恒定 None），只能从
rollout 自己带的这个数现读。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["context_limit_for", "read_codex_context", "read_context_tokens"]

# 先只读文件末尾这么多字节倒着扫，找不到再全文扫
_TAIL_BYTES = 512 * 1024


def _read_tail_or_full(path: Path, scan):
    """尾窗优先、全文回退的行扫描：Claude/Codex 两套 transcript 格式共用
    这套文件 IO（H1：不许各写一套）。小文件（≤ `_TAIL_BYTES`）直接整份扫；
    大文件先扫最后 `_TAIL_BYTES` 字节，扫不到再退回整份重扫一遍（兜底命中的
    那条记录恰好落在尾窗之前）。`scan(lines)` 接一份已按行切好、未反转的
    字符串列表，命中就返回结果，找不到返回 None。
    """
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size > _TAIL_BYTES:
        with open(path, "rb") as f:
            f.seek(-_TAIL_BYTES, os.SEEK_END)
            tail = f.read()
        found = scan(tail.decode("utf-8", errors="replace").splitlines())
        if found is not None:
            return found
    with open(path, "rb") as f:
        return scan(f.read().decode("utf-8", errors="replace").splitlines())


def _usage_from_line(line: str) -> dict | None:
    """这一行若是带 usage 的 assistant 记录则返回 usage dict，否则 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    # CC 在 API 出错 / 本地合成消息时也落 type=assistant 的记录
    # （isApiErrorMessage=true、message.model="<synthetic>"），usage 全零——
    # 那不是一次真实携带上下文的回执，排在最后会把水位读成 0（卡片 0%、
    # Stop 时 over_warn_line=False）。跳过，读它前面那条真回执。
    if record.get("isApiErrorMessage"):
        return None
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None
    if message.get("model") == "<synthetic>":
        return None
    return message["usage"]


def _sum_usage(usage: dict) -> int:
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )


def _scan_lines(lines) -> int | None:
    for line in reversed(lines):
        usage = _usage_from_line(line)
        if usage is None:
            continue
        total = _sum_usage(usage)
        if total > 0:  # 真回执连 system prompt 都有 token，全零只能是合成记录
            return total
    return None


def read_context_tokens(transcript_path: str | os.PathLike) -> int | None:
    """transcript 里最后一条 assistant usage 的 token 和；找不到返回 None。"""
    return _read_tail_or_full(Path(transcript_path), _scan_lines)


def _codex_token_count_from_line(line: str) -> tuple[int, int | None] | None:
    """这一行若是 Codex rollout 里带 token_count 的 event_msg 则返回
    `(total_tokens, model_context_window)`，否则 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    # info 有时是 null（施工令原料 2）——那一条不携带真实水位，跳过读前一条。
    if not isinstance(info, dict):
        return None
    last = info.get("last_token_usage")
    if not isinstance(last, dict):
        return None
    total = last.get("total_tokens")
    if total is None:  # total_tokens 缺失时用 input+output 补
        total = int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)
    window = info.get("model_context_window")
    return int(total), (int(window) if isinstance(window, (int, float)) else None)


def _scan_codex_lines(lines) -> tuple[int, int | None] | None:
    for line in reversed(lines):
        found = _codex_token_count_from_line(line)
        if found is not None:
            return found
    return None


def read_codex_context(
    transcript_path: str | os.PathLike,
) -> tuple[int, int | None] | None:
    """Codex rollout 里最后一条 token_count 记录的 `(total_tokens,
    model_context_window)`；找不到（文件不存在/没有这类记录）返回 None。"""
    return _read_tail_or_full(Path(transcript_path), _scan_codex_lines)


def context_limit_for(model: str, config: dict, runner: str = "claude") -> int | None:
    """这个 runner 的这个模型的上下文上限。

    S6.1 B3：必须按 `runner` 对应的模型表查，不能只看顶层 `config.models`
    ——那张表只是 Claude 的兼容视图，Codex 模型（`context_limit: null`，没有
    稳定水位来源）根本不在里面，查不到就会静默套 default_context_limit，
    伪装成一个已知水位。只有 Claude 查不到时才退到 default_context_limit；
    Codex（或任何非 claude runner）查不到就如实返回 None，不编数字。
    """
    from .store import runner_config  # 延迟导入：store.py 反过来 import 本模块的 context_limit_for

    rc = runner_config(config).get(runner) or {}
    models = rc.get("models") or {}
    if model in models:
        return models[model].get("context_limit")
    if runner == "claude":
        return config.get("default_context_limit")
    return None
