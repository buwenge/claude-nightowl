"""codex_notify.py 的测试：只记账，绝不碰任务状态。"""

import json

import pytest

from nightshift import codex_notify, store

CONFIG = {"projects": {"demo": "/home/user/projects/demo"},
          "efforts": ["low", "medium", "high", "xhigh"]}


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    return tmp_path


def make_task() -> str:
    return store.create_task({
        "title": "notify 测试任务", "project": "demo", "runner": "codex",
        "model": "m", "effort": "high", "run_at": "2026-08-27T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    }, {**CONFIG, "runners": {"codex": {"models": {}, "efforts": CONFIG["efforts"]}}})


REAL_PAYLOAD = {
    "type": "agent-turn-complete",
    "thread-id": "01a05206-e86e-7c80-8540-1b92468c92a1",
    "turn-id": "01a05206-ea47-70f0-a6da-3c829dd99be6",
    "cwd": "/tmp/proj",
    "client": "codex-tui",
    "input-messages": ["干点活"],
    "last-assistant-message": "done",
}


def test_handle_notify_appends_event_only():
    task_id = make_task()
    before = store.read_status(task_id)
    codex_notify.handle_notify(task_id, REAL_PAYLOAD)
    after = store.read_status(task_id)
    assert after == before  # 绝不碰 status.json
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "agent-turn-complete" in events
    assert "01a05206-e86e-7c80-8540-1b92468c92a1" in events


def test_handle_notify_missing_task_dir_noop():
    codex_notify.handle_notify("no-such-task", REAL_PAYLOAD)  # 不炸就算过


def test_main_requires_task_id_env(monkeypatch):
    monkeypatch.delenv("NIGHTOWL_TASK_ID", raising=False)
    assert codex_notify.main([json.dumps(REAL_PAYLOAD)]) == 0
    # 没有环境变量：不落任何事件（没有任务目录可写）


def test_main_bad_json_is_safe(monkeypatch):
    task_id = make_task()
    monkeypatch.setenv("NIGHTOWL_TASK_ID", task_id)
    assert codex_notify.main(["不是json"]) == 0
    events = (store.task_dir(task_id) / "events.log")
    assert not events.exists() or "agent-turn-complete" not in events.read_text(encoding="utf-8")


def test_main_non_dict_json_is_safe(monkeypatch):
    task_id = make_task()
    monkeypatch.setenv("NIGHTOWL_TASK_ID", task_id)
    assert codex_notify.main(["[1, 2, 3]"]) == 0


def test_main_happy_path(monkeypatch):
    task_id = make_task()
    monkeypatch.setenv("NIGHTOWL_TASK_ID", task_id)
    assert codex_notify.main([json.dumps(REAL_PAYLOAD)]) == 0
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "agent-turn-complete" in events


def test_main_no_argv_is_safe(monkeypatch):
    task_id = make_task()
    monkeypatch.setenv("NIGHTOWL_TASK_ID", task_id)
    assert codex_notify.main([]) == 0
