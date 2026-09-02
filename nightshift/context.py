"""读 transcript 算上下文 token。

方法（设计稿 §4.3，调研 §2.6 已验证）：找 transcript JSONL 里最后一条
`type == "assistant"` 且 `message.usage` 存在的记录，
input_tokens + cache_read_input_tokens + cache_creation_input_tokens
即该轮实际携带的上下文量。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["context_limit_for", "read_context_tokens"]

# 先只读文件末尾这么多字节倒着扫，找不到再全文扫
_TAIL_BYTES = 512 * 1024


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
    path = Path(transcript_path)
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size > _TAIL_BYTES:
        with open(path, "rb") as f:
            f.seek(-_TAIL_BYTES, os.SEEK_END)
            tail = f.read()
        found = _scan_lines(tail.decode("utf-8", errors="replace").splitlines())
        if found is not None:
            return found
    with open(path, "rb") as f:
        found = _scan_lines(f.read().decode("utf-8", errors="replace").splitlines())
    return found


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
