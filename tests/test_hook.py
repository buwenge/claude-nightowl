"""hook 接收端的测试：子进程喂夹具，逐步核对 status.json 的状态机。"""

import json
import multiprocessing
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nightshift import store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_WARN_TEXT = (
    "[nightshift] 上下文已 {ctx_k}k / {limit_k}k，到警戒线了。现在收尾："
    "①把已完成/未完成/下一步写进 {handover_path}，末行写 NEXT: continue 或 NEXT: done；"
    "②未提交的改动 commit；③然后停下，不要再开新的活。调度器会按交接开下一班。"
)

CONFIG = {
    "tmux_session": "claude",
    "window_prefix": "ns:",
    "claude_bin": "claude",
    "probe_model": "claude-haiku-4-5-20251001",
    "display_tz_offset_hours": 8,
    "memory_max_bytes": 2147483648,
    "projects": {"demo": "/home/user/projects/demo"},
    "models": {
        "claude-fable-5": {"context_limit": 500000, "usage_label": "Fable"},
        "claude-sonnet-5": {"context_limit": 500000, "usage_label": "Sonnet"},
        "claude-haiku-4-5-20251001": {"context_limit": 200000},
    },
    "default_context_limit": 200000,
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
    "chain": {"max_windows": 3, "on_no_handover": "continue"},
    "context_warn_text": DEFAULT_WARN_TEXT,
    "chain_template": "{task}\n\n这是第 {shift} 班。上一班交接如下：\n{handover}\n\n先核对交接里说的状态再动手。",
}


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    store.atomic_write_json(tmp_path / "config.json", CONFIG)
    return tmp_path


def make_task(**over) -> str:
    task = {
        "title": "hook 测试任务",
        "project": "demo",
        "model": "claude-fable-5",
        "effort": "high",
        "run_at": "2026-08-27T18:00:00Z",
        "task_text": "正文",
        "prompt_final": "完整提示词",
    }
    task.update(over)
    return store.create_task(task, CONFIG)


def run_hook(task_id: str, event: str, payload: str):
    env = dict(os.environ)
    env["NIGHTSHIFT_HOME"] = os.environ["NIGHTSHIFT_HOME"]
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "nightshift.hook", task_id, event],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def run_codex_hook(task_id: str, event: str, payload: str):
    """Codex 的调用形状：task id 走 NIGHTOWL_TASK_ID 环境变量，不是位置参数。"""
    env = dict(os.environ)
    env["NIGHTSHIFT_HOME"] = os.environ["NIGHTSHIFT_HOME"]
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["NIGHTOWL_TASK_ID"] = task_id
    return subprocess.run(
        [sys.executable, "-m", "nightshift.hook", "--codex", event],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def make_task_codex(**over) -> str:
    task = {
        "title": "codex hook 测试任务",
        "project": "demo",
        "runner": "codex",
        "model": "gpt-5.6-luna",
        "effort": "high",
        "run_at": "2026-08-27T18:00:00Z",
        "task_text": "正文",
        "prompt_final": "完整提示词",
    }
    task.update(over)
    codex_config = {
        **CONFIG,
        "runners": {
            "claude": {"models": CONFIG["models"], "efforts": CONFIG["efforts"]},
            "codex": {"models": {"gpt-5.6-luna": {"context_limit": None}},
                      "efforts": ["low", "medium", "high", "xhigh"]},
        },
    }
    store.atomic_write_json(store.home() / "config.json", codex_config)
    return store.create_task(task, codex_config)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def background_lines() -> list[str]:
    return [
        line
        for line in fixture("hook_events_background.jsonl").splitlines()
        if line.strip()
    ]


def test_event_sequence_state_machine():
    task_id = make_task()
    bg = background_lines()
    subagent_start = bg[1]     # SubagentStart
    subagent_stop = bg[3]      # SubagentStop（带 background_tasks 但不该被它用）
    stop_running = bg[4]       # Stop，background_tasks 非空

    # ① UserPromptSubmit → working，turns=1，坐实 session/transcript
    up = json.loads(fixture("hook_userpromptsubmit.json"))
    proc = run_hook(task_id, "UserPromptSubmit", fixture("hook_userpromptsubmit.json"))
    assert proc.returncode == 0
    assert proc.stdout == ""  # stdout 永远不输出
    status = store.read_status(task_id)
    assert status["state"] == "working"
    assert status["turns"] == 1
    assert status["session_id"] == up["session_id"]
    assert status["transcript_path"] == up["transcript_path"]
    # R2：夹具里 permission_mode=default（auto 被回落的实锤形状），要照实进 status
    assert status["permission_mode"] == "default"
    assert status["permission_mode"] == up["permission_mode"]
    assert "last_event_at" in status

    # ② SubagentStart → subagents_running=1（R3：按 agent_id 记集合）
    proc = run_hook(task_id, "SubagentStart", subagent_start)
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["subagents_running"] == 1
    assert status["agent_type"] == "general-purpose"
    assert status["subagents"] == [json.loads(subagent_start)["agent_id"]]

    # ③ SubagentStop → 减回 0，不低于 0
    proc = run_hook(task_id, "SubagentStop", subagent_stop)
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["subagents_running"] == 0
    assert status["subagents"] == []

    # ④ Stop 且 background_tasks 有 running → waiting_background
    stop_payload = json.loads(stop_running)
    proc = run_hook(task_id, "Stop", stop_running)
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["state"] == "waiting_background"
    assert status["background_tasks"] == stop_payload["background_tasks"]
    assert len(status["background_tasks"]) == 1
    assert status["background_tasks"][0]["status"] == "running"
    assert status["last_message"] == "已派出"
    assert status["context_tokens"] is None  # 夹具里的 transcript 不存在

    # ⑤ Stop 且 background_tasks 空 → idle
    proc = run_hook(task_id, "Stop", fixture("hook_stop_idle.json"))
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["state"] == "idle"
    assert status["background_tasks"] == []
    assert status["last_message"] == "好"

    # ⑥ SessionEnd → exited，记 reason
    proc = run_hook(task_id, "SessionEnd", fixture("hook_sessionend.json"))
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["state"] == "exited"
    assert status["exit_reason"] == "other"

    # 全程 events.log 有痕
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "UserPromptSubmit" in events
    assert "SessionEnd" in events


def test_subagents_never_negative():
    task_id = make_task()
    bg = background_lines()
    run_hook(task_id, "SubagentStop", bg[3])  # 没先 Start 就 Stop
    assert store.read_status(task_id)["subagents_running"] == 0


def test_stop_computes_context_tokens(tmp_path):
    task_id = make_task(
        guards={"session_pct_max": 80, "context_limit_tokens": 1000}
    )
    # 手工造一个 transcript（同 test_context 的形状）
    lines = [
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 150,
                        "cache_read_input_tokens": 30,
                        "cache_creation_input_tokens": 20,
                    }
                },
            }
        ),
        "坏 JSON 行",
        json.dumps({"type": "assistant", "message": {"content": []}}),
    ]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = json.loads(fixture("hook_stop_idle.json"))
    payload["transcript_path"] = str(transcript)
    proc = run_hook(task_id, "Stop", json.dumps(payload, ensure_ascii=False))
    assert proc.returncode == 0 and proc.stdout == ""

    status = store.read_status(task_id)
    assert status["context_tokens"] == 200  # 150+30+20
    assert status["context_pct"] == 20  # 200 / guards.context_limit_tokens=1000


def test_post_tool_use_refreshes_every_20(tmp_path):
    task_id = make_task(
        guards={"session_pct_max": 80, "context_limit_tokens": 1000}
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 500,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = json.loads(fixture("hook_userpromptsubmit.json"))
    payload["transcript_path"] = str(transcript)
    text = json.dumps(payload, ensure_ascii=False)

    for _ in range(19):
        run_hook(task_id, "PostToolUse", text)
    status = store.read_status(task_id)
    assert status["tool_calls"] == 19
    assert status["context_tokens"] is None  # 还没到 20 不刷新

    run_hook(task_id, "PostToolUse", text)
    status = store.read_status(task_id)
    assert status["tool_calls"] == 20
    assert status["context_tokens"] == 500
    assert status["context_pct"] == 50


# ---------- S3①：PostToolUse 回注（上下文 / 额度）----------


def make_transcript(path: Path, tokens: int) -> str:
    """手工造 transcript：最后一条 assistant usage 的 token 和可控。"""
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": tokens,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return json.dumps(
        dict(json.loads(fixture("hook_userpromptsubmit.json")),
             transcript_path=str(path)),
        ensure_ascii=False,
    )


