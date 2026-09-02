"""context.py 的测试：transcript 上下文 token 读取与模型上限。"""

import json
from pathlib import Path

import pytest

from nightshift.context import context_limit_for, read_codex_context, read_context_tokens


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))


def line(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def usage_rec(inp: int, cache_read: int = 0, cache_creation: int = 0) -> str:
    return line(
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": inp,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                }
            },
        }
    )


def make_transcript(path: Path) -> None:
    """8 行 transcript：user / 无 usage 的 assistant / 带 usage / 坏 JSON 行……"""
    lines = [
        line({"type": "user", "message": {"role": "user", "content": "你好"}}),
        line({"type": "assistant", "message": {"role": "assistant", "content": []}}),
        usage_rec(100, 50, 10),  # 160
        "{这行是坏 JSON，",
        line({"type": "user", "message": {"role": "user", "content": "继续"}}),
        usage_rec(200),  # 200 ← 最后一条带 usage 的
        line({"type": "user", "message": {"role": "user", "content": "再来"}}),
        line({"type": "assistant", "message": {"role": "assistant", "content": []}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_context_tokens_last_usage(tmp_path):
    path = tmp_path / "transcript.jsonl"
    make_transcript(path)
    assert read_context_tokens(path) == 200  # 最后一条 usage 的和，不是第一条


def test_read_context_tokens_missing_keys_count_as_zero(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        line(
            {
                "type": "assistant",
                "message": {"usage": {"input_tokens": 77}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_context_tokens(path) == 77


def test_read_context_tokens_big_file_over_tail_window(tmp_path):
    path = tmp_path / "big.jsonl"
    filler = "x" * 4096
    with open(path, "w", encoding="utf-8") as f:
        for _ in range(200):  # 约 800 KB > 512 KB 的尾部窗口
            f.write(filler + "\n")
        f.write(usage_rec(12345) + "\n")
    assert path.stat().st_size > 512 * 1024
    assert read_context_tokens(path) == 12345


def test_read_context_tokens_skips_api_error_and_synthetic_records(tmp_path):
    """审查 D7：CC 的 API 错误记录也是 type=assistant（isApiErrorMessage=true、
    model="<synthetic>"、usage 全零）；排在最后不能把水位读成 0，要读前一条真回执。"""
    path = tmp_path / "t.jsonl"
    err = {
        "type": "assistant", "isApiErrorMessage": True, "error": "server_error",
        "message": {"model": "<synthetic>", "role": "assistant",
                    "content": [{"type": "text", "text": "API Error: Server error mid-response."}],
                    "usage": {"input_tokens": 0, "output_tokens": 0,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
    }
    zero = {"type": "assistant", "message": {"model": "<synthetic>", "usage": {"input_tokens": 0}}}
    path.write_text("\n".join([usage_rec(5, 400000, 3000), line(err), line(zero)]) + "\n", encoding="utf-8")
    assert read_context_tokens(path) == 403005
    # 只有合成记录：如实 None，不是 0
    path.write_text(line(err) + "\n", encoding="utf-8")
    assert read_context_tokens(path) is None


def test_read_context_tokens_no_usage(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(line({"type": "user", "message": {"content": "hi"}}) + "\n",
                    encoding="utf-8")
    assert read_context_tokens(path) is None


def test_read_context_tokens_missing_file(tmp_path):
    assert read_context_tokens(tmp_path / "不存在.jsonl") is None


# ---------- 总review三 H1：Codex rollout 的 token_count 记录 ----------


def token_count_rec(total: int, window: int | None, inp: int = 0, out: int = 0) -> str:
    info = {
        "last_token_usage": {
            "input_tokens": inp,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": out,
            "reasoning_output_tokens": 0,
            "total_tokens": total,
        },
    }
    if window is not None:
        info["model_context_window"] = window
    return line({"type": "event_msg", "payload": {"type": "token_count", "info": info}})


def make_rollout(path: Path) -> None:
    """混几条别的 event_msg/response_item，最后一条才是真正的 token_count。"""
    lines = [
        line({"type": "response_item", "payload": {"type": "message", "role": "user"}}),
        line({"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "…"}}),
        token_count_rec(19227, 828400, inp=19170, out=57),  # 第一条 token_count
        "{坏 JSON 行，",
        line({"type": "response_item", "payload": {"type": "message", "role": "assistant"}}),
        token_count_rec(30500, 828400, inp=30400, out=100),  # 最后一条 ← 该读这条
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_codex_context_last_token_count(tmp_path):
    path = tmp_path / "rollout.jsonl"
    make_rollout(path)
    assert read_codex_context(path) == (30500, 828400)


def test_read_codex_context_skips_null_info(tmp_path):
    """info: null 的那条不携带真实水位，要跳过读它前面那条。"""
    path = tmp_path / "rollout.jsonl"
    null_info = line({"type": "event_msg", "payload": {"type": "token_count", "info": None}})
    path.write_text(
        "\n".join([token_count_rec(19227, 828400), null_info]) + "\n", encoding="utf-8"
    )
    assert read_codex_context(path) == (19227, 828400)


def test_read_codex_context_total_tokens_missing_falls_back_to_sum(tmp_path):
    """total_tokens 缺失时用 input_tokens + output_tokens 补。"""
    path = tmp_path / "rollout.jsonl"
    rec = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": 100, "output_tokens": 23},
                "model_context_window": 200000,
            },
        },
    }
    path.write_text(line(rec) + "\n", encoding="utf-8")
    assert read_codex_context(path) == (123, 200000)


def test_read_codex_context_big_file_over_tail_window(tmp_path):
    """跟 Claude 那套一样：尾窗扫不到要回退全文扫。"""
    path = tmp_path / "big_rollout.jsonl"
    filler = "x" * 4096
    with open(path, "w", encoding="utf-8") as f:
        for _ in range(200):  # 约 800 KB > 512 KB 的尾部窗口
            f.write(filler + "\n")
        f.write(token_count_rec(55555, 828400) + "\n")
    assert path.stat().st_size > 512 * 1024
    assert read_codex_context(path) == (55555, 828400)


def test_read_codex_context_no_token_count_record(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        line({"type": "response_item", "payload": {"type": "message"}}) + "\n",
        encoding="utf-8",
    )
    assert read_codex_context(path) is None


def test_read_codex_context_missing_file(tmp_path):
    assert read_codex_context(tmp_path / "不存在.jsonl") is None


def test_context_limit_for():
    config = {
        "models": {"m-big": {"context_limit": 500000}},
        "default_context_limit": 200000,
    }
    assert context_limit_for("m-big", config) == 500000
    assert context_limit_for("m-unknown", config) == 200000


def test_context_limit_for_codex_no_default_fallback():
    """S6.1 B3：Codex（或任何非 claude runner）查不到时必须如实返回 None，
    不能悄悄套 Claude 的 default_context_limit 冒充一个已知水位——那正是
    "Codex 没有稳定水位来源"这件事本身要传达的信息。"""
    config = {
        "models": {"m-big": {"context_limit": 500000}},  # 顶层 Claude 兼容表
        "default_context_limit": 200000,
        "runners": {
            "codex": {"models": {"gpt-5.6-luna": {"context_limit": None}}},
        },
    }
    # Codex 自己的模型表里配了 null：如实返回 None
    assert context_limit_for("gpt-5.6-luna", config, runner="codex") is None
    # Codex 模型表里压根没有这个模型：同样 None，不退到顶层 Claude 表/default
    assert context_limit_for("m-unknown", config, runner="codex") is None
    # Claude 默认 runner 行为不变
    assert context_limit_for("m-big", config, runner="claude") == 500000
    assert context_limit_for("m-unknown", config, runner="claude") == 200000
