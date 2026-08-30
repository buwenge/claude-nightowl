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
