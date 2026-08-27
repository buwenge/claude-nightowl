"""hook 接收端的测试：子进程喂夹具，逐步核对 status.json 的状态机。"""

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift import store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent

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
        "claude-haiku-4-5-20251001": {"context_limit": 200000},
    },
    "default_context_limit": 200000,
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
    "chain": {"max_windows": 3, "on_no_handover": "continue"},
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
    assert "last_event_at" in status

    # ② SubagentStart → subagents_running=1
    proc = run_hook(task_id, "SubagentStart", subagent_start)
    assert proc.returncode == 0 and proc.stdout == ""
    status = store.read_status(task_id)
    assert status["subagents_running"] == 1
    assert status["agent_type"] == "general-purpose"

    # ③ SubagentStop → 减回 0，不低于 0
    proc = run_hook(task_id, "SubagentStop", subagent_stop)
    assert proc.returncode == 0 and proc.stdout == ""
    assert store.read_status(task_id)["subagents_running"] == 0

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
    """4 起 4 停同时跑，最后 subagents_running 归零。"""
    task_id = make_task()
    procs = [
        multiprocessing.Process(
            target=_run_hook_proc,
            args=(task_id, "SubagentStart", '{"hook_event_name":"SubagentStart"}'),
        )
        for _ in range(4)
    ] + [
        multiprocessing.Process(
            target=_run_hook_proc,
            args=(task_id, "SubagentStop", '{"hook_event_name":"SubagentStop"}'),
        )
        for _ in range(4)
    ]
    _spawn_and_join(procs)
    assert store.read_status(task_id)["subagents_running"] == 0


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
