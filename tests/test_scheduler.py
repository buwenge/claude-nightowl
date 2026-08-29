"""scheduler 的测试：tick 全分支、崩溃恢复、保活戳、推迟/失败窗口、run_forever 兜底。

launcher / quota 全部 monkeypatch 成可控假函数；时间用固定的 aware datetime 注入。
"""

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nightshift import launcher, quota, scheduler, store, worktree

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 8, 27, 18, 0, 0, tzinfo=timezone.utc)
NO_PID = 2**24  # 必然不存在的 pid（同 test_launcher 的用法）

CONFIG = {
    "tmux_session": "claude",
    "window_prefix": "ns:",
    "claude_bin": "claude",
    "probe_model": "claude-haiku-4-5-20251001",
    "display_tz_offset_hours": 8,
    "memory_max_bytes": 2147483648,
    "projects": {
        "demo": "/home/user/projects/demo",
        "other": "/home/user/projects/other",
    },
    "models": {
        "claude-fable-5": {"context_limit": 500000, "usage_label": "Fable"},
    },
    "default_context_limit": 200000,
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
    "chain": {"max_windows": 3, "on_no_handover": "continue"},
    "chain_template": "{task}\n\n这是第 {shift} 班。上一班交接如下：\n{handover}\n\n先核对交接里说的状态再动手。",
    "scheduler": {
        "interval_seconds": 30,
        "launch_grace_seconds": 180,
        "postpone_minutes": 30,
        "max_postpone_hours": 6,
        "quota_refresh_minutes": 30,
        "keepalive_idle_minutes": 50,
        "keepalive_text": "保活探针——还在跑吗？",
    },
}


@pytest.fixture(autouse=True)
def ns_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    store.atomic_write_json(tmp_path / "config.json", CONFIG)
    return tmp_path


def make_task(**over):
    task = {
        "title": "夜间重构",
        "project": "demo",
        "model": "claude-fable-5",
        "effort": "high",
        "run_at": scheduler.to_iso(NOW),
        "task_text": "正文",
        "prompt_final": "提示词",
    }
    task.update(over)
    return store.create_task(task, CONFIG)


def usage_fixture() -> dict:
    text = (FIXTURES / "usage_output.txt").read_text(encoding="utf-8")
    return quota.parse_usage(text)


class Fakes:
    """把 launcher / quota 的对外接口全部换成记录调用的假函数。"""

    def __init__(self, monkeypatch, *, trusted=True, window_alive=True,
                 pid_alive=True):
        self.trusted = trusted
        self.window_alive = window_alive
        self.pid_alive = pid_alive
        self.usage = usage_fixture()
        self.usage_exc: Exception | None = None
        self.now = NOW  # 假 launch 写 launched_at 用的时间
        self.launch_calls: list[str] = []
        self.notice_calls: list[tuple] = []
        self.failure_calls: list[tuple] = []
        self.send_keys_calls: list[tuple] = []
        self.fetch_calls: list[int] = []
        self.close_calls: list[list[str]] = []

        monkeypatch.setattr(launcher, "is_trusted", lambda path: self.trusted)
        monkeypatch.setattr(launcher, "launch", self._launch)
        monkeypatch.setattr(
            launcher, "window_alive", lambda wid, config: self.window_alive
        )
        monkeypatch.setattr(launcher, "pid_alive", lambda pid: self.pid_alive)
        monkeypatch.setattr(launcher, "send_keys", self._send_keys)
        monkeypatch.setattr(launcher, "open_notice_window", self._notice)
        monkeypatch.setattr(launcher, "open_failure_window", self._failure)
        monkeypatch.setattr(launcher, "close_windows", self._close_windows)
        monkeypatch.setattr(quota, "fetch_usage", self._fetch)

    def _launch(self, task_id, config):
        self.launch_calls.append(task_id)
        store.update_status(
            task_id,
            state="launching",
            launched_at=scheduler.to_iso(self.now),
            window_id="@9",
            pane_pid=NO_PID,
        )
        return store.read_status(task_id)

    def _close_windows(self, window_ids, config):
        """记录"要关哪些窗口"；只回记录里的 @N，绝不碰会话。"""
        self.close_calls.append([str(w) for w in (window_ids or [])])
        return [str(w) for w in (window_ids or [])]

    def _notice(self, task, suffix, lines, config):
        self.notice_calls.append((task["id"], suffix, list(lines)))

    def _failure(self, task, reason, config):
        self.failure_calls.append((task["id"], reason))

    def _send_keys(self, window_id, text):
        self.send_keys_calls.append((window_id, text))

    def _fetch(self, config, timeout=120):
        self.fetch_calls.append(1)
        if self.usage_exc is not None:
            raise self.usage_exc
        return dict(self.usage)


# ---------- 时间小函数 ----------


def test_parse_iso_and_to_iso_roundtrip():
    dt = datetime(2026, 8, 27, 18, 0, 5, tzinfo=timezone.utc)
    assert scheduler.to_iso(dt) == "2026-08-27T18:00:05Z"
    assert scheduler.parse_iso("2026-08-27T18:00:05Z") == dt
    # 与 store.utc_now_iso 同格式来回转
    assert scheduler.to_iso(scheduler.parse_iso(store.utc_now_iso())).endswith("Z")
    # 裸时间按 UTC
    assert scheduler.parse_iso("2026-08-27T18:00:05").tzinfo is not None


# ---------- scheduled / postponed 到点起跑 ----------