def write_quota(session=10, week=10, per_model=None, age_minutes=0) -> None:
    """往数据目录写一份新鲜（或指定年龄）的 quota.json。"""
    fetched_at = (
        datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.atomic_write_json(
        store.home() / "quota.json",
        {
            "usage": {
                "session_pct": session,
                "week_all_pct": week,
                "per_model": per_model or {},
            },
            "fetched_at": fetched_at,
        },
    )


def write_quota_dual(session=10, week=10, per_model=None, age_minutes=0) -> None:
    """往数据目录写一份 S6 双分片形状的 quota.json（只填 claude 那一片，
    codex 留空），用来验证 `_read_fresh_usage` 真的在读双分片而不是只兼容
    一期旧顶层形状。"""
    fetched_at = (
        datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.atomic_write_json(
        store.home() / "quota.json",
        {
            "claude": {
                "usage": {
                    "session_pct": session,
                    "week_all_pct": week,
                    "per_model": per_model or {},
                },
                "fetched_at": fetched_at,
                "error": None,
            },
            "codex": {},
        },
    )


def _resets_text(dt: datetime) -> str:
    """把 datetime 格成 /usage 那种 `Aug 27, 6:40pm (UTC)` 文本，喂给
    `quota.resets_in_minutes` 用。"""
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{dt.strftime('%b')} {dt.day}, {hour12}:{dt.minute:02d}{ampm} (UTC)"


def test_post_tool_use_injects_context_warn_at_20_and_40(tmp_path):
    task_id = make_task(
        guards={
            "session_pct_max": 80,
            "context_warn_tokens": 400,
            "context_limit_tokens": 2000,
        }
    )
    payload = make_transcript(tmp_path / "transcript.jsonl", 1200)  # ≥ 400 线
    handover = str(store.task_dir(task_id) / "handover-1.md")

    for _ in range(19):  # 线下不许出声
        proc = run_hook(task_id, "PostToolUse", payload)
        assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["tool_calls"] == 19
    assert status.get("context_warned_at") is None

    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次：恰一个 JSON
    assert proc.returncode == 0
    assert proc.stdout.endswith("\n")
    lines = proc.stdout.splitlines()
    assert len(lines) == 1
    out = json.loads(lines[0])["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    ctx = out["additionalContext"]
    assert "1k" in ctx and "2k" in ctx  # {ctx_k} / {limit_k} 渲染后的数字
    assert handover in ctx  # {handover_path} 渲染后的绝对路径
    status = store.read_status(task_id)
    assert status["context_warned_at"]
    assert status["context_warn_count"] == 1
    assert status["handover_path"] == handover
    first_warned_at = status["context_warned_at"]
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "回注上下文提醒 #1（1200）" in events

    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 40 次：仍在线上 → 再注
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert handover in ctx
    status = store.read_status(task_id)
    assert status["context_warn_count"] == 2
    assert status["context_warned_at"] == first_warned_at  # 首次时间不变


def test_post_tool_use_below_line_never_injects(tmp_path):
    task_id = make_task(
        guards={
            "session_pct_max": 80,
            "context_warn_tokens": 400,
            "context_limit_tokens": 2000,
        }
    )
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)  # 远低于线
    for _ in range(20):
        proc = run_hook(task_id, "PostToolUse", payload)
        assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["tool_calls"] == 20
    assert status["context_tokens"] == 100
    assert status.get("context_warned_at") is None


def test_post_tool_use_custom_warn_text(tmp_path):
    task_id = make_task(
        guards={
            "session_pct_max": 80,
            "context_warn_tokens": 400,
            "context_limit_tokens": 2000,
            "context_warn_text": "自定义提醒：{ctx_k}k/{limit_k}k，写到 {handover_path}",
        }
    )
    payload = make_transcript(tmp_path / "transcript.jsonl", 1200)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(
        f"自定义提醒：1k/2k，写到 {store.task_dir(task_id) / 'handover-1.md'}"
    )


def test_post_tool_use_quota_warn(tmp_path):
    task_id = make_task(
        guards={
            "session_pct_max": 80,
            "weekly_pct_max": 95,
            "context_warn_tokens": 100000,  # 上下文别捣乱
            "context_limit_tokens": 200000,
        }
    )
    write_quota(session=85, week=10)  # 五小时 85 ≥ 80
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次：只有五小时暂停一段
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "五小时额度只剩 15%" in ctx and "线 20%" in ctx
    assert "ScheduleWakeup" in ctx and "50 分钟" in ctx
    assert "上下文已" not in ctx  # 上下文没到线，只有额度这一段
    status = store.read_status(task_id)
    assert status["quota_pause_count"] == 1
    assert status["quota_paused_until"]


def test_post_tool_use_quota_warn_dual_slice(tmp_path):
    """二次返修阻断二反例①：quota.json 是 S6 真实双分片形状
    （`{"claude": {...}, "codex": {...}}`）时，第 20 次 PostToolUse 仍要
    正常回注五小时暂停——旧代码直接读顶层 `data["fetched_at"]`/
    `data["usage"]`，双分片下这两个键根本不存在，会静默读不到任何数据。"""
    task_id = make_task(
        guards={
            "session_pct_max": 80,
            "weekly_pct_max": 95,
            "context_warn_tokens": 100000,
            "context_limit_tokens": 200000,
        }
    )
    write_quota_dual(session=85, week=10)  # 五小时 85 ≥ 80
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "五小时额度只剩 15%" in ctx and "线 20%" in ctx
    status = store.read_status(task_id)
    assert status["quota_pause_count"] == 1
    assert status["quota_paused_until"]


def test_post_tool_use_stale_quota_ignored(tmp_path):
    task_id = make_task(
        guards={
            "session_pct_max": 80,
            "weekly_pct_max": 95,
            "context_warn_tokens": 100000,
            "context_limit_tokens": 200000,
        }
    )
    write_quota(session=85, age_minutes=61)  # 61 分钟 > 2 × 30 分钟 → 当没有
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(20):
        proc = run_hook(task_id, "PostToolUse", payload)
        assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status.get("quota_warned_at") is None


def test_post_tool_use_quota_freshness_floor_at_5_minute_refresh(tmp_path):
    """总review F4：新鲜度上限是 max(2 × quota_refresh_minutes, 30) 分钟，
    不是单纯 2 倍——刷新间隔缩到 5 分钟后，20 分钟前的数据仍要算新鲜
    （在 30 分钟下限之内），35 分钟前的才算过期。"""
    cfg = dict(CONFIG)
    cfg["scheduler"] = {"quota_refresh_minutes": 5}
    store.atomic_write_json(store.home() / "config.json", cfg)
    task_id = make_task(guards={
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    write_quota(session=85, age_minutes=20)  # 20 分钟前：在 30 分钟下限内，仍新鲜
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "五小时额度只剩 15%" in ctx

    task_id2 = make_task(guards={
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    write_quota(session=85, age_minutes=35)  # 35 分钟前：超过 30 分钟下限，算过期
    payload2 = make_transcript(tmp_path / "transcript2.jsonl", 100)
    for _ in range(20):
        proc = run_hook(task_id2, "PostToolUse", payload2)
    assert proc.stdout == ""
    assert store.read_status(task_id2).get("quota_pause_count") is None


# ---------- 总review F6：缓存额度早于本轮刷新时刻时不重复注入五小时暂停 ----------


def test_post_tool_use_quota_pause_skipped_when_cached_usage_predates_refresh(tmp_path):
    """模型闹钟醒来后第一次工具调用触发刷新，quota.json 若还是刷新前抓的
    （session_resets 已经过去）——这份数据早于本轮刷新，不该再注一次
    "停下定闹钟"，让模型白等一轮。"""
    task_id = make_task(guards={
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    store.atomic_write_json(store.home() / "quota.json", {
        "usage": {
            "session_pct": 90, "week_all_pct": 10, "per_model": {},
            "session_resets": _resets_text(past),
        },
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次
    assert proc.stdout == ""
    status = store.read_status(task_id)
    assert "quota_paused_until" not in status
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "缓存额度早于刷新时刻，跳过五小时暂停判定，等下一次刷新" in events


def test_post_tool_use_quota_pause_fires_when_resets_still_in_future(tmp_path):
    task_id = make_task(guards={
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    store.atomic_write_json(store.home() / "quota.json", {
        "usage": {
            "session_pct": 90, "week_all_pct": 10, "per_model": {},
            "session_resets": _resets_text(future),
        },
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "五小时额度只剩 10%" in ctx
    assert store.read_status(task_id)["quota_paused_until"]


# ---------- 总review F7(a)：guards 缺 key/None 回退 config.guards ----------


def test_post_tool_use_quota_check_falls_back_to_config_guards_when_task_has_none():
    """任务完全没配 guards（网页编辑清空、或老任务）——CONFIG 顶层已有
    guards.session_pct_max=80/weekly_pct_max=95，以前 hook._quota_check
    直接整段 return None（跟 quota.check_guards 的口径不一致），现在要
    回退到 config 照样判定。"""
    task_id = make_task()  # 不传 guards
    write_quota(session=85, week=10)  # 85 ≥ config.guards.session_pct_max(80)
    payload = make_transcript(store.task_dir(task_id) / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次
    assert proc.stdout, "guards 全靠 config 兜底，也该判定到线"
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "五小时额度只剩 15%" in ctx
    assert store.read_status(task_id)["quota_paused_until"]


def test_post_tool_use_quota_check_task_guard_overrides_config_guard():
    """任务自己配了 session_pct_max，就该用任务的，不回退 config 那份。"""
    task_id = make_task(guards={"session_pct_max": 96})  # 高于 config 的 80，几乎不会到线
    write_quota(session=85, week=10)  # 85 < 96：不到线；但 ≥ config 的 80
    payload = make_transcript(store.task_dir(task_id) / "transcript.jsonl", 100)
    for _ in range(20):
        proc = run_hook(task_id, "PostToolUse", payload)
    assert proc.stdout == "", "任务自己配了线就该用它，不该被 config 的更低线拦住"
    assert "quota_paused_until" not in store.read_status(task_id)


def test_post_tool_use_context_and_quota_one_json_two_paragraphs(tmp_path):
    task_id = make_task(
        guards={
            "session_pct_max": 80,
            "weekly_pct_max": 95,
            "context_warn_tokens": 400,
            "context_limit_tokens": 2000,
        }
    )
    write_quota(session=85, week=10, per_model={"Fable": 96})  # 三条线全中
    payload = make_transcript(tmp_path / "transcript.jsonl", 1200)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次
    lines = proc.stdout.splitlines()
    assert len(lines) == 1  # 两种提醒也只打一个 JSON
    ctx = json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]
    assert "上下文已 1k / 2k" in ctx
    assert "额度只剩" in ctx
    assert "\n\n" in ctx  # 两段用空行隔开
    status = store.read_status(task_id)
    assert status["context_warn_count"] == 1
    assert status["quota_warn_count"] == 1


def test_stop_writes_over_warn_line_without_stdout(tmp_path):
    task_id = make_task(
        guards={
            "session_pct_max": 80,
            "context_warn_tokens": 400,
            "context_limit_tokens": 2000,
        }
    )
    payload = json.dumps(
        dict(json.loads(fixture("hook_stop_idle.json")),
             transcript_path=str(tmp_path / "transcript.jsonl")),
        ensure_ascii=False,
    )
    make_transcript(tmp_path / "transcript.jsonl", 1200)
    proc = run_hook(task_id, "Stop", payload)  # Stop 永远沉默
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["over_warn_line"] is True

    make_transcript(tmp_path / "transcript.jsonl", 100)
    run_hook(task_id, "Stop", payload)
    assert store.read_status(task_id)["over_warn_line"] is False


# ---------- 并发 hook 进程（R2：计数不许丢）----------


def _run_hook_proc(task_id: str, event: str, payload: str) -> None:
    proc = run_hook(task_id, event, payload)
    assert proc.returncode == 0
    assert proc.stdout == ""


def _spawn_and_join(procs) -> None:
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs)


def test_concurrent_post_tool_use_counts():
    """8 个 hook 进程同时打点，tool_calls 一次不丢。"""
    task_id = make_task()
    payload = '{"hook_event_name":"PostToolUse"}'
    procs = [
        multiprocessing.Process(
            target=_run_hook_proc, args=(task_id, "PostToolUse", payload)
        )
        for _ in range(8)
    ]
    _spawn_and_join(procs)
    assert store.read_status(task_id)["tool_calls"] == 8


def test_concurrent_subagents_start_stop():
    """R3：8 个进程用 4 个不同 agent_id 各 Start/Stop 一次，无论进程以什么
    顺序跑完，最后 subagents 归空、subagents_running 归零（按 id 记集合后
    不再有旧计数器"最后一笔是 Start"的抖动）。"""
    task_id = make_task()
    agent_ids = [f"a374269b6bde46c2{i}" for i in range(4)]
    procs = [
        multiprocessing.Process(
            target=_run_hook_proc,
            args=(
                task_id,
                event,
                json.dumps({"hook_event_name": event, "agent_id": agent_id}),
            ),
        )
        for agent_id in agent_ids
        for event in ("SubagentStart", "SubagentStop")
    ]
    _spawn_and_join(procs)
    status = store.read_status(task_id)
    assert status["subagents"] == []
    assert status["subagents_running"] == 0


def test_subagent_stop_before_start_stays_zero():
    """R3 乱序兜底：同一 agent_id 的 Stop 先于 Start 串行到达（hook 进程
    被调度延迟的实况），最后也必须归零——迟到的 Start 不许把 agent 复活。"""
    task_id = make_task()
    stop = json.dumps({"hook_event_name": "SubagentStop", "agent_id": "a374269b6bde46c22"})
    start = json.dumps({"hook_event_name": "SubagentStart", "agent_id": "a374269b6bde46c22"})

    _run_hook_proc(task_id, "SubagentStop", stop)  # 先到：agent 还没记上，不许出错也不许欠账
    status = store.read_status(task_id)
    assert status["subagents_running"] == 0
    assert status["subagents"] == []

    _run_hook_proc(task_id, "SubagentStart", start)  # 后到：这枚 Start 必须被墓碑拦下
    status = store.read_status(task_id)
    assert status["subagents"] == []
    assert status["subagents_running"] == 0


# ---------- config 缺失时的兜底（R4）----------


def test_stop_without_config_still_updates(tmp_path):
    """config.json 缺失算不出上限：Stop 照常落盘，context_pct 留空，events 留痕。"""
    task_id = make_task()  # guards 里没有 context_limit_tokens → 会去读 config
    (store.home() / "config.json").unlink()
    assert not (store.home() / "config.json").exists()

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 150,
                        "cache_read_input_tokens": 30,
                        "cache_creation_input_tokens": 20,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = json.loads(fixture("hook_stop_idle.json"))
    payload["transcript_path"] = str(transcript)
    proc = run_hook(task_id, "Stop", json.dumps(payload, ensure_ascii=False))
    assert proc.returncode == 0 and proc.stdout == ""

    status = store.read_status(task_id)
    assert status["state"] == "idle"
    assert status["context_tokens"] == 200  # transcript 照常统计
    assert status["context_pct"] is None
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "算不出上限" in events


def test_precompact_only_logs():
    task_id = make_task()
    before = store.read_status(task_id)
    proc = run_hook(task_id, "PreCompact", '{"hook_event_name":"PreCompact"}')
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["state"] == "scheduled"  # 状态不动
    assert status["updated_at"] == before["updated_at"]  # 也不碰 status
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "PreCompact" in events


def test_unknown_event_only_logs():
    task_id = make_task()
    proc = run_hook(task_id, "Notification", '{"message":"hi"}')
    assert proc.returncode == 0 and proc.stdout == ""
    assert store.read_status(task_id)["state"] == "scheduled"
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "Notification" in events


# ---------- S7：审稿意见文件协议（Stop → review-<round>.md） ----------


def make_review_task(*, runner: str = "claude", round_: int = 1, **over) -> str:
    """建一个 role=review 的任务：先走 store.create_task 的正常校验拿到一个
    合法的 worktree=true + review.enabled=true 任务，再手工把 role/round
    补上（create_task 本身不认这两个字段，是流水线调度器写的）。"""
    config = dict(CONFIG)
    if runner == "codex":
        config["runners"] = {
            "claude": {"models": CONFIG["models"], "efforts": CONFIG["efforts"]},
            "codex": {"models": {"gpt-5.6-luna": {"context_limit": None}},
                      "efforts": ["low", "medium", "high", "xhigh"]},
        }
        store.atomic_write_json(store.home() / "config.json", config)
    task = {
        "title": "审稿任务",
        "project": "demo",
        "model": "claude-fable-5",
        "effort": "high",
        "run_at": "2026-08-27T18:00:00Z",
        "task_text": "正文",
        "prompt_final": "完整提示词",
        "review": {
            "enabled": True, "runner": runner,
            "model": "gpt-5.6-luna" if runner == "codex" else "claude-fable-5",
            "effort": "high",
        },
    }
    task.update(over)
    task_id = store.create_task(task, config)
    data = store.load_task(task_id)
    data["role"] = "review"
    data["round"] = round_
    store.atomic_write_json(store.task_dir(task_id) / "task.json", data)
    return task_id


def test_stop_review_writes_review_file_and_verdict_done():
    task_id = make_review_task()
    text = "看过了，改动都对，测试也过。\n\nNEXT: done"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["state"] == "idle"
    assert status["review_verdict"] == "done"
    assert status["review_recorded_round"] == 1
    review_path = Path(status["review_file"])
    assert review_path == store.task_dir(task_id) / "review-1.md"
    assert review_path.read_text(encoding="utf-8") == text


def test_stop_review_fix_verdict_and_file_content():
    task_id = make_review_task(round_=2)
    text = "有个 bug：边界条件没处理。\n改法：加个 if。\n\nNEXT: fix"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "fix"
    assert status["review_file"] == str(store.task_dir(task_id) / "review-2.md")
    assert Path(status["review_file"]).read_text(encoding="utf-8") == text


def test_stop_review_missing_next_defaults_to_fix():
    task_id = make_review_task()
    text = "看完了，大体没问题，但是忘了写 NEXT 那一行。"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "fix"  # 协议缺失，保守按 fix
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "协议缺失" in events


def test_stop_review_background_task_running_does_not_conclude():
    """总review二 G9：审稿班用 Bash 后台跑长命令并结束这一回合——这次 Stop
    没有 NEXT 不是"协议缺失"，不该按 fix 假退回。只落 waiting_background，
    不写 review 文件、不记 verdict，等后台跑完真正的 Stop 再下结论。"""
    task_id = make_review_task()
    payload = {
        "last_assistant_message": "在后台跑测试，先不下结论。",
        "background_tasks": [
            {"id": "bg1", "status": "running"},
        ],
    }
    proc = run_hook(task_id, "Stop", json.dumps(payload))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["state"] == "waiting_background"
    assert "review_verdict" not in status
    assert "review_file" not in status
    assert not (store.task_dir(task_id) / "review-1.md").exists()
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "审稿班后台任务仍在跑，这次 Stop 不当结论" in events


def test_stop_review_background_task_finished_still_concludes():
    """background_tasks 里没有 running 项（已完成/失败）不该被这条新分支
    拦住——照常按协议解析 verdict。"""
    task_id = make_review_task()
    payload = {
        "last_assistant_message": "后台命令跑完了，看过了。\n\nNEXT: done",
        "background_tasks": [
            {"id": "bg1", "status": "finished"},
        ],
    }
    proc = run_hook(task_id, "Stop", json.dumps(payload))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["state"] == "idle"
    assert status["review_verdict"] == "done"


def test_stop_review_control_turn_takes_priority_over_background_task():
    """控制 turn（这里用"我来看"hold）优先级排在背景任务检查之前——即使
    背景任务还在跑，"我来看"也该按控制语义停到 held，不能被截胡成
    waiting_background。"""
    task_id = make_review_task()
    store.update_status(
        task_id, review_awaiting_verdict=False, review_control_kind="hold",
    )
    payload = {
        "last_assistant_message": "好，我先停下来。",
        "background_tasks": [{"id": "bg1", "status": "running"}],
    }
    proc = run_hook(task_id, "Stop", json.dumps(payload))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["state"] == "held"
    assert "review_verdict" not in status
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "控制 turn" in events


def test_stop_review_empty_message_defaults_to_fix():
    task_id = make_review_task()
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": ""}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "fix"
    assert Path(status["review_file"]).is_file()


def test_stop_review_pending_verdict():
    task_id = make_review_task()
    text = "额度快到线了，还没看完。\n\nNEXT: pending"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    assert store.read_status(task_id)["review_verdict"] == "pending"


def test_stop_review_idempotent_duplicate_stop_does_not_overwrite():
    """CC 对一次残缺响应会静默重试（同一 turn 两次 Stop）：第二次不该覆盖
    已经记过的 verdict/文件内容（moving CLAUDE.md 条目 84 的同款风险）。"""
    task_id = make_review_task()
    first = "第一次的意见。\n\nNEXT: done"
    proc1 = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": first}))
    assert proc1.returncode == 0
    assert store.read_status(task_id)["review_verdict"] == "done"

    second = "重试后不一样的意见，不该生效。\n\nNEXT: fix"
    proc2 = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": second}))
    assert proc2.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "done"  # 第一次的结果没被覆盖
    review_path = Path(status["review_file"])
    assert review_path.read_text(encoding="utf-8") == first
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "忽略重复 Stop" in events


def test_stop_review_codex_uses_same_protocol():
    task_id = make_review_task(runner="codex")
    text = "Codex 审稿意见。\n\nNEXT: fix"
    proc = run_codex_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "fix"
    assert status["state"] == "idle"
    assert Path(status["review_file"]).read_text(encoding="utf-8") == text


def test_stop_build_role_never_writes_review_file():
    """role=build（缺省）的普通任务走原生 Stop 分支，不产生 review 文件。"""
    task_id = make_task()
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": "干完了"}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert "review_verdict" not in status
    assert not (store.task_dir(task_id) / "review-1.md").exists()


# ---------- S7.1 阻断二：并发安全 + pending 可继续 + 控制 turn 隔离 ----------


def test_stop_review_pending_then_done_records_new_verdict():
    """pending 之后同一轮真正的 done 必须能正常记录——旧写法拿
    review_recorded_round==round 当"已处理过"的全部依据，pending 自己也会
    把这个字段设成当前 round，导致后续同轮的 done 被误判成重复 Stop 而
    忽略掉。"""
    task_id = make_review_task()
    pending_text = "额度快到线了，还没看完。\n\nNEXT: pending"
    proc1 = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": pending_text}))
    assert proc1.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "pending"
    assert status["review_recorded_round"] == 1
    assert status["review_verdict_final"] is False

    done_text = "额度刷新后接着看完了，都对。\n\nNEXT: done"
    proc2 = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": done_text}))
    assert proc2.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "done"  # 没有被 pending 的"已记录"挡住
    assert status["review_verdict_final"] is True
    assert Path(status["review_file"]).read_text(encoding="utf-8") == done_text
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "忽略重复 Stop" not in events


def test_stop_review_pending_then_fix_records_new_verdict():
    """同上，pending → fix 同样要能正常记录（不止 done 一条路径）。"""
    task_id = make_review_task()
    pending_text = "还没看完。\n\nNEXT: pending"
    run_hook(task_id, "Stop", json.dumps({"last_assistant_message": pending_text}))
    fix_text = "接着看完了，有个 bug 要退回。\n\nNEXT: fix"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": fix_text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "fix"
    assert status["review_verdict_final"] is True
    assert Path(status["review_file"]).read_text(encoding="utf-8") == fix_text


def test_stop_review_done_then_pending_is_ignored_as_duplicate():
    """反过来：done（终态）之后同一轮又来一次 Stop（哪怕内容是 pending），
    必须仍按重复 Stop 忽略——不可覆盖的只应该是 done/fix 这类终态，不能
    因为"新内容不是 pending"就放宽成允许覆盖已确认的结果。"""
    task_id = make_review_task()
    done_text = "看完了，都对。\n\nNEXT: done"
    run_hook(task_id, "Stop", json.dumps({"last_assistant_message": done_text}))
    later_text = "改主意了。\n\nNEXT: pending"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": later_text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "done"  # 终态没被覆盖
    assert Path(status["review_file"]).read_text(encoding="utf-8") == done_text
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "忽略重复 Stop" in events


def test_stop_review_control_turn_does_not_record_verdict():
    """保活/我来看这类"不要求正式 verdict"的回复（发之前调用方会把
    review_awaiting_verdict 落成 False）：Stop 只清运行期字段，不碰
    review_verdict/review_recorded_round，即使回复正文没有合法 NEXT 也不会
    被判协议缺失、保守转成 fix。"""
    task_id = make_review_task()
    store.update_status(task_id, review_awaiting_verdict=False, state="held")
    text = "收到，我在等，先不动。"  # 没有 NEXT 行的普通控制回复
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert "review_verdict" not in status
    assert "review_recorded_round" not in status
    assert not (store.task_dir(task_id) / "review-1.md").exists()
    assert status["state"] == "held"  # 不瞎猜复原状态，原样保留
    assert status["review_awaiting_verdict"] is False  # 不自作主张翻回 True
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "控制 turn" in events


def test_stop_review_control_turn_after_verdict_does_not_overwrite():
    """已经有过一次真正 verdict 之后，控制 turn（比如返工新一轮开始前的
    额外保活戳）不能把已经记录的 verdict 冲掉。"""
    task_id = make_review_task()
    done_text = "看完了，都对。\n\nNEXT: done"
    run_hook(task_id, "Stop", json.dumps({"last_assistant_message": done_text}))
    store.update_status(task_id, review_awaiting_verdict=False)
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": "收到"}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "done"
    assert status["review_recorded_round"] == 1


def test_stop_review_control_turn_hold_restores_held_even_when_working():
    """S7.2 阻断五.1：hold 打进一个 working 的 reviewer 后，Stop 回来账面
    不该继续停在 working——`review_control_kind="hold"` 要求控制 turn 分支
    把 state 显式转成 held，不管发送前是 working/idle 哪一种。"""
    task_id = make_review_task()
    store.update_status(
        task_id, review_awaiting_verdict=False, review_control_kind="hold",
        state="working",
    )
    text = "收到，我在等，先不动。"  # 没有 NEXT 行的普通控制回复
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["state"] == "held"
    assert status.get("held_reason")
    assert "review_verdict" not in status  # 仍然只是控制 turn，不解析 verdict
    assert "review_control_kind" not in status  # 消费后清掉，不留残影


def test_stop_review_control_turn_keepalive_kind_keeps_state_unchanged():
    """对照：control_kind="keepalive" 时状态保持原样不动（keepalive 本来
    只会戳 held/waiting_background，收到控制回复不该变）。"""
    task_id = make_review_task()
    store.update_status(
        task_id, review_awaiting_verdict=False, review_control_kind="keepalive",
        state="waiting_background",
    )
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": "还在"}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["state"] == "waiting_background"
    assert "review_control_kind" not in status


def test_stop_review_file_write_failure_leaves_verdict_unset_and_retryable():
    """S7.2 阻断四反例：review 文件写失败（这里用"目标路径已经是个目录"
    制造一个不依赖 root 权限也一定失败的 OSError）时，verdict/final 不能
    被提前钉死——status 除了一个会被清掉的"正在处理"标记之外没有任何
    字段被改过；同一轮后续 Stop（含 CC 的静默重试）应该能重新尝试并
    正常写入。"""
    task_id = make_review_task()
    review_path = store.task_dir(task_id) / "review-1.md"
    review_path.mkdir()  # 让 os.replace(tmp, path) 必然因为类型不对而失败
    text = "看过了，改动都对。\n\nNEXT: done"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0  # hook 自己吞掉异常，不炸整个进程
    status = store.read_status(task_id)
    assert "review_verdict" not in status
    assert "review_verdict_final" not in status
    assert "review_recorded_round" not in status
    assert "review_file_claim" not in status  # 失败后 claim 标记已清掉
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "写审稿文件失败" in events
    assert "本轮可重试" in events

    review_path.rmdir()  # 排除写入障碍，模拟"重试成功"
    proc2 = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc2.returncode == 0
    status2 = store.read_status(task_id)
    assert status2["review_verdict"] == "done"
    assert status2["review_verdict_final"] is True
    assert Path(status2["review_file"]).read_text(encoding="utf-8") == text


def test_stop_review_fresh_claim_still_blocks_concurrent_stop():
    """S7.3 阻断一反例（未过期对照组）：手工放一个"刚刚"claim（claimed_at
    是现在），紧接着送一个合法 NEXT: done 的 Stop——必须仍按 duplicate
    处理，不写文件、不落 verdict。锁住"没过期时不能被新的过期判断误放行"，
    防止这次改动引入相反方向的新 bug。"""
    task_id = make_review_task()
    now = store.utc_now_iso()
    store.update_status(
        task_id, review_file_claim={"round": 1, "token": "old-token", "claimed_at": now},
    )
    text = "本该被挡住的意见。\n\nNEXT: done"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert "review_verdict" not in status
    assert not (store.task_dir(task_id) / "review-1.md").is_file()
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "忽略重复 Stop" in events


def test_stop_review_stale_claim_after_claim_before_write_crash_recovers():
    """S7.3 阻断一反例（切点①：claim 落盘之后、写文件之前崩溃）：手工放一个
    "40 秒前"（超过 30 秒过期阈值）的 claim，目标文件不存在（模拟上一次
    在这一步之后就死了，从没写过文件）。下一次 Stop 必须能正常写文件、
    正常落 verdict——不能像返修令描述的那样永远停在
    state=working, verdict=None, claim 卡住。"""
    task_id = make_review_task()
    stale_at = (
        datetime.now(timezone.utc) - timedelta(seconds=40)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_status(
        task_id,
        review_file_claim={"round": 1, "token": "dead-token", "claimed_at": stale_at},
    )
    assert not (store.task_dir(task_id) / "review-1.md").is_file()
    text = "过期 claim 之后重新处理的意见。\n\nNEXT: done"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "done"
    assert status["review_recorded_round"] == 1
    assert "review_file_claim" not in status
    assert Path(status["review_file"]).read_text(encoding="utf-8") == text


def test_stop_review_stale_claim_after_write_before_commit_crash_recovers():
    """S7.3 阻断一反例（切点②：写文件之后、commit 之前崩溃）：手工放一个
    过期 claim，且目标文件已经存在（模拟上一次真的写完了文件，但在
    commit verdict 那一步之前就死了）。下一次 Stop 重新 claim、重新走
    一遍——旧文件内容会被这次 Stop 的正文覆盖，这是预期行为（不是从半途
    恢复，是干净地重做一遍），最终 verdict 与新文件内容要对得上。"""
    task_id = make_review_task()
    review_path = store.task_dir(task_id) / "review-1.md"
    review_path.write_text("上一次死掉前已经写好但没提交的旧内容", encoding="utf-8")
    stale_at = (
        datetime.now(timezone.utc) - timedelta(seconds=40)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_status(
        task_id,
        review_file_claim={"round": 1, "token": "dead-token-2", "claimed_at": stale_at},
    )
    text = "重新处理后的新意见。\n\nNEXT: fix"
    proc = run_hook(task_id, "Stop", json.dumps({"last_assistant_message": text}))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["review_verdict"] == "fix"
    assert "review_file_claim" not in status
    assert review_path.read_text(encoding="utf-8") == text


def test_stop_review_claim_takeover_old_writer_cannot_corrupt_canonical_file(monkeypatch):
    """S7.4 阻断一反例（监理原话"双 writer：旧的慢、新的接管并先提交、旧的
    随后恢复"）：A 的 Stop 先 claim、写文件这一步被拦截（模拟"写了很久"）；
    拦截期间，B 的 Stop（时间戳晚 40 秒，跨过 30 秒过期阈值）完整跑完一遍
    ——claim 接管、写文件、提交 NEXT: fix。A 的写文件随后才真正完成、走到
    自己的 commit，这时它的 token 已经不是当前 claim 的 token，必须整个
    放弃（连 canonical 文件的 os.replace 都不做），不能把 canonical 覆盖
    回自己的旧内容。S7.3 的 token 检查只保护了 status 提交，没保护写文件
    这一步（那一步以前直接碰 canonical），这条反例就是补这个洞：最终
    canonical 正文、verdict、last_message 三者都必须来自 B，A 自己的暂存
    文件不残留。"""
    from nightshift import hook

    task_id = make_review_task()
    a_text = "旧 writer 的意见（不该生效）。\n\nNEXT: done"
    b_text = "新 writer 接管后的意见（应该生效）。\n\nNEXT: fix"

    real_atomic_write_text = store.atomic_write_text
    state = {"intercepted": False}

    def fake_atomic_write_text(path, text, mode=None):
        if not state["intercepted"] and str(path).endswith(".pending"):
            state["intercepted"] = True
            future_now = (
                datetime.now(timezone.utc) + timedelta(seconds=40)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            hook._handle_review_stop(
                store.load_task(task_id),
                {"last_assistant_message": b_text}, future_now,
            )
        return real_atomic_write_text(path, text, mode=mode)

    monkeypatch.setattr(store, "atomic_write_text", fake_atomic_write_text)
    now_a = store.utc_now_iso()
    hook._handle_review_stop(
        store.load_task(task_id), {"last_assistant_message": a_text}, now_a,
    )

    status = store.read_status(task_id)
    assert status["review_verdict"] == "fix"
    assert status["last_message"].startswith("新 writer 接管后的意见")
    assert "review_file_claim" not in status
    canonical = store.task_dir(task_id) / "review-1.md"
    assert canonical.read_text(encoding="utf-8") == b_text
    pending_files = list(store.task_dir(task_id).glob("review-1.*.pending"))
    assert pending_files == []


def test_stop_review_concurrent_stop_accepts_only_one_verdict():
    """S7.1 阻断二反例：两个并发 Stop（不同内容）打向同一个 review 任务
    同一轮，最终只有一个 verdict 被接受、两个进程都不抛异常、review_file
    内容与被接受的那次一致（不会两边都判定"我可以写"、也不会文件内容被
    交叉写乱）。"""
    task_id = make_review_task()
    texts = {
        "done": "第一个并发 Stop 的意见，通过。\n\nNEXT: done",
        "fix": "第二个并发 Stop 的意见，退回。\n\nNEXT: fix",
    }
    procs = [
        multiprocessing.Process(
            target=_run_hook_proc,
            args=(task_id, "Stop", json.dumps({"last_assistant_message": t})),
        )
        for t in texts.values()
    ]
    _spawn_and_join(procs)
    status = store.read_status(task_id)
    assert status["review_verdict"] in texts
    assert status["review_recorded_round"] == 1
    assert status["review_verdict_final"] is True
    winner_text = texts[status["review_verdict"]]
    review_path = Path(status["review_file"])
    assert review_path.read_text(encoding="utf-8") == winner_text


def test_post_tool_use_context_warn_review_role_ends_in_pending(tmp_path):
    """S7.1 阻断二：review 撞上下文警戒线收到的是 review 专属文案（末行
    NEXT: pending），不是 build 的 handover/NEXT:continue-done 协议——
    否则回复末行写 NEXT:continue 会被 `_parse_review_verdict` 判协议缺失，
    保守转成 fix（假退回）。不用在测试 config 里额外配
    review_context_warn_text，模块内置默认兜底就该生效。"""
    task_id = make_review_task(guards={
        "session_pct_max": 80,
        "context_warn_tokens": 400,
        "context_limit_tokens": 2000,
    })
    payload = make_transcript(tmp_path / "transcript.jsonl", 1200)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次
    assert proc.returncode == 0
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "NEXT: pending" in ctx
    assert "NEXT: continue" not in ctx
    assert "handover" not in ctx  # 不是 build 那套交接协议
    from nightshift.hook import _parse_review_verdict

    simulated_reply = ctx + "\n\n（模拟审稿正文接在提醒后面）\n\nNEXT: pending"
    verdict, protocol_ok = _parse_review_verdict(simulated_reply)
    assert verdict == "pending" and protocol_ok


def test_post_tool_use_quota_pause_review_role_ends_in_pending(tmp_path):
    """S7.1 阻断二：review 撞五小时额度线不走 build 那套 ScheduleWakeup
    多轮自我唤醒闹钟（中间那些"·"回复没有 NEXT，会被判协议缺失、保守转成
    fix）——改成当场一次性 NEXT: pending，交给 nightshift 调度器按额度
    刷新时间主动敲它继续。"""
    task_id = make_review_task(guards={
        "session_pct_max": 80,
        "weekly_pct_max": 95,
        "context_warn_tokens": 100000,
        "context_limit_tokens": 200000,
    })
    write_quota(session=85, week=10)  # 五小时 85 ≥ 80
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 20 次
    assert proc.returncode == 0
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "NEXT: pending" in ctx
    assert "ScheduleWakeup" not in ctx  # 不再走 build 那套多轮自我唤醒
    status = store.read_status(task_id)
    assert status["quota_paused_until"]


def test_silence_on_empty_stdin():
    task_id = make_task()
    proc = run_hook(task_id, "Stop", "")
    assert proc.returncode == 0 and proc.stdout == ""  # 空 stdin 当 {} 处理，但静默


def test_silence_on_bad_json():
    task_id = make_task()
    proc = run_hook(task_id, "Stop", "{这不是 JSON")
    assert proc.returncode == 0 and proc.stdout == ""  # 坏 JSON 当 {} 处理，但静默


def test_silence_on_missing_task():
    proc = run_hook("20990101-000000- deadbeef".replace(" ", ""), "Stop", "{}")
    assert proc.returncode == 0 and proc.stdout == ""


def test_session_end_keeps_terminal_states(tmp_path):
    """finished/chained 之后关窗口，SessionEnd 只记 exit_reason，不把状态盖成 exited。"""
    task_id = make_task()
    for state in ("finished", "chained"):
        store.update_status(task_id, state=state)
        run_hook(task_id, "SessionEnd", '{"reason": "other"}')
        status = store.read_status(task_id)
        assert status["state"] == state
        assert status["exit_reason"] == "other"
        assert status["session_ended_at"]
    store.update_status(task_id, state="idle")
    run_hook(task_id, "SessionEnd", '{"reason": "other"}')
    assert store.read_status(task_id)["state"] == "exited"


def test_quota_warn_text_from_config(tmp_path):
    """额度提醒文案走 config.quota_warn_text（模板页可改），不再是藏起来的常量。"""
    cfg = dict(CONFIG)
    cfg["quota_pause_text"] = "自定义暂停：剩{session_left}，线{session_line_left}"
    store.atomic_write_json(store.home() / "config.json", cfg)
    task_id = make_task(guards={
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    write_quota(session=85, week=10)
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert ctx == "自定义暂停：剩15，线20"


def test_weekly_quota_wrapup_beats_pause(tmp_path):
    """周线到了 → 收尾交接（哪怕五小时也到了，收尾优先）；写 context_warned_at 供换班判定。"""
    task_id = make_task(guards={
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    write_quota(session=90, week=97)
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "周额度只剩 3%" in ctx and "NEXT: done" in ctx and "handover-1.md" in ctx
    assert "ScheduleWakeup" not in ctx
    status = store.read_status(task_id)
    assert status["quota_warn_count"] == 1 and status["context_warned_at"]


def test_wrapup_commit_step_old_style_only(tmp_path):
    """S5 {commit_step}：工作树任务渲染为空（调度器打存档点）；
    老式任务（worktree=false）保留"把未提交的改动 commit"。"""
    cfg = dict(CONFIG)
    cfg["quota_wrapup_text"] = "收尾：写 {handover_path}；{commit_step}然后停下。"
    store.atomic_write_json(store.home() / "config.json", cfg)
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for worktree_flag, want_commit in ((True, False), (False, True)):
        task_id = make_task(
            worktree=worktree_flag,
            guards={
                "session_pct_max": 80, "weekly_pct_max": 95,
                "context_warn_tokens": 100000, "context_limit_tokens": 200000,
            },
        )
        write_quota(session=90, week=97)
        for _ in range(19):
            run_hook(task_id, "PostToolUse", payload)
        proc = run_hook(task_id, "PostToolUse", payload)
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        assert ("把未提交的改动 commit" in ctx) is want_commit, (worktree_flag, ctx)
        assert "然后停下" in ctx


def test_stop_with_session_crons_is_waiting_wakeup():
    task_id = make_task()
    run_hook(task_id, "Stop", json.dumps({
        "background_tasks": [],
        "session_crons": [{"id": "x", "schedule": "50 20 * * *", "recurring": False, "prompt": "继续"}],
        "last_assistant_message": "·",
    }))
    status = store.read_status(task_id)
    assert status["state"] == "waiting_wakeup"
    assert status["session_crons"][0]["id"] == "x"


def test_stop_build_hold_control_kind_goes_held_not_normal_stop_path():
    """S7.4 阻断三反例①：working build 被"我来看"打断（`build_control_kind`
    已经在 send 之前落盘，见 server._api_pipeline_hold）——这次 Stop 不该
    走正常的存档点/换班/审稿流程，必须直接转 held、清掉控制标记，且不该
    触碰任何交接判定字段（`chain_checked`/`checkpoint_done` 保持原样，不
    因为这次 Stop 而被误置成"已经收工"）。"""
    task_id = make_task()
    store.update_status(task_id, state="working", build_control_kind="hold")
    proc = run_hook(task_id, "Stop", json.dumps({
        "last_assistant_message": "收到，我先停下。",
    }))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert status["state"] == "held"
    assert status["held_reason"] == "我来看：工头要来看，已停在这里"
    assert "build_control_kind" not in status
    assert "checkpoint_done" not in status
    assert "chain_checked" not in status
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "我来看：已停在这里等工头" in events


def test_alarm_plan():
    from nightshift.hook import alarm_plan
    text, total = alarm_plan(110)
    assert text == "50 分钟、50 分钟、13 分钟（共 3 个）" and total == 113
    text, total = alarm_plan(0)
    assert text == "3 分钟（共 1 个）" and total == 3
    text, total = alarm_plan(None)
    assert "按最长等" in text and total == 300


def test_other_model_weekly_line_only_notes(tmp_path):
    """sonnet 任务：Fable 周线到了不叫停，只提示一次"别派 Fable 子 agent"。"""
    cfg = dict(CONFIG)
    cfg["models"] = {"claude-sonnet-5": {"context_limit": 500000, "usage_label": "Sonnet"},
                     "claude-fable-5": {"context_limit": 500000, "usage_label": "Fable"}}
    store.atomic_write_json(store.home() / "config.json", cfg)
    task_id = make_task(model="claude-sonnet-5", guards={
        "session_pct_max": 80, "weekly_pct_max": 95, "model_weekly_pct_max": 90,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    write_quota(session=10, week=20, per_model={"Fable": 92, "Sonnet": 30})
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Fable 的周额度只剩 8%" in ctx and "别再派 Fable" in ctx
    assert "NEXT: done" not in ctx and "ScheduleWakeup" not in ctx
    assert store.read_status(task_id)["other_model_warned"] == ["Fable"]
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 40 次：同一模型不再提示
    assert proc.stdout == ""


def test_own_model_line_uses_model_weekly_pct_max(tmp_path):
    task_id = make_task(guards={
        "session_pct_max": 80, "weekly_pct_max": 95, "model_weekly_pct_max": 85,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    write_quota(session=10, week=20, per_model={"Fable": 88})
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Fable 单独周线剩 12%（线 15%）" in ctx and "NEXT: done" in ctx


def test_quota_model_lookup_uses_runner_view_not_stale_toplevel(tmp_path):
    """二次返修阻断二反例②：顶层 `config.models` 与
    `runners.claude.models` 故意分裂——own-model/other-model 判断只该认
    runner view，不能被过期的顶层快照带偏（比如顶层还留着旧的
    usage_label，或者压根没声明这个模型）。"""
    cfg = dict(CONFIG)
    # 顶层留一份跟真实情况不一样的旧快照：把 Fable 的 usage_label 改错，
    # 且压根不认识 Sonnet。
    cfg["models"] = {"claude-fable-5": {"context_limit": 500000, "usage_label": "旧标签"}}
    cfg["runners"] = {
        "claude": {
            "models": {
                "claude-fable-5": {"context_limit": 500000, "usage_label": "Fable"},
                "claude-sonnet-5": {"context_limit": 500000, "usage_label": "Sonnet"},
            },
            "efforts": CONFIG["efforts"],
        }
    }
    store.atomic_write_json(store.home() / "config.json", cfg)
    task_id = make_task(model="claude-sonnet-5", guards={
        "session_pct_max": 80, "weekly_pct_max": 95, "model_weekly_pct_max": 90,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    write_quota(session=10, week=20, per_model={"Fable": 92, "Sonnet": 30})
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(19):
        run_hook(task_id, "PostToolUse", payload)
    proc = run_hook(task_id, "PostToolUse", payload)
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    # own model 是 Sonnet（runner view 里查得到 usage_label），"旧标签"
    # 那份过期顶层快照如果被用来查 own，会把 Fable 也当成"不是自己"去提示，
    # 或者干脆查不到 own 导致 Fable 提示逻辑判断错误。
    assert "Fable 的周额度只剩 8%" in ctx and "别再派 Fable" in ctx
    assert "旧标签" not in ctx


# ---------- S4①/S4.1 疑似卡住：恢复事件清整个卡住周期 ----------


def test_hook_events_clear_stuck_cycle():
    """三类恢复事件清 stuck，且连本次卡住周期的 auto_interrupted /
    stuck_since 一起清——只清 stuck 的话，会话缓过来后第二次卡住
    永远不会再自动 Esc（S4.1 必修2）。"""
    task_id = make_task()
    stale_cycle = {
        "stuck": True,
        "stuck_since": "2026-08-27T17:00:00Z",
        "auto_interrupted": True,
    }

    def assert_recovered() -> None:
        status = store.read_status(task_id)
        assert status["stuck"] is False
        assert "auto_interrupted" not in status
        assert "stuck_since" not in status

    # UserPromptSubmit 清
    store.update_status(task_id, **stale_cycle)
    run_hook(task_id, "UserPromptSubmit", fixture("hook_userpromptsubmit.json"))
    assert_recovered()
    # PostToolUse 清
    post_tool = next(
        line for line in background_lines()
        if json.loads(line).get("hook_event_name") == "PostToolUse"
    )
    store.update_status(task_id, **stale_cycle)
    run_hook(task_id, "PostToolUse", post_tool)
    assert_recovered()
    # Stop 清
    store.update_status(task_id, **stale_cycle)
    run_hook(task_id, "Stop", fixture("hook_stop_idle.json"))
    assert_recovered()


# ---------- 总review F2：额度刷新时间过后自己清 quota_paused_until ----------


def test_user_prompt_submit_drops_expired_quota_pause():
    task_id = make_task()
    store.update_status(
        task_id, state="waiting_wakeup", quota_paused_until="2020-01-01T00:00:00Z",
        quota_resume_sent=False, session_crons=[{"id": "c1"}],
    )
    proc = run_hook(task_id, "UserPromptSubmit", fixture("hook_userpromptsubmit.json"))
    assert proc.returncode == 0
    status = store.read_status(task_id)
    assert "quota_paused_until" not in status
    assert "quota_resume_sent" not in status
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "额度刷新时间已过，会话已自行继续/收工，取消调度器补敲" in events


def test_user_prompt_submit_keeps_quota_pause_still_in_future():
    task_id = make_task()
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_status(
        task_id, state="waiting_wakeup", quota_paused_until=future, quota_resume_sent=False,
    )
    run_hook(task_id, "UserPromptSubmit", fixture("hook_userpromptsubmit.json"))
    status = store.read_status(task_id)
    assert status["quota_paused_until"] == future


def test_user_prompt_submit_control_turn_does_not_drop_quota_pause():
    """控制 turn（调度器投递的保活/我来看）不该顺带把过期的
    quota_paused_until 也清掉——那不是模型自己真的又开工了。"""
    task_id = make_task()
    store.update_status(
        task_id, state="held", quota_paused_until="2020-01-01T00:00:00Z",
        build_control_kind="keepalive",
    )
    run_hook(task_id, "UserPromptSubmit", fixture("hook_userpromptsubmit.json"))
    status = store.read_status(task_id)
    assert status["quota_paused_until"] == "2020-01-01T00:00:00Z"


def test_user_prompt_submit_review_role_does_not_drop_quota_pause():
    """review 的 hold/resume 协议靠 quota_paused_until 触发调度器主动叫醒，
    hook 侧提前清掉会让 review 永久卡在 held。"""
    task_id = make_review_task()
    store.update_status(
        task_id, state="held", quota_paused_until="2020-01-01T00:00:00Z",
    )
    run_hook(task_id, "UserPromptSubmit", fixture("hook_userpromptsubmit.json"))
    status = store.read_status(task_id)
    assert status["quota_paused_until"] == "2020-01-01T00:00:00Z"


def test_stop_idle_drops_expired_quota_pause():
    """闹钟响完模型自己继续、干完、Stop → idle：过期的 quota_paused_until
    要在这次 Stop 里清掉，调度器下一 tick 才不会误判"还没恢复"再补敲一句。"""
    task_id = make_task()
    store.update_status(
        task_id, state="waiting_wakeup", quota_paused_until="2020-01-01T00:00:00Z",
        quota_resume_sent=False,
    )
    run_hook(task_id, "Stop", fixture("hook_stop_idle.json"))
    status = store.read_status(task_id)
    assert status["state"] == "idle"
    assert "quota_paused_until" not in status
    assert "quota_resume_sent" not in status
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "额度刷新时间已过，会话已自行继续/收工，取消调度器补敲" in events


# ---------- S6：Codex hook（--codex 调用形状，NIGHTOWL_TASK_ID 路由）----------


def test_codex_hook_no_task_id_env_is_noop():
    proc = subprocess.run(
        [sys.executable, "-m", "nightshift.hook", "--codex", "Stop"],
        input="{}", capture_output=True, text=True,
        env={**os.environ, "NIGHTSHIFT_HOME": os.environ["NIGHTSHIFT_HOME"],
             "PYTHONPATH": str(REPO_ROOT)},
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""  # 没有 NIGHTOWL_TASK_ID：静默退出，不炸


def test_codex_hook_full_event_sequence():
    task_id = make_task_codex()

    proc = run_codex_hook(task_id, "SessionStart", fixture("codex_hook_sessionstart.json"))
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["thread_id"] == "01a05206-e86e-7c80-8540-1b92468c92a1"
    assert status["session_id"] == status["thread_id"]
    assert status["quota_source"] == "codex"
    assert status["permission_mode"] == "bypassPermissions"

    proc = run_codex_hook(task_id, "UserPromptSubmit", fixture("codex_hook_userpromptsubmit.json"))
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["state"] == "working"
    assert status["turns"] == 1

    # PostToolUse：只记账，不算上下文、不回注（Codex 一律 send-keys，不用 stdout）
    proc = run_codex_hook(task_id, "PostToolUse", fixture("codex_hook_posttooluse.json"))
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["tool_calls"] == 1
    assert status["context_tokens"] is None

    proc = run_codex_hook(task_id, "Stop", fixture("codex_hook_stop.json"))
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["state"] == "idle"  # S6：无登记后台时 idle（F12 的登记簿是 S6④ 才有）
    assert status["over_warn_line"] is False
    assert "canary.txt" in status["last_message"]
    assert status["context_tokens"] is None  # Codex 没有稳定上下文水位来源

    proc = run_codex_hook(task_id, "SessionEnd", fixture("codex_hook_sessionend.json"))
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["state"] == "exited"
    assert status["exit_reason"] == "other"


def test_codex_hook_stop_waiting_wakeup_when_quota_paused():
    task_id = make_task_codex()
    store.update_status(task_id, quota_paused_until="2099-01-01T00:00:00Z")
    proc = run_codex_hook(task_id, "Stop", fixture("codex_hook_stop.json"))
    assert proc.returncode == 0
    assert store.read_status(task_id)["state"] == "waiting_wakeup"


def test_codex_hook_subagent_events():
    task_id = make_task_codex()
    proc = run_codex_hook(task_id, "SubagentStart", fixture("codex_hook_subagentstart.json"))
    assert proc.returncode == 0
    assert store.read_status(task_id)["subagents_running"] == 1
    proc = run_codex_hook(task_id, "SubagentStop", fixture("codex_hook_subagentstop.json"))
    assert proc.returncode == 0
    assert store.read_status(task_id)["subagents_running"] == 0


def test_claude_session_start_is_ignored_not_crash():
    """Claude 不挂 SessionStart，但万一收到（比如手滑用了 --codex 之外的调用）
    也不能崩，只记一笔忽略。"""
    task_id = make_task()
    proc = run_hook(task_id, "SessionStart", "{}")
    assert proc.returncode == 0 and proc.stdout == ""
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "SessionStart" in events


def test_codex_hook_never_prints_stdout_injection():
    """设计决定：Codex 一律 send-keys，不用 hook stdout 回注——PostToolUse
    多打几次也不该有任何 stdout 输出（跟 Claude 每 20 次会回注形成对照）。"""
    task_id = make_task_codex()
    for _ in range(25):
        proc = run_codex_hook(task_id, "PostToolUse", fixture("codex_hook_posttooluse.json"))
        assert proc.stdout == ""
    assert store.read_status(task_id)["tool_calls"] == 25


# ---------- S8 审查 B：水位刷新按增量触发；子 agent 体内不回注 ----------


def test_post_tool_use_refreshes_early_when_transcript_grows(tmp_path):
    """第 20 次刷到线下之后几次大 Read 把 transcript 撑过硬上限——不能等到第 40 次才提醒。
    transcript 自上次刷新长了 ≥ max(32KB, 上限×5%×4B) 就提前刷一次；没再长就不刷（不刷屏）。"""
    task_id = make_task(
        guards={"session_pct_max": 80, "context_limit_tokens": 2000, "context_warn_tokens": 1600}
    )
    transcript = tmp_path / "transcript.jsonl"
    payload = make_transcript(transcript, 500)
    for _ in range(20):
        run_hook(task_id, "PostToolUse", payload)
    status = store.read_status(task_id)
    assert status["tool_calls"] == 20 and status["context_tokens"] == 500
    assert status["context_refresh_size"] == transcript.stat().st_size
    # 两次大 Read：transcript 涨 80KB，最后一条 usage 报 2500（已越过硬上限 2000）
    with transcript.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "R" * 40000}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1000}}}) + "\n")
        f.write(json.dumps({"type": "user", "message": {"content": "R" * 40000}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 2500}}}) + "\n")
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 21 次：增量触发，立刻刷新并回注
    assert proc.returncode == 0, proc.stderr
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "2k" in ctx
    status = store.read_status(task_id)
    assert status["tool_calls"] == 21 and status["context_tokens"] == 2500
    assert status["context_warn_count"] == 1
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 22 次：没再长，不刷不注
    assert proc.stdout == ""
    assert store.read_status(task_id)["context_warn_count"] == 1


def test_post_tool_use_time_triggered_refresh_fires_after_5_minutes(tmp_path):
    """总review F5：每次工具调用都跑几分钟测试的班，"每 20 次"之间可能一
    小时都不刷一次水位/额度（B 组报告 B-2）。距上次刷新决定成立 ≥ 5 分钟
    要提前刷一次，不等到第 20 次。"""
    task_id = make_task(guards={
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(4):
        run_hook(task_id, "PostToolUse", payload)
    assert "context_refreshed_at" in store.read_status(task_id)  # 首次调用只登记
    stale = (datetime.now(timezone.utc) - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_status(task_id, context_refreshed_at=stale)
    proc = run_hook(task_id, "PostToolUse", payload)  # 第 5 次：距上次刷新 6 分钟 → 刷
    assert proc.returncode == 0
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "hook PostToolUse #5 刷新上下文" in events
    assert store.read_status(task_id)["context_refreshed_at"] != stale


def test_post_tool_use_time_triggered_refresh_does_not_fire_before_5_minutes(tmp_path):
    task_id = make_task(guards={
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_tokens": 100000, "context_limit_tokens": 200000,
    })
    payload = make_transcript(tmp_path / "transcript.jsonl", 100)
    for _ in range(4):
        run_hook(task_id, "PostToolUse", payload)
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_status(task_id, context_refreshed_at=fresh)
    run_hook(task_id, "PostToolUse", payload)  # 第 5 次：距上次刷新只 1 分钟 → 不刷
    events_path = store.task_dir(task_id) / "events.log"
    events = events_path.read_text(encoding="utf-8") if events_path.is_file() else ""
    assert "hook PostToolUse #5 刷新上下文" not in events
    assert store.read_status(task_id)["context_refreshed_at"] == fresh


def test_post_tool_use_subagent_call_defers_injection_to_main(tmp_path):
    """9/1 本机真机复现：第 20 次落在子 agent 的工具调用里时，回注只会进子 agent 的工具
    结果，主会话看不见、status 却已记 warned/paused。判据：payload.tool_use_id 不在主
    transcript 尾部 → 这次不刷不注、记欠账；主会话下一次工具调用立刻补刷补注。"""
    task_id = make_task(
        guards={"session_pct_max": 80, "context_warn_tokens": 400, "context_limit_tokens": 2000}
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 1200},
                "content": [{"type": "tool_use", "id": "toolu_main_001", "name": "Bash", "input": {}}],
            },
        }) + "\n",
        encoding="utf-8",
    )
    base = json.loads(fixture("hook_userpromptsubmit.json"))
    base["transcript_path"] = str(transcript)
    main_payload = json.dumps({**base, "tool_use_id": "toolu_main_001"})
    sub_payload = json.dumps({**base, "tool_use_id": "toolu_sub_999"})
    for _ in range(19):
        run_hook(task_id, "PostToolUse", main_payload)
    proc = run_hook(task_id, "PostToolUse", sub_payload)  # 第 20 次在子 agent 里
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["tool_calls"] == 20
    assert status.get("context_warned_at") is None and status.get("context_tokens") is None
    assert status["context_refresh_pending"] is True
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "落在子 agent" in events
    proc = run_hook(task_id, "PostToolUse", main_payload)  # 第 21 次回到主会话：补刷补注
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "1k" in ctx
    status = store.read_status(task_id)
    assert status["context_warned_at"] and status["context_warn_count"] == 1
    assert status["context_tokens"] == 1200
    assert "context_refresh_pending" not in status