def test_due_scheduled_launches_and_future_does_not(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task()  # run_at == NOW
    fakes.now = NOW
    actions = scheduler.tick(CONFIG, NOW)
    assert fakes.launch_calls == [tid]
    assert any(tid in a for a in actions)

    # 没到点的不叫；到过点的那个已转 launching（宽限期内）也不重起
    make_task(title="未来任务", run_at=scheduler.to_iso(NOW + timedelta(minutes=5)))
    fakes.launch_calls.clear()
    actions = scheduler.tick(CONFIG, NOW)
    assert fakes.launch_calls == []
    assert actions == []


def test_postponed_due_relaunches(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task()
    store.update_status(
        tid, state="postponed",
        next_attempt_at=scheduler.to_iso(NOW - timedelta(minutes=1)),
    )
    fakes.now = NOW
    scheduler.tick(CONFIG, NOW)
    assert fakes.launch_calls == [tid]


# ---------- 推迟与失败窗口 ----------


def test_quota_over_postpones_with_one_notice_window(monkeypatch):
    fakes = Fakes(monkeypatch)
    fakes.usage["session_pct"] = 85  # 超五小时线
    tid = make_task()
    scheduler.tick(CONFIG, NOW)

    status = store.read_status(tid)
    assert status["state"] == "postponed"
    assert status["next_attempt_at"] == scheduler.to_iso(NOW + timedelta(minutes=30))
    assert status["postponed_count"] == 1
    assert "85%" in status["postpone_reason"]
    assert fakes.launch_calls == []
    # 第一次推迟：开一个窗口，suffix 含"推迟"
    assert len(fakes.notice_calls) == 1
    assert fakes.notice_calls[0][0] == tid and "推迟" in fakes.notice_calls[0][1]
    lines = fakes.notice_calls[0][2]
    assert any("下次尝试" in line for line in lines)
    assert any("最多推到" in line for line in lines)

    # 再过 30 分钟仍超线 → 第二次推迟，不再开窗口
    scheduler.tick(CONFIG, NOW + timedelta(minutes=30))
    status = store.read_status(tid)
    assert status["state"] == "postponed"
    assert status["postponed_count"] == 2
    assert len(fakes.notice_calls) == 1


def test_usage_unavailable_postpones_fail_closed(monkeypatch):
    fakes = Fakes(monkeypatch)
    fakes.usage_exc = quota.UsageUnavailable("/usage 超时（120s）")
    tid = make_task()
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "postponed"
    assert "fail-closed" in status["postpone_reason"]
    assert fakes.launch_calls == []


def test_postpone_past_deadline_fails_with_window(monkeypatch):
    fakes = Fakes(monkeypatch)
    fakes.usage["session_pct"] = 85
    tid = make_task(run_at=scheduler.to_iso(NOW - timedelta(hours=6, minutes=1)))
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "failed"
    assert "推迟超过 6 小时仍不满足" in status["error"]
    assert "85%" in status["error"]
    assert len(fakes.failure_calls) == 1
    assert fakes.notice_calls == []


# ---------- 同目录不并跑 / 目录信任 ----------


def test_same_project_worktree_parallel_matrix(monkeypatch):
    """S5 同项目并跑矩阵：true/true 不互挡；只要一方是 false 仍按一期同目录锁推迟。"""
    for busy_wt, cand_wt, blocked in ((True, True, False), (True, False, True),
                                      (False, True, True), (False, False, True)):
        fakes = Fakes(monkeypatch)  # 窗口在、pid 在
        shutil.rmtree(store.home() / "tasks", ignore_errors=True)  # 每格重开一局
        busy = make_task(title="在跑的", worktree=busy_wt)
        store.update_status(busy, state="working", window_id="@1", pane_pid=NO_PID)
        cand = make_task(title="同目录的", worktree=cand_wt)

        fakes.now = NOW
        scheduler.tick(CONFIG, NOW)

        status = store.read_status(cand)
        if blocked:
            assert status["state"] == "postponed", (busy_wt, cand_wt)
            assert "还在跑" in status["postpone_reason"]
            assert status["postponed_count"] == 1
            assert fakes.notice_calls == []  # 同目录推迟不开窗口
            assert fakes.launch_calls == []
        else:
            assert status["state"] == "launching", (busy_wt, cand_wt)
            assert fakes.launch_calls == [cand]
        assert store.read_status(busy)["state"] == "working"


def test_worktree_false_postpones_with_reason(monkeypatch):
    """老式任务撞上同项目活跃任务：推迟原因照旧，不开窗口。"""
    fakes = Fakes(monkeypatch)
    busy = make_task(title="在跑的", worktree=False)
    store.update_status(busy, state="working", window_id="@1", pane_pid=NO_PID)
    same_dir = make_task(title="老式同目录", worktree=False)

    fakes.now = NOW
    scheduler.tick(CONFIG, NOW)

    status = store.read_status(same_dir)
    assert status["state"] == "postponed"
    assert busy in status["postpone_reason"]
    assert fakes.notice_calls == []


def test_untrusted_project_fails_without_postpone(monkeypatch):
    fakes = Fakes(monkeypatch, trusted=False)
    tid = make_task()
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "failed"
    assert "未信任" in status["error"]
    assert status.get("postpone_reason") is None
    assert len(fakes.failure_calls) == 1
    assert fakes.launch_calls == []
    # 信任检查在额度之前：根本不该去查 /usage
    assert fakes.fetch_calls == []


def test_launch_failure_reported_from_launch_return(monkeypatch):
    """R5：launch() 返回 state=failed（tmux 失败路径）时，动作描述必须是
    "启动失败"，不能谎报"已启动"。"""
    fakes = Fakes(monkeypatch)
    tid = make_task()
    monkeypatch.setattr(
        launcher, "launch",
        lambda task_id, config: {"state": "failed", "error": "x"},
    )
    actions = scheduler.tick(CONFIG, NOW)
    assert any("启动失败" in a and "x" in a for a in actions)


# ---------- 崩溃恢复：launching ----------


def test_launching_retry_then_failed(monkeypatch):
    fakes = Fakes(monkeypatch, window_alive=False)  # 窗口没了
    tid = make_task()
    t0 = NOW
    store.update_status(tid, state="launching", launched_at=scheduler.to_iso(t0),
                        turns=0)
    # 宽限期内不动
    scheduler.tick(CONFIG, t0 + timedelta(seconds=60))
    status = store.read_status(tid)
    assert status["state"] == "launching"
    assert not status.get("retries")

    # 过了宽限期、窗口没了 → retries=1，回到 scheduled；run_at 不许被改写，
    # 重试时刻记进 status.retry_at（R4）
    cur = t0 + timedelta(seconds=200)
    for retries in (1, 2, 3):
        scheduler.tick(CONFIG, cur)
        status = store.read_status(tid)
        assert status["state"] == "scheduled"
        assert status["retries"] == retries
        assert status["retry_at"] == scheduler.to_iso(cur)
        assert store.load_task(tid)["run_at"] == scheduler.to_iso(t0)
        # 下一 tick 到点重起（假 launch 盖新的 launched_at）→ 又变 launching
        fakes.now = cur
        scheduler.tick(CONFIG, cur)
        assert store.read_status(tid)["state"] == "launching"
        cur = cur + timedelta(seconds=200)
    # 第 4 次 → 超限判失败 + 失败窗口
    scheduler.tick(CONFIG, cur)
    status = store.read_status(tid)
    assert status["state"] == "failed"
    assert "启动重试超限" in status["error"]
    assert len(fakes.failure_calls) == 1


def test_launching_alive_window_or_turned_untouched(monkeypatch):
    fakes = Fakes(monkeypatch)  # 窗口在、pid 在
    tid = make_task(title="还在起")
    stale = scheduler.to_iso(NOW - timedelta(seconds=999))
    store.update_status(tid, state="launching", launched_at=stale, turns=0,
                        window_id="@5", pane_pid=NO_PID)
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "launching"
    assert not status.get("retries")

    # turns>0（hook 其实来过）也不按崩溃处理
    store.update_status(tid, window_id=None, turns=1)
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "launching"
    assert not status.get("retries")


def test_launching_exit_code_retries_even_within_grace(monkeypatch):
    """R1 真雷：claude 起来就崩，run.sh 写下 exit_code 而 pane 靠 read 留窗；
    宽限期内/窗口还在都必须立刻重试，不许永远卡在 launching。"""
    fakes = Fakes(monkeypatch)  # 窗口在、pid 在（read 留窗的假象）
    tid = make_task()
    t0 = NOW
    store.update_status(tid, state="launching", launched_at=scheduler.to_iso(t0),
                        window_id="@21", pane_pid=NO_PID, turns=0)
    (store.task_dir(tid) / "exit_code").write_text("1\n", encoding="utf-8")

    # 宽限期内 + 窗口都在：exit_code 是死透的铁证 → 立即重试
    cur = t0 + timedelta(seconds=10)
    scheduler.tick(CONFIG, cur)
    status = store.read_status(tid)
    assert status["state"] == "scheduled"
    assert status["retries"] == 1
    assert status["retry_at"] == scheduler.to_iso(cur)
    # R4：task.json 的 run_at 一个字不改
    assert store.load_task(tid)["run_at"] == scheduler.to_iso(t0)
    # 留证：exit_code 改名 exit_code.<retries>
    d = store.task_dir(tid)
    assert not (d / "exit_code").exists()
    assert (d / "exit_code.1").read_text(encoding="utf-8").strip() == "1"
    # 事件带退出码
    events = (d / "events.log").read_text(encoding="utf-8")
    assert "exit_code=1" in events

    # 下一 tick 到点（retry_at）重起 → 又变 launching
    fakes.now = cur
    scheduler.tick(CONFIG, cur)
    assert fakes.launch_calls == [tid]
    assert store.read_status(tid)["state"] == "launching"
    # 重启只清 exit_code 本尊，留证文件还在
    assert (store.task_dir(tid) / "exit_code.1").read_text(
        encoding="utf-8"
    ).strip() == "1"


def test_working_exit_code_marks_exited(monkeypatch):
    """R1 兜底：SessionEnd hook 没来时，working/idle + exit_code → 退场，
    exit_reason 带退出码；窗口/pid 都在（read 留窗）也拦不住这条。"""
    fakes = Fakes(monkeypatch)  # 窗口在、pid 在
    tid = make_task()
    store.update_status(tid, state="working", window_id="@22", pane_pid=NO_PID)
    (store.task_dir(tid) / "exit_code").write_text("7\n", encoding="utf-8")
    actions = scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "exited"
    assert status["exit_reason"] == "claude_exit_7"
    assert any("claude_exit_7" in a for a in actions)

    # idle 同样吃这条兜底；没有 exit_code 的任务不受影响。
    # S3 起 idle 首次评估会走换班判定（无交接且从未提醒 → finished），
    # 这里只验"兜底不碰 idle"，故先落 chain_checked=True 跳过换班评估
    other = make_task(title="正常 idle")
    store.update_status(other, state="idle", window_id="@23", pane_pid=NO_PID,
                        chain_checked=True)
    scheduler.tick(CONFIG, NOW)
    assert store.read_status(other)["state"] == "idle"


# ---------- 运行期巡检：窗口消失 / 保活戳 ----------


def test_working_window_gone_exits_and_alive_untouched(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(title="巡检对象")
    store.update_status(tid, state="working", window_id="@3", pane_pid=NO_PID)
    scheduler.tick(CONFIG, NOW)
    assert store.read_status(tid)["state"] == "working"  # 窗口在、pid 在 → 不动

    fakes.window_alive = False
    actions = scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "exited"
    assert status["exit_reason"] == "window_gone"
    assert any("window_gone" in a for a in actions)


def test_non_auto_permission_mode_warns_once(monkeypatch):
    """R2：会话权限模式被回落成 default（如 haiku 不吃 auto）→ 开一次提醒窗，
    mode_warned 落盘；不改 state、不杀窗口；auto/bypassPermissions 不提醒。"""
    fakes = Fakes(monkeypatch)
    tid = make_task()
    store.update_status(
        tid, state="working", window_id="@11", pane_pid=NO_PID,
        permission_mode="default",
    )
    scheduler.tick(CONFIG, NOW)
    assert len(fakes.notice_calls) == 1
    noticed_id, suffix, lines = fakes.notice_calls[0]
    assert noticed_id == tid and "(注意)" in suffix
    assert any("default" in line and "auto" in line for line in lines)
    status = store.read_status(tid)
    assert status["mode_warned"] is True
    assert status["state"] == "working"  # 不改 state

    # 再 tick 不再叫（mode_warned 已落盘）
    scheduler.tick(CONFIG, NOW)
    assert len(fakes.notice_calls) == 1

    # 对照：auto 模式不提醒
    auto_task = make_task(title="auto 的")
    store.update_status(
        auto_task, state="working", window_id="@12", pane_pid=NO_PID,
        permission_mode="auto",
    )
    scheduler.tick(CONFIG, NOW)
    assert len(fakes.notice_calls) == 1
    # bypassPermissions 同样放过
    store.update_status(
        auto_task, state="working", permission_mode="bypassPermissions",
        window_id="@12",
    )
    scheduler.tick(CONFIG, NOW)
    assert len(fakes.notice_calls) == 1


def test_keepalive_pokes_waiting_background_once(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(title="等背景")
    stale = scheduler.to_iso(NOW - timedelta(minutes=51))
    store.update_status(tid, state="waiting_background", window_id="@4",
                        pane_pid=NO_PID, last_event_at=stale)
    scheduler.tick(CONFIG, NOW)
    assert len(fakes.send_keys_calls) == 1
    window_id, text = fakes.send_keys_calls[0]
    assert window_id == "@4" and "保活" in text
    status = store.read_status(tid)
    assert status["last_keepalive_at"] == scheduler.to_iso(NOW)
    assert status["keepalive_count"] == 1

    # 紧接着再 tick 一次（now+1min）不再叫
    scheduler.tick(CONFIG, NOW + timedelta(minutes=1))
    assert len(fakes.send_keys_calls) == 1

    # 49 分钟不够久也不叫（边界对照：now+1min 时才静默 49 分钟）
    other = make_task(title="还太早")
    fresh = scheduler.to_iso(NOW - timedelta(minutes=48))
    store.update_status(other, state="waiting_background", window_id="@6",
                        pane_pid=NO_PID, last_event_at=fresh)
    scheduler.tick(CONFIG, NOW + timedelta(minutes=1))
    assert len(fakes.send_keys_calls) == 1


def test_idle_never_poked(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(title="已收尾")
    stale = scheduler.to_iso(NOW - timedelta(minutes=51))
    # S3 起 idle 首次评估会走换班判定；本条只验"idle 永远不戳保活"，
    # 先落 chain_checked=True 跳过换班评估
    store.update_status(tid, state="idle", window_id="@7", pane_pid=NO_PID,
                        last_event_at=stale, chain_checked=True)
    scheduler.tick(CONFIG, NOW)
    assert fakes.send_keys_calls == []
    assert store.read_status(tid)["state"] == "idle"


def test_keepalive_disabled_not_poked(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(title="不许戳", guards={"keepalive": False})
    stale = scheduler.to_iso(NOW - timedelta(minutes=51))
    store.update_status(tid, state="waiting_background", window_id="@8",
                        pane_pid=NO_PID, last_event_at=stale)
    scheduler.tick(CONFIG, NOW)
    assert fakes.send_keys_calls == []


# ---------- S4① after 触发：等前置任务 ----------


def _pre_chain() -> tuple[str, str]:
    """造一条"前置在干活、后继等它"的最小场景，返回 (pre, after)。"""
    pre = make_task(title="前置", run_at=scheduler.to_iso(NOW + timedelta(hours=1)))
    return pre, ""


def test_after_task_waits_until_pre_chain_finished(monkeypatch):
    fakes = Fakes(monkeypatch)
    pre, _ = _pre_chain()
    after = make_task(
        title="后继", trigger={"type": "after", "task": pre, "when": "finished"}
    )
    # 前置在干活：后继不起（scheduled 分支什么都不做）
    store.update_status(pre, state="working", window_id="@40", pane_pid=NO_PID)
    actions = scheduler.tick(CONFIG, NOW)
    assert fakes.launch_calls == []
    assert all(after not in a for a in actions)
    # 前置链开出第 2 班：链判定看最新一班，第 2 班没完仍不起
    succ = make_task(title="前置", shift=2, root_id=pre, parent_id=pre,
                     run_at=scheduler.to_iso(NOW + timedelta(hours=1)))
    store.update_status(pre, state="finished")
    store.update_status(succ, state="working", window_id="@41", pane_pid=NO_PID)
    scheduler.tick(CONFIG, NOW)
    assert fakes.launch_calls == []
    # 第 2 班 finished → 后继起（launch 被叫一次）
    store.update_status(succ, state="finished")
    scheduler.tick(CONFIG, NOW)
    assert fakes.launch_calls == [after]
    assert store.read_status(after)["trigger_met_at"] == scheduler.to_iso(NOW)


def test_after_when_ended_fires_on_failed_only(monkeypatch):
    fakes = Fakes(monkeypatch)
    pre, _ = _pre_chain()
    ended = make_task(title="等结束", trigger={"type": "after", "task": pre, "when": "ended"})
    # 另一个项目：免得 ended 先起跑后，fin 被同目录互斥预检推迟
    fin = make_task(title="等完工", project="other",
                    trigger={"type": "after", "task": pre, "when": "finished"})
    store.update_status(pre, state="failed")
    scheduler.tick(CONFIG, NOW)
    # when=ended：failed 也算结束 → 起；when=finished：不起
    assert sorted(fakes.launch_calls) == sorted([ended])
    store.update_status(pre, state="finished")
    scheduler.tick(CONFIG, NOW)
    assert sorted(fakes.launch_calls) == sorted([ended, fin])


def test_after_understands_worktree_completion_states(monkeypatch):
    """merged 算真正完工；awaiting_merge 只算施工已经结束。"""
    fakes = Fakes(monkeypatch)
    pre = make_task(title="工作树前置", run_at=scheduler.to_iso(NOW + timedelta(hours=1)))
    wait_finished = make_task(
        title="等真正合入", project="other",
        trigger={"type": "after", "task": pre, "when": "finished"},
    )
    wait_ended = make_task(
        title="等施工结束", trigger={"type": "after", "task": pre, "when": "ended"},
    )
    store.update_status(pre, state="awaiting_merge")
    scheduler.tick(CONFIG, NOW)
    assert wait_ended in fakes.launch_calls
    assert wait_finished not in fakes.launch_calls
    store.update_status(pre, state="merged")
    scheduler.tick(CONFIG, NOW)
    assert wait_finished in fakes.launch_calls


def test_after_pre_deleted_needs_attention_once(monkeypatch):
    import shutil

    fakes = Fakes(monkeypatch)
    pre, _ = _pre_chain()
    after = make_task(title="等前置", trigger={"type": "after", "task": pre, "when": "finished"})
    shutil.rmtree(store.task_dir(pre))
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(after)
    assert status["state"] == "needs_attention"
    assert status["attention_noted"] is True
    assert fakes.launch_calls == []
    assert len(fakes.notice_calls) == 1
    noticed_id, suffix, lines = fakes.notice_calls[0]
    assert noticed_id == after and "(需要人工)" in suffix
    assert any(pre in ln and "不存在" in ln for ln in lines)
    # 只开一次：再 tick 不再重复标窗口
    scheduler.tick(CONFIG, NOW)
    assert len(fakes.notice_calls) == 1


def test_after_postpone_window_anchored_at_trigger_met(monkeypatch):
    fakes = Fakes(monkeypatch)
    fakes.usage["session_pct"] = 85  # 前置满足后预检必推迟
    pre, _ = _pre_chain()
    after = make_task(title="后继", trigger={"type": "after", "task": pre, "when": "finished"})
    store.update_status(pre, state="finished")
    met = NOW + timedelta(minutes=10)
    scheduler.tick(CONFIG, met)
    status = store.read_status(after)
    assert status["state"] == "postponed"
    assert status["trigger_met_at"] == scheduler.to_iso(met)
    # run_at（NOW）+ 6h 已过，但从 trigger_met_at 起算还没到 → 不许判失败
    scheduler.tick(CONFIG, NOW + timedelta(hours=6, minutes=1))
    assert store.read_status(after)["state"] == "postponed"
    # 下一次尝试（第二次推迟 + 30 分钟）仍不满足，且已过 trigger_met_at + 6h → 判失败
    scheduler.tick(CONFIG, NOW + timedelta(hours=6, minutes=31))
    status = store.read_status(after)
    assert status["state"] == "failed"
    assert "推迟超过 6 小时" in status["error"]


def test_after_postponed_rejudges_trigger_at_next_attempt(monkeypatch):
    """postponed 的 after 任务：next_attempt_at 到点后再判一次前置条件。"""
    fakes = Fakes(monkeypatch)
    pre, _ = _pre_chain()
    after = make_task(title="后继", trigger={"type": "after", "task": pre, "when": "finished"})
    store.update_status(
        after, state="postponed",
        next_attempt_at=scheduler.to_iso(NOW - timedelta(minutes=1)),
    )
    # 前置还没完工：到点也不起
    scheduler.tick(CONFIG, NOW)
    assert fakes.launch_calls == []
    # 前置完工 → 起
    store.update_status(pre, state="finished")
    scheduler.tick(CONFIG, NOW)
    assert fakes.launch_calls == [after]


# ---------- S4① 疑似卡住检测 ----------


def test_stuck_marked_after_silence_and_not_repeated(monkeypatch):
    Fakes(monkeypatch)
    tid = make_task(title="卡住的")
    stale = scheduler.to_iso(NOW - timedelta(minutes=20))
    store.update_status(tid, state="working", window_id="@50", pane_pid=NO_PID,
                        last_event_at=stale)
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["stuck"] is True
    assert status["stuck_since"] == scheduler.to_iso(NOW)
    assert status["state"] == "working"  # 不改 state
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "疑似卡住" in events
    # 已标过：再 tick 不重复记事件
    n_before = len(events.splitlines())
    scheduler.tick(CONFIG, NOW)
    assert len((store.task_dir(tid) / "events.log").read_text(
        encoding="utf-8").splitlines()) == n_before

    # 刚有事件、没到线：不标；waiting_wakeup / idle 不参与卡住判定
    fresh = make_task(title="很活跃")
    store.update_status(fresh, state="working", window_id="@52", pane_pid=NO_PID,
                        last_event_at=scheduler.to_iso(NOW - timedelta(minutes=5)))
    wakeup = make_task(title="等闹钟的")
    store.update_status(wakeup, state="waiting_wakeup", window_id="@53",
                        pane_pid=NO_PID,
                        last_event_at=scheduler.to_iso(NOW - timedelta(minutes=99)))
    scheduler.tick(CONFIG, NOW)
    assert not store.read_status(fresh).get("stuck")
    assert not store.read_status(wakeup).get("stuck")


def test_stuck_uses_configured_minutes(monkeypatch):
    Fakes(monkeypatch)
    tid = make_task(title="按配置判卡")
    stale = scheduler.to_iso(NOW - timedelta(minutes=6))
    store.update_status(tid, state="waiting_background", window_id="@54",
                        pane_pid=NO_PID, last_event_at=stale)
    config = {**CONFIG, "scheduler": {**CONFIG["scheduler"], "stuck_minutes": 5}}
    scheduler.tick(config, NOW)
    assert store.read_status(tid)["stuck"] is True
    # stuck_minutes=0 关掉检测
    other = make_task(title="不判卡")
    store.update_status(other, state="working", window_id="@55", pane_pid=NO_PID,
                        last_event_at=scheduler.to_iso(NOW - timedelta(hours=9)))
    config_off = {**CONFIG, "scheduler": {**CONFIG["scheduler"], "stuck_minutes": 0}}
    scheduler.tick(config_off, NOW)
    assert not store.read_status(other).get("stuck")


def test_auto_interrupt_fires_once(monkeypatch):
    fakes = Fakes(monkeypatch)
    escapes: list[str] = []
    monkeypatch.setattr(launcher, "send_escape", lambda wid: escapes.append(wid))
    tid = make_task(title="自动中止", guards={"auto_interrupt_minutes": 5})
    stale = scheduler.to_iso(NOW - timedelta(minutes=20))
    store.update_status(tid, state="working", window_id="@56", pane_pid=NO_PID,
                        last_event_at=stale)
    scheduler.tick(CONFIG, NOW)  # 首次标 stuck，stuck_since=NOW，还不到 5 分钟
    assert escapes == []
    scheduler.tick(CONFIG, NOW + timedelta(minutes=6))  # 卡住满 6 分钟 ≥ 5
    assert escapes == ["@56"]
    assert store.read_status(tid)["auto_interrupted"] is True
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "自动中止" in events
    # 不重复
    scheduler.tick(CONFIG, NOW + timedelta(minutes=7))
    assert escapes == ["@56"]


def test_auto_interrupt_recovers_per_stuck_cycle(monkeypatch):
    """S4.1 回归：第一次卡住 → Esc 一次 → hook 恢复（清 auto_interrupted /
    stuck_since）→ 第二次卡住 → 可再 Esc 一次；同一周期内仍不重复。"""
    from nightshift import hook

    Fakes(monkeypatch)
    escapes: list[str] = []
    monkeypatch.setattr(launcher, "send_escape", lambda wid: escapes.append(wid))
    tid = make_task(title="两次卡住", guards={"auto_interrupt_minutes": 5})
    stale = scheduler.to_iso(NOW - timedelta(minutes=20))
    store.update_status(tid, state="working", window_id="@57", pane_pid=NO_PID,
                        last_event_at=stale)
    scheduler.tick(CONFIG, NOW + timedelta(minutes=6))   # 标第一次卡住
    assert store.read_status(tid)["stuck"] is True
    scheduler.tick(CONFIG, NOW + timedelta(minutes=12))  # 卡满 ≥5 分钟 → Esc 一次
    assert escapes == ["@57"]
    scheduler.tick(CONFIG, NOW + timedelta(minutes=13))  # 同一周期内不重复
    assert escapes == ["@57"]

    # hook 恢复（UserPromptSubmit 到场）：本次卡住周期的三个标记都要清
    hook.handle_event(tid, "UserPromptSubmit", {})
    status = store.read_status(tid)
    assert status["stuck"] is False
    assert "auto_interrupted" not in status
    assert "stuck_since" not in status

    # 第二次卡住：把 last_event_at 拉回静默线以前，再走一遍
    store.update_status(
        tid, last_event_at=scheduler.to_iso(NOW + timedelta(minutes=14))
    )
    scheduler.tick(CONFIG, NOW + timedelta(minutes=31))  # 静默 17 分钟 → 再标卡
    assert store.read_status(tid)["stuck"] is True
    scheduler.tick(CONFIG, NOW + timedelta(minutes=37))  # 又卡满 ≥5 分钟 → 再 Esc
    assert escapes == ["@57", "@57"]
    scheduler.tick(CONFIG, NOW + timedelta(minutes=38))  # 仍不重复
    assert escapes == ["@57", "@57"]


# ---------- S3② 换班：交接判定与后继任务 ----------


def _go_idle(tid: str, **extra) -> None:
    fields = {"state": "idle", "window_id": "@30", "pane_pid": NO_PID}
    fields.update(extra)
    store.update_status(tid, **fields)


def _write_handover(tid: str, text: str, shift: int = 1) -> None:
    (store.task_dir(tid) / f"handover-{shift}.md").write_text(
        text + "\n", encoding="utf-8"
    )


def test_chain_continue_creates_successor(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(retry_max=2, worktree=False)
    _go_idle(tid)
    _write_handover(tid, "登录页已完成。\n还差支付页。\nNEXT: continue")
    actions = scheduler.tick(CONFIG, NOW)

    parent_status = store.read_status(tid)
    assert parent_status["state"] == "chained"
    assert parent_status["chain_checked"] is True
    successor_id = parent_status["successor_id"]
    assert successor_id
    assert any(successor_id in a for a in actions)
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert f"续班 → {successor_id}（第 2 班）" in events

    successor = store.load_task(successor_id)
    assert successor["shift"] == 2
    assert successor["parent_id"] == tid
    assert successor["root_id"] == tid
    assert successor["retry_max"] == 2
    assert store.read_status(successor_id)["state"] == "scheduled"
    # 提示词 = 续班模板：交接正文 + 第 2 班
    assert "还差支付页" in successor["prompt_final"]
    assert "第 2 班" in successor["prompt_final"]
    # 复制的字段
    parent = store.load_task(tid)
    for key in ("title", "project", "model", "effort", "task_text", "guards", "chain"):
        assert successor[key] == parent[key]
    # 父窗口不关（没人去杀它；这里至少保证调度器没开/关任何窗口）
    assert fakes.failure_calls == []


def test_chain_done_finishes(monkeypatch):
    # worktree=false 走一期路径：NEXT: done → finished（S5② 回归开关）
    Fakes(monkeypatch)
    tid = make_task(worktree=False)
    _go_idle(tid)
    _write_handover(tid, "全部完成，已提交。\nNEXT: done")
    actions = scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "finished"
    assert status.get("successor_id") is None
    assert any("finished" in a for a in actions)
    assert len(store.list_tasks()) == 1  # 没有后继


def test_chain_handover_without_next_treated_as_continue(monkeypatch):
    Fakes(monkeypatch)
    tid = make_task(worktree=False)
    _go_idle(tid)
    _write_handover(tid, "做了一半，进度记在这里")
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "chained"
    successor = store.load_task(status["successor_id"])
    assert successor["shift"] == 2
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "交接末行没写 NEXT，按 continue" in events


def test_chain_no_handover_warned_continue_uses_fallback_text(monkeypatch):
    Fakes(monkeypatch)
    tid = make_task(worktree=False)
    _go_idle(tid, context_warned_at=scheduler.to_iso(NOW - timedelta(minutes=10)))
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "chained"
    successor = store.load_task(status["successor_id"])
    assert "上一班没留交接" in successor["prompt_final"]
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "on_no_handover=continue" in events


def test_chain_no_handover_warned_stop_needs_attention(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(chain={"on_no_handover": "stop"}, worktree=False)
    _go_idle(tid, context_warned_at=scheduler.to_iso(NOW - timedelta(minutes=10)))
    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention"
    assert status.get("successor_id") is None
    assert len(fakes.notice_calls) == 1
    noticed_id, suffix, lines = fakes.notice_calls[0]
    assert noticed_id == tid and "(需要人工)" in suffix
    assert any("没留交接" in ln for ln in lines)
    assert any("handover-1.md" in ln for ln in lines)
    assert len(store.list_tasks()) == 1


def test_chain_no_handover_never_warned_finishes(monkeypatch):
    # worktree=false：没交接也没被提醒 → 一期 finished 路径不变
    Fakes(monkeypatch)
    tid = make_task(worktree=False)
    _go_idle(tid)
    actions = scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "finished"
    assert any("finished" in a for a in actions)
    assert len(store.list_tasks()) == 1


def test_chain_shift_at_max_windows_exhausted(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(shift=3, worktree=False)  # chain.max_windows=3，这班就是最后一班
    _go_idle(tid)
    _write_handover(tid, "还没做完。\nNEXT: continue", shift=3)
    actions = scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "chain_exhausted"
    assert status.get("successor_id") is None
    assert any("班次用尽" in a for a in actions)
    assert len(fakes.notice_calls) == 1
    assert "(班次用尽)" in fakes.notice_calls[0][1]


def test_chain_evaluated_only_once(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(worktree=False)
    _go_idle(tid, context_warned_at=scheduler.to_iso(NOW - timedelta(minutes=10)))
    scheduler.tick(CONFIG, NOW)
    successor_id = store.read_status(tid)["successor_id"]
    # 再 tick：父任务已 chained（不在巡检范围），不许重复评估、不许重复续班
    actions = scheduler.tick(CONFIG, NOW)
    assert actions == []
    assert store.read_status(tid)["successor_id"] == successor_id
    assert len(store.list_tasks()) == 2
    assert fakes.notice_calls == []


def test_chain_exited_with_handover_continues(monkeypatch):
    Fakes(monkeypatch)
    tid = make_task(worktree=False)
    store.update_status(tid, state="exited", exit_reason="window_gone")
    _write_handover(tid, "上下文写完交接时会话被关了。\nNEXT: continue")
    actions = scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "chained"
    successor_id = status["successor_id"]
    assert successor_id
    assert any(successor_id in a for a in actions)
    assert store.read_status(successor_id)["state"] == "scheduled"


def test_chain_exited_without_handover_untouched(monkeypatch):
    Fakes(monkeypatch)
    tid = make_task()
    store.update_status(tid, state="exited", exit_reason="other")
    actions = scheduler.tick(CONFIG, NOW)
    assert actions == []
    status = store.read_status(tid)
    assert status["state"] == "exited"  # 没交接不动
    assert status["chain_checked"] is True
    assert len(store.list_tasks()) == 1


# ---------- S5②：收工存档点与完工分流（真 Git 仓库） ----------


def _make_repo(tmp_path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(proj), "config", "user.email", "ns@example.test"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(proj), "config", "user.name", "ns"],
                   check=True, capture_output=True)
    (proj / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)
    return proj


def _config_for(proj: Path) -> dict:
    cfg = dict(CONFIG)
    cfg["projects"] = {"demo": str(proj), "other": str(proj / "nope")}
    return cfg


def _register_tree(proj: Path, tid: str, title: str) -> Path:
    """launch 被 Fakes 替掉了，树由测试手工建好并登记（与 launcher.launch 同形，
    含 info/exclude——否则 .claude/ 在主线是 untracked，merge 预检会判脏）。"""
    from nightshift import worktree as wt_mod
    slug = wt_mod.slug_for(tid, title)
    wt = proj / ".claude" / "worktrees" / slug
    subprocess.run(
        ["git", "-C", str(proj), "worktree", "add", str(wt), "-b", f"ns/{slug}"],
        check=True, capture_output=True,
    )
    wt_mod.ensure_exclude(proj)
    head = subprocess.run(["git", "-C", str(proj), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    store.update_status(tid, worktree_path=str(wt), branch=f"ns/{slug}", base_ref=head)
    return wt


def _branch_log(proj: Path, branch: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(proj), "log", "--format=%s", branch],
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def test_worktree_continue_checkpoints_then_successor_same_tree(tmp_path, monkeypatch):
    """NEXT: continue：先打 c1 再造后继；后继在同一树继续改，第二班再打 c2。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _config_for(proj)
    tid = make_task()
    _register_tree(proj, tid, "夜间重构")
    _go_idle(tid, window_id="@21")
    _write_handover(tid, "做了一半。\nNEXT: continue")
    # 工作树里有未提交改动 → 存档点必须收进来
    wt = Path(store.read_status(tid)["worktree_path"])
    (wt / "canary.txt").write_text("第一班\n", encoding="utf-8")

    actions = scheduler.tick(cfg, NOW)
    status = store.read_status(tid)
    assert status["state"] == "chained"
    assert status.get("checkpoint_done") is True
    c1 = status.get("checkpoint_sha")
    assert c1 and len(c1) == 40
    assert "ns: 夜间重构 第1轮 build#1" in _branch_log(proj, status["branch"])
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "已打存档点" in events
    assert any("续班" in a for a in actions)
    # 后继沿用同一棵树
    succ_id = status["successor_id"]
    succ_status = store.read_status(succ_id)
    for key in ("worktree_path", "branch", "base_ref"):
        assert succ_status[key] == status[key]

        # 第二班：同树再改，收工 NEXT: done（manual → awaiting_merge）
        _go_idle(succ_id, window_id="@22")
        (store.task_dir(succ_id) / "handover-2.md").write_text(
            "另一半也好了。\nNEXT: done\n", encoding="utf-8")
        (wt / "canary.txt").write_text("第一班\n第二班\n", encoding="utf-8")
    scheduler.tick(cfg, NOW)
    succ_status = store.read_status(succ_id)
    assert succ_status["state"] == "awaiting_merge"
    c2 = succ_status.get("checkpoint_sha")
    assert c2 and c2 != c1
    subjects = _branch_log(proj, succ_status["branch"])
    # S5 没有轮次字段：round 固定 1，班次取真实 shift
    assert "ns: 夜间重构 第1轮 build#2" in subjects
    # manual 不合并：窗口都还留着（合并成功才关），没有会话级操作
    assert fakes.close_calls == []


def test_worktree_manual_done_awaits_merge_never_finished(tmp_path, monkeypatch):
    Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _config_for(proj)
    tid = make_task()
    _register_tree(proj, tid, "夜间重构")
    _go_idle(tid)
    _write_handover(tid, "干完了。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    status = store.read_status(tid)
    assert status["state"] == "awaiting_merge"  # 不是 finished
    assert status.get("checkpoint_done") is True
    assert status.get("checkpoint_sha") is None  # 没改动：不打存档点
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "第 1 班无改动，未打存档点" in events
    # 树与分支都保留
    assert Path(status["worktree_path"]).exists()


def test_worktree_auto_done_merges_and_cleans(tmp_path, monkeypatch):
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _config_for(proj)
    tid = make_task(review={"enabled": False, "merge_policy": "auto"})
    wt = _register_tree(proj, tid, "夜间重构")
    _go_idle(tid, window_id="@31")
    (wt / "canary.txt").write_text("自动合并的活\n", encoding="utf-8")
    _write_handover(tid, "干完了。\nNEXT: done")

    actions = scheduler.tick(cfg, NOW)
    status = store.read_status(tid)
    assert status["state"] == "merged", actions
    assert status.get("merge_sha")
    # --no-ff merge commit（两个 parent）
    parents = subprocess.run(
        ["git", "-C", str(proj), "rev-list", "--parents", "-n", "1", "HEAD"],
        capture_output=True, text=True, check=True).stdout.split()
    assert len(parents) == 3
    # 树与分支清掉（合并成功后链成员的元数据也被清）；链上登记的窗口被关
    assert not wt.exists()
    assert "worktree_path" not in status
    branch = f"ns/{worktree.slug_for(tid, '夜间重构')}"
    out = subprocess.run(["git", "-C", str(proj), "branch", "--list", branch],
                         capture_output=True, text=True, check=True).stdout
    assert branch not in out
    assert fakes.close_calls == [["@31"]]


def test_worktree_auto_dirty_main_needs_attention_exact_text(tmp_path, monkeypatch):
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _config_for(proj)
    tid = make_task(review={"enabled": False, "merge_policy": "auto"})
    wt = _register_tree(proj, tid, "夜间重构")
    (wt / "canary.txt").write_text("活\n", encoding="utf-8")
    # 主线有工头自己的 untracked 改动
    (proj / "note.txt").write_text("别动\n", encoding="utf-8")
    _go_idle(tid)
    _write_handover(tid, "干完了。\nNEXT: done")

    scheduler.tick(cfg, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention"
    assert status["error"] == "主线有你没提交的改动，没敢自动合并；处理完按'合并进主线'"
    assert len(fakes.notice_calls) == 1  # 设计要求：卡片红字之外再开一次告警窗
    assert "自动合并没有完成" in fakes.notice_calls[0][2][0]
    # 树与分支保留、merge commit 数不变
    assert wt.exists()
    merges = subprocess.run(
        ["git", "-C", str(proj), "rev-list", "--count", "--merges", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert merges == "0"
    # 工头清完主线 → 网页重试（同一 helper）→ merged
    (proj / "note.txt").unlink()
    ok, note = worktree.merge_task(
        store.load_task(tid), proj, store.read_status(tid), cfg,
        close_windows=lambda ids: launcher.close_windows(ids, cfg))
    assert ok, note
    assert store.read_status(tid)["state"] == "merged"


def test_worktree_checkpoint_failure_stops_chain(tmp_path, monkeypatch):
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _config_for(proj)
    tid = make_task()
    _register_tree(proj, tid, "夜间重构")
    (Path(store.read_status(tid)["worktree_path"]) / "canary.txt").write_text(
        "活\n", encoding="utf-8")
    _go_idle(tid)
    _write_handover(tid, "做了一半。\nNEXT: continue")
    monkeypatch.setattr(
        worktree, "checkpoint",
        lambda task, path: (_ for _ in ()).throw(worktree.WorktreeError("git commit 失败（exit 128）：没有身份")))

    actions = scheduler.tick(cfg, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention"
    assert any("存档点失败" in a for a in actions)
    assert "存档点失败" in status["error"]
    assert status.get("successor_id") is None  # 不造后继
    assert len(store.list_tasks()) == 1
    assert len(fakes.notice_calls) == 1  # 开一次提醒窗
    # 再 tick：不重复刷（chain_checked 已落）
    assert scheduler.tick(cfg, NOW) == []


def test_worktree_missing_metadata_stops_instead_of_fake_finish(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(worktree=True)
    _go_idle(tid)
    _write_handover(tid, "干完了。\nNEXT: done")
    actions = scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention"
    assert "没有登记 worktree_path" in status["error"]
    assert any("元数据缺失" in action for action in actions)
    assert len(fakes.notice_calls) == 1


def test_checkpoint_shift_is_idempotent(tmp_path, monkeypatch):
    """重复 tick 不得多打第二颗同内容 commit（checkpoint_done 锁）。"""
    Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    tid = make_task()
    _register_tree(proj, tid, "夜间重构")
    wt = Path(store.read_status(tid)["worktree_path"])
    (wt / "canary.txt").write_text("活\n", encoding="utf-8")
    status = store.read_status(tid)
    now = NOW
    cfg = _config_for(proj)
    blocked = scheduler._checkpoint_shift(store.load_task(tid), status, cfg, now)
    assert blocked is None
    sha = store.read_status(tid)["checkpoint_sha"]
    # 第二次直接调：checkpoint_done 已锁，什么都不做
    blocked = scheduler._checkpoint_shift(
        store.load_task(tid), store.read_status(tid), cfg, now)
    assert blocked is None
    assert store.read_status(tid)["checkpoint_sha"] == sha
    count = subprocess.run(
        ["git", "-C", str(wt), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert count == "2"  # init + 一颗存档点


def test_worktree_exited_with_handover_checkpoints_first(tmp_path, monkeypatch):
    """exited（会话被关但交接写完）也是收工边界：先存档再判 NEXT。"""
    Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _config_for(proj)
    tid = make_task()
    wt = _register_tree(proj, tid, "夜间重构")
    (wt / "canary.txt").write_text("活\n", encoding="utf-8")
    store.update_status(tid, state="exited", exit_reason="window_gone")
    _write_handover(tid, "写完交接会话就被关了。\nNEXT: continue")
    scheduler.tick(cfg, NOW)
    status = store.read_status(tid)
    assert status["state"] == "chained"
    assert status.get("checkpoint_sha")


def test_worktree_false_manual_done_still_finished(tmp_path, monkeypatch):
    """显式 worktree=false：原状态机一字不变，done → finished。"""
    Fakes(monkeypatch)
    tid = make_task(worktree=False, review={"enabled": False, "merge_policy": "manual"})
    _go_idle(tid)
    _write_handover(tid, "干完了。\nNEXT: done")
    scheduler.tick(CONFIG, NOW)
    assert store.read_status(tid)["state"] == "finished"


# ---------- 额度刷新（零开销） ----------


def test_quota_refreshed_only_when_active_and_stale(monkeypatch):
    fakes = Fakes(monkeypatch)
    tid = make_task(title="刷新对象", run_at=scheduler.to_iso(NOW + timedelta(hours=1)))
    # 没活跃任务：quota.json 不存在也不刷
    scheduler.tick(CONFIG, NOW)
    assert fakes.fetch_calls == []
    assert not (store.home() / "quota.json").exists()

    # 有活跃任务 → 刷，写盘 fetched_at == now
    store.update_status(tid, state="working", window_id="@6", pane_pid=NO_PID)
    scheduler.tick(CONFIG, NOW)
    assert len(fakes.fetch_calls) == 1
    data = json.loads((store.home() / "quota.json").read_text(encoding="utf-8"))
    assert data["fetched_at"] == scheduler.to_iso(NOW)
    assert data["usage"]["session_pct"] == 13

    # 刚刷过（5 分钟 < 30 分钟）→ 不刷
    scheduler.tick(CONFIG, NOW + timedelta(minutes=5))
    assert len(fakes.fetch_calls) == 1

    # 过期（31 分钟 ≥ 30 分钟）→ 再刷
    scheduler.tick(CONFIG, NOW + timedelta(minutes=31))
    assert len(fakes.fetch_calls) == 2


def test_quota_refresh_error_written_to_file(monkeypatch):
    fakes = Fakes(monkeypatch)
    fakes.usage_exc = quota.UsageUnavailable("坏了")
    tid = make_task(title="刷新失败", run_at=scheduler.to_iso(NOW + timedelta(hours=1)))
    store.update_status(tid, state="working", window_id="@6", pane_pid=NO_PID)
    scheduler.tick(CONFIG, NOW)
    data = json.loads((store.home() / "quota.json").read_text(encoding="utf-8"))
    assert "error" in data and "坏了" in data["error"]
    assert data["fetched_at"] == scheduler.to_iso(NOW)


# ---------- run_forever：单轮异常吞掉并记日志 ----------


def test_run_forever_swallows_tick_error(tmp_path, monkeypatch):
    def boom(config, now=None):
        raise RuntimeError("炸了")

    monkeypatch.setattr(scheduler, "tick", boom)
    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: None)
    scheduler.run_forever(CONFIG, max_ticks=1)  # 不许往外抛
    log = (store.home() / "scheduler.log").read_text(encoding="utf-8")
    assert "炸了" in log


def test_waiting_wakeup_not_finished_nor_poked(monkeypatch):
    """等闹钟：不收尾、不续班、不 send-keys；刷新时间没到的 idle 也不收尾。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append(t))
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: None)
    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    tid = make_task()
    store.update_status(tid, state="waiting_wakeup", window_id="@1", pane_pid=1,
                        last_event_at="2026-08-27T09:00:00Z",
                        quota_paused_until="2026-08-27T13:00:00Z")
    sched.tick(CONFIG, now)
    assert store.read_status(tid)["state"] == "waiting_wakeup" and sent == []
    # 刷新时间没到、它却没定闹钟就停了（idle）→ 也不收尾
    store.update_status(tid, state="idle")
    sched.tick(CONFIG, now)
    assert store.read_status(tid)["state"] == "idle" and sent == []
    # 刷新时间到了还 idle → 敲一句继续，只敲一次
    later = datetime(2026, 8, 27, 13, 5, tzinfo=timezone.utc)
    sched.tick(CONFIG, later)
    assert len(sent) == 1 and "额度应已刷新" in sent[0]
    st = store.read_status(tid)
    assert st["quota_resume_sent"] and st["quota_paused_until"] is None
    sched.tick(CONFIG, later)
    assert len(sent) == 1


def test_run_forever_reloads_config_each_tick(tmp_path, monkeypatch):
    """网页改了 config.json，下一轮 tick 就要用新的，不能等重启。"""
    import nightshift.scheduler as sched
    seen = []
    monkeypatch.setattr(sched, "tick", lambda cfg, now: seen.append(cfg.get("marker")) or [])
    monkeypatch.setattr(sched.time, "sleep", lambda s: store.atomic_write_json(store.home() / "config.json", {**CONFIG, "marker": "second"}))
    store.atomic_write_json(store.home() / "config.json", {**CONFIG, "marker": "first"})
    sched.run_forever({**CONFIG, "marker": "stale"}, max_ticks=2)
    assert seen == ["first", "second"]
