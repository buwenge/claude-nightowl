"""scheduler 的测试：tick 全分支、崩溃恢复、保活戳、推迟/失败窗口、run_forever 兜底。

launcher / quota 全部 monkeypatch 成可控假函数；时间用固定的 aware datetime 注入。
"""

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nightshift import background_runner, launcher, quota, scheduler, store, worktree

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
        monkeypatch.setattr(quota, "fetch_usage_claude", self._fetch)

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
        return subprocess.CompletedProcess([], 0)

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
    keys: list[tuple] = []
    monkeypatch.setattr(launcher, "send_escape", lambda wid: escapes.append(wid))
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: keys.append((wid, text)) or subprocess.CompletedProcess([], 0))
    tid = make_task(title="自动中止", guards={"auto_interrupt_minutes": 5})
    stale = scheduler.to_iso(NOW - timedelta(minutes=20))
    store.update_status(tid, state="working", window_id="@56", pane_pid=NO_PID,
                        last_event_at=stale)
    scheduler.tick(CONFIG, NOW)  # 首次标 stuck，stuck_since=NOW，还不到 5 分钟
    assert escapes == []
    assert keys == []
    scheduler.tick(CONFIG, NOW + timedelta(minutes=6))  # 卡住满 6 分钟 ≥ 5
    assert escapes == ["@56"]
    # Esc 不会触发 Stop hook（CC 不认用户中断），必须紧跟着敲一句话进去起
    # 新轮次，靠新轮次自然结束才能真正复原——这是 S5 之后补的裁决项修复。
    assert keys == [("@56", scheduler.DEFAULT_STUCK_INTERRUPT_TEXT.replace(
        "{stuck_minutes}", "5"))]
    assert store.read_status(tid)["auto_interrupted"] is True
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "自动中止" in events
    assert "注入自检提示" in events
    # 不重复
    scheduler.tick(CONFIG, NOW + timedelta(minutes=7))
    assert escapes == ["@56"]
    assert keys == [("@56", scheduler.DEFAULT_STUCK_INTERRUPT_TEXT.replace(
        "{stuck_minutes}", "5"))]


def test_auto_interrupt_uses_configured_text(monkeypatch):
    """模板页能改这段自检提示；占位符 {stuck_minutes} 照样替换。"""
    Fakes(monkeypatch)
    monkeypatch.setattr(launcher, "send_escape", lambda wid: None)
    keys: list[tuple] = []
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: keys.append((wid, text)) or subprocess.CompletedProcess([], 0))
    tid = make_task(title="自定义自检文案", guards={"auto_interrupt_minutes": 3})
    stale = scheduler.to_iso(NOW - timedelta(minutes=20))
    store.update_status(tid, state="working", window_id="@58", pane_pid=NO_PID,
                        last_event_at=stale)
    config = {**CONFIG, "stuck_interrupt_text": "自定义：卡了 {stuck_minutes} 分钟"}
    scheduler.tick(config, NOW)
    scheduler.tick(config, NOW + timedelta(minutes=4))
    assert keys == [("@58", "自定义：卡了 3 分钟")]


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


def test_chain_continue_claude_does_not_close_parent_window(monkeypatch):
    """Claude 换班原样保留旧窗口——一期行为不变，close_windows 不该被调用。"""
    fakes = Fakes(monkeypatch)
    tid = make_task(worktree=False)
    _go_idle(tid)
    _write_handover(tid, "登录页已完成。\n还差支付页。\nNEXT: continue")
    scheduler.tick(CONFIG, NOW)
    assert fakes.close_calls == []


def test_chain_continue_codex_closes_parent_window_after_successor_persisted(monkeypatch):
    """S6.1 A7：Codex 续班要在后继落盘（父任务已经是 chained + successor_id）
    之后才关父班窗口，且只关登记在案的那个 @N，不碰会话/其它窗口。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    monkeypatch.setattr(
        sched.launcher, "send_keys",
        lambda w, t: subprocess.CompletedProcess([], 0),
    )
    tid = make_task_codex(worktree=False)  # 跳过工作树存档点，只测续班本身
    close_calls = []
    call_order = []

    def fake_close_windows(ids, config):
        # 调用发生时，父任务必须已经落盘 chained + successor_id——
        # "发生在后继落盘后"不是靠调用顺序猜的，是断言当时的磁盘状态
        parent_status = store.read_status(tid)
        call_order.append(("close_windows", parent_status.get("state"),
                           bool(parent_status.get("successor_id"))))
        close_calls.append([str(w) for w in ids])
        return [str(w) for w in ids]

    monkeypatch.setattr(sched.launcher, "close_windows", fake_close_windows)

    store.update_status(tid, state="idle", window_id="@7", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW), thread_id="thread-1")
    _write_handover(tid, "已完成第一段。\nNEXT: continue")
    sched.tick(CODEX_CONFIG, NOW)

    assert close_calls == [["@7"]]
    assert call_order == [("close_windows", "chained", True)]
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "已关闭父班窗口 @7" in events


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
    # S7：换班上限现在比较 role_shift，纯 build 链路里两者同步递增
    tid = make_task(shift=3, role_shift=3, worktree=False)  # chain.max_windows=3，这班就是最后一班
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

    # 有活跃任务 → 刷，写盘 fetched_at == now（S6：claude 分片）
    store.update_status(tid, state="working", window_id="@6", pane_pid=NO_PID)
    scheduler.tick(CONFIG, NOW)
    assert len(fakes.fetch_calls) == 1
    data = json.loads((store.home() / "quota.json").read_text(encoding="utf-8"))
    assert data["claude"]["fetched_at"] == scheduler.to_iso(NOW)
    assert data["claude"]["usage"]["session_pct"] == 13
    assert data["codex"] == {}  # 没有活跃的 codex 任务，不碰它那份

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
    assert "坏了" in data["claude"]["error"]
    assert data["claude"]["fetched_at"] == scheduler.to_iso(NOW)


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
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append(t) or subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: None)
    # 总review F8：waiting_wakeup/idle 都在 ACTIVE_STATES 里，tick 末尾
    # _maybe_refresh_quota 会真调 fetch_usage_claude——照 D10 那行假掉。
    monkeypatch.setattr(sched.quota, "fetch_usage_claude",
                         lambda c: {"session_pct": 1, "week_all_pct": 1, "per_model": {}, "raw": ""})
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


def test_idle_after_alarm_expired_and_hook_cleared_pause_not_repoked(monkeypatch):
    """F2 反例（原 A 组报告 N2，反例 test_a6）：Claude build 五小时线到线
    自己设了缓存闹钟（waiting_wakeup）→ 闹钟响完自己接着干、干完写交接、
    Stop——hook 这次 Stop 已经按 F2 清掉过期的 quota_paused_until，调度器
    这一 tick 不该再补敲"额度应已刷新，请继续"，而是正常进入换班判定。"""
    from nightshift import hook

    fakes = Fakes(monkeypatch)
    tid = make_task(worktree=False)
    paused_until = NOW + timedelta(hours=2)
    store.update_status(tid, state="waiting_wakeup", window_id="@1", pane_pid=NO_PID,
                        quota_paused_until=scheduler.to_iso(paused_until),
                        session_crons=[{"id": "c1"}], last_event_at=scheduler.to_iso(NOW))
    _write_handover(tid, "干完了。\nNEXT: done")
    # 闹钟响完模型自己接着干完，干完写交接、Stop（没有闹钟了）；hook 用的
    # 是真实墙上时钟（store.utc_now_iso()），paused_until 落在测试用的固定
    # NOW 附近，早已过去，这次 Stop 会把它清掉。
    hook.handle_event(tid, "Stop", {"last_assistant_message": "干完了。\nNEXT: done"})
    status = store.read_status(tid)
    assert status["state"] == "idle"
    assert "quota_paused_until" not in status  # F2：hook 已经清掉

    scheduler.tick(CONFIG, paused_until + timedelta(minutes=30))
    resumes = [t for _, t in fakes.send_keys_calls if "额度应已刷新" in t]
    assert resumes == [], f"quota_paused_until 已经被 hook 清掉，调度器不该再敲：{resumes}"
    assert store.read_status(tid)["state"] == "finished"


def test_claude_waiting_wakeup_grace_period_59_minutes_not_poked(monkeypatch):
    """F3：闹钟没丢的正常路径——刷新时间过了不到 60 分钟，调度器还得再等
    它自己醒，不许提前敲。"""
    fakes = Fakes(monkeypatch)
    tid = make_task()
    paused_until = NOW - timedelta(minutes=59)
    store.update_status(tid, state="waiting_wakeup", window_id="@1", pane_pid=NO_PID,
                        quota_paused_until=scheduler.to_iso(paused_until),
                        last_event_at=scheduler.to_iso(NOW - timedelta(hours=1)))
    scheduler.tick(CONFIG, NOW)
    assert fakes.send_keys_calls == []
    status = store.read_status(tid)
    assert status["state"] == "waiting_wakeup"
    assert status["quota_paused_until"] == scheduler.to_iso(paused_until)


def test_claude_waiting_wakeup_grace_period_61_minutes_poked_once(monkeypatch):
    """F3：闹钟大概率丢了（CC 的 cron 没触发）——超过 60 分钟宽限期，调度器
    主动敲一句让它继续，且只敲这一次（quota_resume_sent 落盘后下一 tick
    不再重复）。"""
    fakes = Fakes(monkeypatch)
    tid = make_task()
    paused_until = NOW - timedelta(minutes=61)
    store.update_status(tid, state="waiting_wakeup", window_id="@1", pane_pid=NO_PID,
                        quota_paused_until=scheduler.to_iso(paused_until),
                        last_event_at=scheduler.to_iso(NOW - timedelta(hours=1)))
    scheduler.tick(CONFIG, NOW)
    assert len(fakes.send_keys_calls) == 1
    _, text = fakes.send_keys_calls[0]
    assert "额度应已刷新" in text
    status = store.read_status(tid)
    assert status["quota_resume_sent"] is True
    assert status["quota_paused_until"] is None
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "额度刷新已过 60 分钟仍未自醒" in events

    # 状态还没被下一次 UserPromptSubmit 事件推进（测试里没有真的 hook），
    # 但 quota_resume_sent 已经落盘，下一 tick 不该再敲第二次
    scheduler.tick(CONFIG, NOW + timedelta(minutes=1))
    assert len(fakes.send_keys_calls) == 1


# ---------- S6③：Codex 额度、按 runner 预检、缓存唤醒、保活分家 ----------

CODEX_CONFIG = {
    **CONFIG,
    "runners": {
        # S6.1 B3：runners.claude 存在时是唯一权威源（不再退回顶层
        # claude_bin/probe_model），这里必须显式带全，不能只带部分字段
        # 指望顶层兜底——那正是 B3 要堵死的分裂口子。
        "claude": {"bin": CONFIG["claude_bin"], "probe_model": CONFIG["probe_model"],
                   "models": CONFIG["models"], "efforts": CONFIG["efforts"],
                   "keepalive_idle_minutes": 50},
        "codex": {"bin": "codex", "profile": "nightowl",
                  "models": {"gpt-5.6-luna": {"context_limit": None}},
                  "efforts": ["low", "medium", "high", "xhigh"],
                  "keepalive_idle_minutes": 25},
    },
}


def make_task_codex(**over):
    task = {
        "title": "Codex 夜间重构",
        "project": "demo",
        "runner": "codex",
        "model": "gpt-5.6-luna",
        "effort": "high",
        "run_at": scheduler.to_iso(NOW),
        "task_text": "正文",
        "prompt_final": "提示词",
    }
    task.update(over)
    return store.create_task(task, CODEX_CONFIG)


def test_try_launch_codex_skips_claude_trust_check(monkeypatch):
    """Codex 任务不查 ~/.claude.json；哪怕它整个不存在也照跑预检。"""
    fakes = Fakes(monkeypatch, trusted=False)  # is_trusted 恒定 False
    monkeypatch.setattr(quota, "fetch_usage_codex", lambda config, timeout=15.0: dict(fakes.usage))
    tid = make_task_codex()
    scheduler.tick(CODEX_CONFIG, NOW)
    assert store.read_status(tid)["state"] == "launching"
    assert fakes.failure_calls == []


def test_claude_quota_bad_does_not_block_codex_launch(monkeypatch):
    """一家额度坏了不能拦另一家起跑：Claude 查不到额度时 Codex 照样能起跑。"""
    fakes = Fakes(monkeypatch)
    fakes.usage_exc = quota.UsageUnavailable("claude 额度查不到")
    monkeypatch.setattr(quota, "fetch_usage_codex", lambda config, timeout=15.0: dict(fakes.usage))
    codex_tid = make_task_codex()
    claude_tid = make_task(project="other")  # 不同目录，不撞同目录锁
    scheduler.tick(CODEX_CONFIG, NOW)
    assert store.read_status(codex_tid)["state"] == "launching"
    assert store.read_status(claude_tid)["state"] == "postponed"


def test_try_launch_codex_writes_quota_source_and_codex_slice(monkeypatch):
    fakes = Fakes(monkeypatch)
    codex_usage = {**usage_fixture(), "session_pct": 5}
    monkeypatch.setattr(quota, "fetch_usage_codex", lambda config, timeout=15.0: dict(codex_usage))
    tid = make_task_codex()
    scheduler.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["quota_at_launch"]["quota_source"] == "codex"
    data = json.loads((store.home() / "quota.json").read_text(encoding="utf-8"))
    assert data["codex"]["usage"]["session_pct"] == 5
    assert data["claude"] == {}  # 只查了这一班自己的 runner


def test_maybe_refresh_quota_independent_per_runner(monkeypatch):
    claude_calls = []
    codex_calls = []
    monkeypatch.setattr(
        quota, "fetch_usage_claude",
        lambda config, timeout=120: claude_calls.append(1) or usage_fixture(),
    )
    monkeypatch.setattr(
        quota, "fetch_usage_codex",
        lambda config, timeout=15.0: codex_calls.append(1) or {**usage_fixture(), "session_pct": 7},
    )
    actions: list[str] = []
    scheduler._maybe_refresh_quota(CODEX_CONFIG, NOW, actions, runners={"claude"})
    assert len(claude_calls) == 1 and len(codex_calls) == 0
    scheduler._maybe_refresh_quota(CODEX_CONFIG, NOW, actions, runners={"codex"})
    assert len(claude_calls) == 1 and len(codex_calls) == 1
    # claude 那份还新鲜（0 分钟前），再刷不会重复调用；codex 同理
    scheduler._maybe_refresh_quota(CODEX_CONFIG, NOW, actions, runners={"claude", "codex"})
    assert len(claude_calls) == 1 and len(codex_calls) == 1
    data = json.loads((store.home() / "quota.json").read_text(encoding="utf-8"))
    assert data["claude"]["usage"]["session_pct"] == 13
    assert data["codex"]["usage"]["session_pct"] == 7


def test_codex_working_over_session_line_sends_pause_and_waits(monkeypatch):
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append(t) or subprocess.CompletedProcess([], 0))
    tid = make_task_codex(guards={"session_pct_max": 80, "weekly_pct_max": 95})
    quota.write_quota_runner("codex", {
        "usage": {"session_pct": 85, "session_resets": "2026-08-27T20:00:00Z",
                  "week_all_pct": 1, "per_model": {}},
        "fetched_at": scheduler.to_iso(NOW), "error": None,
    })
    store.update_status(tid, state="working", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "waiting_wakeup"
    assert status["quota_paused_until"] == "2026-08-27T20:00:00Z"
    assert len(sent) == 1 and "五小时额度" in sent[0]
    # 已经停下了，同一轮/下一轮不该重复敲
    sched.tick(CODEX_CONFIG, NOW)
    assert len(sent) == 1


def test_codex_working_session_equal_line_also_pauses(monkeypatch):
    """总review F7：`_check_codex_quota_pause` 的比较改成 >=，跟
    `quota.check_guards` 的口径统一——刚好等于线也该停，不用真的超过。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append(t) or subprocess.CompletedProcess([], 0))
    tid = make_task_codex(guards={"session_pct_max": 80, "weekly_pct_max": 95})
    quota.write_quota_runner("codex", {
        "usage": {"session_pct": 80, "session_resets": "2026-08-27T20:00:00Z",
                  "week_all_pct": 1, "per_model": {}},
        "fetched_at": scheduler.to_iso(NOW), "error": None,
    })
    store.update_status(tid, state="working", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "waiting_wakeup"
    assert len(sent) == 1 and "五小时额度" in sent[0]


def test_codex_review_over_session_line_uses_review_text_and_stays_working(monkeypatch):
    """S7.2 阻断六：Codex review 撞五小时线不能走 build 那套协议——旧写法
    发的是 build 语气文案（不要求 NEXT）且转 build 专属的 waiting_wakeup，
    review 随后真的发 Stop 时因为没有合法 NEXT 会被 `_parse_review_verdict`
    判协议缺失、保守转成 fix（把额度暂停误判成代码审查退回）。改法：
    role=review 时发 review 语气文案（要求 NEXT: pending）、send-keys 成功
    后不改 state（留在 working，等它真的发 Stop）。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(
        sched.launcher, "send_keys",
        lambda w, t: sent.append(t) or subprocess.CompletedProcess([], 0),
    )
    tid = store.create_task({
        "title": "codex 审稿", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "run_at": scheduler.to_iso(NOW),
        "task_text": "正文", "prompt_final": "提示词",
        "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
        "review": {"enabled": True, "runner": "codex", "model": "gpt-5.6-luna", "effort": "high"},
    }, CODEX_CONFIG)
    data = store.load_task(tid)
    data["role"] = "review"
    store.atomic_write_json(store.task_dir(tid) / "task.json", data)
    quota.write_quota_runner("codex", {
        "usage": {"session_pct": 85, "session_resets": "2026-08-27T20:00:00Z",
                  "week_all_pct": 1, "per_model": {}},
        "fetched_at": scheduler.to_iso(NOW), "error": None,
    })
    # S7.5：审稿流水线要求 worktree_path 元数据（真实场景下 review.enabled=true
    # 早已强制 worktree=true，build 阶段就已经建好树）；这条测试只关心
    # Codex 五小时线协议，不走真 git 工作树，补一个占位路径满足前置条件。
    store.update_status(tid, state="working", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW),
                        worktree_path="/tmp/ns-codex-review-worktree",
                        branch="ns/codex-review", base_ref="deadbeef")
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "working"  # 不转 build 专属的 waiting_wakeup
    assert status["quota_paused_until"] == "2026-08-27T20:00:00Z"
    assert len(sent) == 1
    assert "NEXT: pending" in sent[0]
    assert "缓存闹钟" not in sent[0] and "ScheduleWakeup" not in sent[0]

    # 随后模拟它真的回复 NEXT:pending（走 S7.1②建好的 review 统一 idle →
    # _check_review_idle → _review_pending 路径，不再被 parser 判协议缺失）
    review_file = store.task_dir(tid) / "review-1.md"
    review_file.write_text("额度到线，没看完。\n\nNEXT: pending", encoding="utf-8")
    store.update_status(
        tid, state="idle", review_verdict="pending", review_verdict_final=False,
        review_file=str(review_file), review_recorded_round=1,
    )
    sched.tick(CODEX_CONFIG, NOW)
    after = store.read_status(tid)
    # 默认 on_no_quota=release：另起同轮审稿，不是被当 fix 误判返工
    assert after["state"] == "chained"
    new_review = store.load_task(after["successor_id"])
    assert new_review["role"] == "review" and new_review["round"] == 1


def test_codex_review_over_session_line_then_pending_hold_resumes(monkeypatch):
    """S7.2 阻断六后续：on_no_quota=hold 场景下，Codex review 五小时线
    → NEXT:pending → held，_review_hold_resume_eta 应该优先复用
    `_check_codex_quota_pause` 已经落盘的精确 `quota_paused_until`（不是
    自己另估一个），到点能被 review 专属恢复文案叫醒。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(
        sched.launcher, "send_keys",
        lambda w, t: sent.append(t) or subprocess.CompletedProcess([], 0),
    )
    cfg = dict(CODEX_CONFIG)
    cfg["review"] = {"max_rounds": 5, "on_no_quota": "hold", "merge_policy": "manual"}
    tid = store.create_task({
        "title": "codex 审稿 hold", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "run_at": scheduler.to_iso(NOW),
        "task_text": "正文", "prompt_final": "提示词",
        "guards": {"session_pct_max": 80, "weekly_pct_max": 95},
        "review": {"enabled": True, "runner": "codex", "model": "gpt-5.6-luna", "effort": "high",
                   "on_no_quota": "hold"},
    }, cfg)
    data = store.load_task(tid)
    data["role"] = "review"
    store.atomic_write_json(store.task_dir(tid) / "task.json", data)
    quota.write_quota_runner("codex", {
        "usage": {"session_pct": 85, "session_resets": "2026-08-27T20:00:00Z",
                  "week_all_pct": 1, "per_model": {}},
        "fetched_at": scheduler.to_iso(NOW), "error": None,
    })
    store.update_status(tid, state="working", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    sched.tick(cfg, NOW)
    paused_until = store.read_status(tid)["quota_paused_until"]
    assert paused_until == "2026-08-27T20:00:00Z"

    review_file = store.task_dir(tid) / "review-1.md"
    review_file.write_text("额度到线，没看完。\n\nNEXT: pending", encoding="utf-8")
    store.update_status(
        tid, state="idle", review_verdict="pending", review_verdict_final=False,
        review_file=str(review_file), review_recorded_round=1,
    )
    sched.tick(cfg, NOW)
    held_status = store.read_status(tid)
    assert held_status["state"] == "held"
    # _review_hold_resume_eta 复用了已经精确落盘的 quota_paused_until，
    # 不是另估一个模糊值
    assert held_status["quota_paused_until"] == paused_until

    after_refresh = datetime(2026, 8, 27, 20, 5, tzinfo=timezone.utc)
    sched.tick(cfg, after_refresh)
    resumed = store.read_status(tid)
    assert resumed["state"] == "working"
    assert resumed["review_awaiting_verdict"] is True
    assert len(sent) == 2  # 第一次五小时线提醒 + 这次恢复
    assert "NEXT: pending" not in sent[-1] or "继续" in sent[-1]  # 恢复文案要求继续审稿


def test_codex_waiting_wakeup_actively_woken_unlike_claude(monkeypatch):
    """S6③ 核心行为差异：Claude 的 waiting_wakeup 等它自己醒（不敲）；
    Codex 没有这个能力，到点必须调度器主动敲，只敲一次。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append(t) or subprocess.CompletedProcess([], 0))
    tid = make_task_codex()
    store.update_status(
        tid, state="waiting_wakeup", window_id="@1", pane_pid=1,
        last_event_at=scheduler.to_iso(NOW - timedelta(hours=1)),
        quota_paused_until="2026-08-27T19:00:00Z",
    )
    before = NOW  # 18:00，还没到刷新时间
    sched.tick(CODEX_CONFIG, before)
    assert sent == [] and store.read_status(tid)["state"] == "waiting_wakeup"

    after = datetime(2026, 8, 27, 19, 5, tzinfo=timezone.utc)
    sched.tick(CODEX_CONFIG, after)
    assert len(sent) == 1 and "继续" in sent[0]
    status = store.read_status(tid)
    assert status["quota_resume_sent"] is True
    assert status["quota_paused_until"] is None
    sched.tick(CODEX_CONFIG, after)
    assert len(sent) == 1  # 只敲一次


def test_keepalive_codex_25_minutes_claude_50_minutes(monkeypatch):
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append(t) or subprocess.CompletedProcess([], 0))
    # 总review F8：两个任务都在 waiting_background（ACTIVE_STATES），tick
    # 末尾会真调 fetch_usage_claude——照 D10 那行假掉。
    monkeypatch.setattr(sched.quota, "fetch_usage_claude",
                         lambda c: {"session_pct": 1, "week_all_pct": 1, "per_model": {}, "raw": ""})

    codex_tid = make_task_codex()
    store.update_status(codex_tid, state="waiting_background", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    claude_tid = make_task(project="other")
    store.update_status(claude_tid, state="waiting_background", window_id="@2", pane_pid=2,
                        last_event_at=scheduler.to_iso(NOW))

    at_26 = NOW + timedelta(minutes=26)
    sched.tick(CODEX_CONFIG, at_26)
    # Codex 25 分钟到线该戳了；Claude 50 分钟还没到
    assert len(sent) == 1
    assert store.read_status(codex_tid)["keepalive_count"] == 1
    assert "keepalive_count" not in store.read_status(claude_tid)

    at_51 = NOW + timedelta(minutes=51)
    sched.tick(CODEX_CONFIG, at_51)
    assert store.read_status(claude_tid)["keepalive_count"] == 1


# ---------- S6④：Codex 后台登记簿核对（F12） ----------


def _codex_running_task(monkeypatch, sched, window_id="@1"):
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append((w, t)) or subprocess.CompletedProcess([], 0))
    tid = make_task_codex()
    store.update_status(tid, state="idle", window_id=window_id, pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    return tid, sent


def test_codex_idle_with_running_background_forced_back(monkeypatch):
    """核心不变量：登记簿里还有 running 项，idle 必须被摁回 waiting_background，
    不许收尾、不许存档、不许换班。"""
    import nightshift.scheduler as sched
    tid, sent = _codex_running_task(monkeypatch, sched)
    background_runner.modify_registry(tid, lambda d: d.update(
        {"bg-1": {"state": "running", "background_id": "bg-1"}}
    ))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "waiting_background"
    assert sent == []  # 只是纠正状态，不是完成通知，不该敲键
    assert "chain_checked" not in status  # 没走到 idle-chain 那条路


def test_codex_running_background_stale_heartbeat_needs_attention(monkeypatch):
    """原 wrapper 心跳超时（大概率丢了：那次 exec 的沙箱/PID namespace 没了）：
    不能让任务永远卡在 waiting_background 等一个再也不会来的完成事件。"""
    import nightshift.scheduler as sched
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    tid, sent = _codex_running_task(monkeypatch, sched)
    stale_hb = scheduler.to_iso(NOW - timedelta(seconds=200))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "running", "background_id": "bg-1", "heartbeat_at": stale_hb},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention"
    assert sent == []  # 不敲错窗口
    assert len(notices) == 1

    # 重复 tick：同一次心跳超时不许反复告警
    sched.tick(CODEX_CONFIG, NOW + timedelta(seconds=30))
    assert len(notices) == 1


def test_codex_running_background_fresh_heartbeat_stays_waiting_background(monkeypatch):
    """心跳新鲜（原 wrapper 还活着、还在正常跑）：不该被误判成丢失。"""
    import nightshift.scheduler as sched
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    tid, sent = _codex_running_task(monkeypatch, sched)
    fresh_hb = scheduler.to_iso(NOW - timedelta(seconds=2))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "running", "background_id": "bg-1", "heartbeat_at": fresh_hb},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "waiting_background"
    assert notices == []


def test_codex_running_background_missing_heartbeat_not_treated_as_stale(monkeypatch):
    """刚起、wrapper 还没来得及写第一次心跳（没有 heartbeat_at 字段）：不能
    误判成丢失，走正常的 waiting_background 路径。"""
    import nightshift.scheduler as sched
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    tid, sent = _codex_running_task(monkeypatch, sched)
    background_runner.modify_registry(tid, lambda d: d.update(
        {"bg-1": {"state": "running", "background_id": "bg-1"}}
    ))
    sched.tick(CODEX_CONFIG, NOW)
    assert store.read_status(tid)["state"] == "waiting_background"
    assert notices == []


def test_codex_healthy_background_not_stuck_and_not_auto_interrupted(monkeypatch):
    """S6.1 A5 真机复现：`last_event_at` 20 分钟前（按通用标准早该判卡住/
    自动 Esc 了）+ F12 心跳刚刚——这是"前台安静但后台任务健康在跑"，不是
    "卡在一条工具调用里没反应"，不该被通用卡住判定误伤，更不该被自动 Esc
    打断一个跟这个后台进程毫不相干的前台会话。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    escaped = []
    monkeypatch.setattr(sched.launcher, "send_escape", lambda w: escaped.append(w))
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append((w, t)) or subprocess.CompletedProcess([], 0))

    tid = make_task_codex(guards={
        "session_pct_max": 80, "weekly_pct_max": 95, "auto_interrupt_minutes": 5,
    })
    stale_event = scheduler.to_iso(NOW - timedelta(minutes=20))
    store.update_status(tid, state="waiting_background", window_id="@1", pane_pid=1,
                        last_event_at=stale_event, stuck_since=stale_event)
    fresh_hb = scheduler.to_iso(NOW - timedelta(seconds=2))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "running", "background_id": "bg-1", "heartbeat_at": fresh_hb},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert not status.get("stuck")
    assert not status.get("auto_interrupted")
    assert escaped == []
    # 保活戳（跟卡住判定是两条独立逻辑，不该被这个改动误伤）不受影响：
    # waiting_background 静默超过 25 分钟该戳还是会戳，这里 20 分钟不到线
    assert sent == []


def test_codex_background_heartbeat_stale_seconds_config_override(monkeypatch):
    """`scheduler.background_heartbeat_stale_seconds` 可配置宽限，不是硬编码。"""
    import nightshift.scheduler as sched
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    tid, sent = _codex_running_task(monkeypatch, sched)
    hb_30s_ago = scheduler.to_iso(NOW - timedelta(seconds=30))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "running", "background_id": "bg-1", "heartbeat_at": hb_30s_ago},
    }))
    tight_config = {
        **CODEX_CONFIG,
        "scheduler": {**CODEX_CONFIG["scheduler"], "background_heartbeat_stale_seconds": 10},
    }
    sched.tick(tight_config, NOW)
    # 30 秒前的心跳，宽限只有 10 秒：该判丢失
    assert store.read_status(tid)["state"] == "needs_attention"
    assert len(notices) == 1


def test_codex_finished_background_notifies_and_marks_once(monkeypatch):
    import nightshift.scheduler as sched
    tid, sent = _codex_running_task(monkeypatch, sched)
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/x.log", "notification_state": "pending"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    assert len(sent) == 1
    assert "bg-1" in sent[0][1] and "/tmp/x.log" in sent[0][1]
    registry = background_runner.load_registry(tid)
    assert registry["bg-1"]["notification_state"] == "notified"
    assert store.read_status(tid)["state"] == "waiting_background"

    # 重复 tick：同一个 completion 不许再敲一次
    sched.tick(CODEX_CONFIG, NOW + timedelta(seconds=30))
    assert len(sent) == 1


def test_codex_multiple_finished_items_combine_into_one_message(monkeypatch):
    import nightshift.scheduler as sched
    tid, sent = _codex_running_task(monkeypatch, sched)
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/a.log", "notification_state": "pending"},
        "bg-2": {"state": "finished", "background_id": "bg-2", "exit_code": 1,
                 "result_path": "/tmp/b.log", "notification_state": "pending"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    assert len(sent) == 1  # 合并成一条
    assert "bg-1" in sent[0][1] and "bg-2" in sent[0][1]
    # 总review F9：多条完成通知拼单行用中文分号，不再是裸 LF 块（Codex
    # TUI 对 paste-buffer -r 保留的裸换行块未验证过是否安全）。
    assert "\n" not in sent[0][1]
    assert "；" in sent[0][1]


def test_codex_finished_plus_still_running_stays_waiting_background(monkeypatch):
    """有的完成了、有的还在跑：通知完那个完成的，但仍留在 waiting_background。"""
    import nightshift.scheduler as sched
    tid, sent = _codex_running_task(monkeypatch, sched)
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/a.log", "notification_state": "pending"},
        "bg-2": {"state": "running", "background_id": "bg-2"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    assert len(sent) == 1
    status = store.read_status(tid)
    assert status["state"] == "waiting_background"


def test_codex_background_finished_window_gone_needs_attention(monkeypatch):
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: False)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append((w, t)) or subprocess.CompletedProcess([], 0))
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    tid = make_task_codex()
    # S6.1 A4：F12 现在跑在通用 alive 检查之前，直接调用私有 helper 单测本体
    # 依旧有用（针对性测边界），但真正证明"真实 tick 也走得到"的是下面
    # test_codex_tick_background_finished_window_gone_reaches_needs_attention
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/a.log", "notification_state": "pending"},
    }))
    status = store.read_status(tid)
    result = sched._reconcile_codex_background(
        store.load_task(tid), status, CODEX_CONFIG, NOW, "@1", alive=False,
    )
    assert result is not None
    assert store.read_status(tid)["state"] == "needs_attention"
    assert sent == []  # 不敲错窗口
    assert len(notices) == 1


def test_codex_tick_background_finished_window_gone_reaches_needs_attention(monkeypatch):
    """S6.1 A4 的核心反例：真实 scheduler.tick()（不是直接调用私有 helper）
    走完整 window_gone 判断路径时，F12 的 needs_attention 必须真的够得到——
    改之前通用 alive 检查会抢在前面把它判成 exited(window_gone)，F12 自己
    的分支永远没机会跑。这里任务状态故意是 working（不是 idle/
    waiting_background），验证"窗口消失"这条不分顶层状态都能触发。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: False)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append((w, t)) or subprocess.CompletedProcess([], 0))
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    tid = make_task_codex()
    store.update_status(tid, state="working", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/a.log", "notification_state": "pending"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention", status
    assert sent == []  # 不敲错窗口
    assert len(notices) == 1
    # 不该被通用 window_gone 分支抢答成 exited
    assert status.get("exit_reason") != "window_gone"


def test_codex_tick_background_thread_mismatch_needs_attention(monkeypatch):
    """S6.1 A4：窗口活着，但登记时的 thread_id 跟当前 status 的 thread_id
    不一样（比如同一个 @N 窗口号先后属于不同 session）——不敢冒充通知，
    走 needs_attention，不能敲进一个其实不是那次后台任务发起者的会话。"""
    import nightshift.scheduler as sched
    tid, sent = _codex_running_task(monkeypatch, sched)
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    store.update_status(tid, thread_id="thread-current")
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/a.log", "notification_state": "pending",
                 "thread_id_at_start": "thread-stale"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention"
    assert sent == []  # 不敲错会话
    assert len(notices) == 1


def test_codex_tick_fresh_running_window_gone_needs_attention(monkeypatch):
    """二次返修阻断一反例①：登记簿里只有一个新鲜 running 项（不是
    finished/stopped），窗口却已经消失——旧代码只在 finished_pending 分支查
    alive，running 项会绕过检查被通用分支抢答成 exited(window_gone)，F12
    自己的 needs_attention 永远没机会触发。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: False)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append((w, t)) or subprocess.CompletedProcess([], 0))
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    tid = make_task_codex()
    store.update_status(tid, state="working", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    fresh_hb = scheduler.to_iso(NOW - timedelta(seconds=2))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "running", "background_id": "bg-1", "heartbeat_at": fresh_hb},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention", status
    assert sent == []
    assert len(notices) == 1
    assert status.get("exit_reason") != "window_gone"  # 不该被通用分支抢答成 exited


def test_codex_tick_fresh_running_thread_mismatch_needs_attention(monkeypatch):
    """二次返修阻断一反例②：登记簿里只有一个新鲜 running 项，窗口还活着，
    但登记时的 thread_id 跟当前 status 的 thread_id 对不上号——旧代码只在
    finished_pending 分支查 mismatch，running 项会被摁回 waiting_background
    当作什么都没发生，实际上这个窗口现在属于别的 session。"""
    import nightshift.scheduler as sched
    tid, sent = _codex_running_task(monkeypatch, sched)
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    store.update_status(tid, thread_id="thread-current")
    fresh_hb = scheduler.to_iso(NOW - timedelta(seconds=2))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "running", "background_id": "bg-1", "heartbeat_at": fresh_hb,
                 "thread_id_at_start": "thread-stale"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention", status
    assert sent == []  # 不敲错会话
    assert len(notices) == 1
    assert status["state"] != "waiting_background"


def test_codex_tick_only_notified_terminal_window_gone_still_exits_normally(monkeypatch):
    """二次返修阻断一的边界：registry 里没有未收口项（只剩已经 notified 的
    终态），窗口消失应该走回普通的 exited(window_gone)——这是正常退场，
    F12 的核验不该拦下它。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: False)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    tid = make_task_codex()
    store.update_status(tid, state="waiting_background", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/a.log", "notification_state": "notified"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "exited"
    assert status.get("exit_reason") == "window_gone"


def test_codex_tick_background_send_keys_failure_not_marked_notified(monkeypatch):
    """S6.1 A4：send-keys 真的失败（returncode != 0）不能假装已经通知——
    留 pending 转 needs_attention，不许悄悄标 notified 然后没人再理它。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    monkeypatch.setattr(
        sched.launcher, "send_keys",
        lambda w, t: subprocess.CompletedProcess([], 1, stderr="tmux 抽风了"),
    )
    notices = []
    monkeypatch.setattr(sched.launcher, "open_notice_window", lambda *a, **k: notices.append(a))
    tid = make_task_codex()
    store.update_status(tid, state="idle", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/a.log", "notification_state": "pending"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention"
    assert len(notices) == 1
    registry = background_runner.load_registry(tid)
    assert registry["bg-1"]["notification_state"] == "pending"  # 没被谎报成 notified


def test_codex_stopped_background_notifies_and_marks_once(monkeypatch):
    """S6.1 A3：state=stopped（用户主动停后台）也是需要通知的终态，不只
    finished——不然 stop 完的任务永远卡在 waiting_background 没人理。"""
    import nightshift.scheduler as sched
    tid, sent = _codex_running_task(monkeypatch, sched)
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "stopped", "background_id": "bg-1", "exit_code": None,
                 "result_path": "/tmp/a.log", "notification_state": "pending"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    assert len(sent) == 1
    assert "已停止" in sent[0][1]
    assert "bg-1" in sent[0][1]
    registry = background_runner.load_registry(tid)
    assert registry["bg-1"]["notification_state"] == "notified"
    assert store.read_status(tid)["state"] == "waiting_background"

    sched.tick(CODEX_CONFIG, NOW + timedelta(seconds=30))
    assert len(sent) == 1  # 同一个 completion 不许再敲一次


def test_codex_background_survives_registry_reread_like_restart(monkeypatch):
    """"scheduler 重启前后不丢事件"离线等价：只要登记簿在磁盘上，
    重新读取（模拟进程重启后的冷启动）也能正确核对，不依赖内存态。"""
    import nightshift.scheduler as sched
    tid, sent = _codex_running_task(monkeypatch, sched)
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/a.log", "notification_state": "pending"},
    }))
    # 全新读一遍登记簿（不依赖任何进程内缓存/全局状态）
    fresh_registry = background_runner.load_registry(tid)
    assert fresh_registry["bg-1"]["state"] == "finished"
    sched.tick(CODEX_CONFIG, NOW)
    assert len(sent) == 1
    assert background_runner.load_registry(tid)["bg-1"]["notification_state"] == "notified"


def test_codex_empty_or_bad_registry_does_not_block_idle_chain(monkeypatch):
    """没有登记簿/登记簿是空文件：不该拦着正常收尾流程（老式非工作树任务，
    避免这里被工作树存档点的机制干扰，那不是这条测试要看的东西）。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append((w, t)) or subprocess.CompletedProcess([], 0))
    tid = make_task_codex(worktree=False)
    store.update_status(tid, state="idle", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    # 没有 background.json：_reconcile 返回 None，交回正常流程
    sched.tick(CODEX_CONFIG, NOW)
    assert sent == []
    status = store.read_status(tid)
    assert status["state"] == "finished"  # 正常按老式路径收尾，没被拦住


def test_codex_working_task_not_disturbed_by_finished_background(monkeypatch):
    """working 中途不该被后台完成打断——等它自然停到 idle/waiting_background
    再通知，免得往正在打字的会话里插话。"""
    import nightshift.scheduler as sched
    monkeypatch.setattr(sched.launcher, "window_alive", lambda *a, **k: True)
    monkeypatch.setattr(sched.launcher, "pid_alive", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sched.launcher, "send_keys", lambda w, t: sent.append((w, t)) or subprocess.CompletedProcess([], 0))
    tid = make_task_codex()
    store.update_status(tid, state="working", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))
    background_runner.modify_registry(tid, lambda d: d.update({
        "bg-1": {"state": "finished", "background_id": "bg-1", "exit_code": 0,
                 "result_path": "/tmp/a.log", "notification_state": "pending"},
    }))
    sched.tick(CODEX_CONFIG, NOW)
    assert sent == []
    assert store.read_status(tid)["state"] == "working"


def test_run_forever_reloads_config_each_tick(tmp_path, monkeypatch):
    """网页改了 config.json，下一轮 tick 就要用新的，不能等重启。"""
    import nightshift.scheduler as sched
    seen = []
    monkeypatch.setattr(sched, "tick", lambda cfg, now: seen.append(cfg.get("marker")) or [])
    monkeypatch.setattr(sched.time, "sleep", lambda s: store.atomic_write_json(store.home() / "config.json", {**CONFIG, "marker": "second"}))
    store.atomic_write_json(store.home() / "config.json", {**CONFIG, "marker": "first"})
    sched.run_forever({**CONFIG, "marker": "stale"}, max_ticks=2)
    assert seen == ["first", "second"]


# ---------- S7：审稿流水线（build ↔ review 轮转、held、返工轮数、我来看） ----------

REVIEW_CONFIG = {
    **CONFIG,
    "review_template": (
        "REVIEW {title} round={round} base={base_ref}\ndiff: {diff_command}\n"
        "交接：{build_handover}\n上一轮：{previous_review}\n标准：{criteria}\n"
        "{stop_build_hint}只读，末行 NEXT。"
    ),
    "review_fix_template": (
        "FIX {title} round={round}\n审稿意见：{review}\n{worktree_instruction}{task}"
    ),
    "review": {"max_rounds": 5, "on_no_quota": "release", "merge_policy": "manual",
               "criteria_text": ""},
}


def make_review_task(**over):
    task = {
        "title": "审稿流水线任务",
        "project": "demo",
        "model": "claude-fable-5",
        "effort": "high",
        "run_at": scheduler.to_iso(NOW),
        "task_text": "正文",
        "prompt_final": "提示词",
        "review": {
            "enabled": True, "runner": "claude",
            "model": "claude-fable-5", "effort": "high",
        },
    }
    task.update(over)
    return store.create_task(task, REVIEW_CONFIG)


def _review_config_for(proj: Path) -> dict:
    cfg = dict(REVIEW_CONFIG)
    cfg["projects"] = {"demo": str(proj), "other": str(proj / "nope")}
    return cfg


def test_review_dont_ask_permission_mode_not_warned(monkeypatch):
    """S7.1 阻断五：review 角色故意用 dontAsk（无人值守拒绝语义，见
    launcher._claude_command），不是"被静默回落成非 auto"，R2 的"没进 auto
    模式"提醒窗不该误伤它。"""
    fakes = Fakes(monkeypatch)
    tid = make_review_task()
    data = store.load_task(tid)
    data["role"] = "review"
    store.atomic_write_json(store.task_dir(tid) / "task.json", data)
    store.update_status(
        tid, state="working", window_id="@1", pane_pid=NO_PID,
        permission_mode="dontAsk",
    )
    scheduler.tick(REVIEW_CONFIG, NOW)
    assert fakes.notice_calls == []
    assert store.read_status(tid).get("mode_warned") is not True

    # 对照：build 角色（同样是 dontAsk，理论上不该出现，但既然出现了就该
    # 按老规则提醒——只有 review 角色的 dontAsk 是合法豁免）
    build_tid = make_task(title="build dontAsk 异常")
    store.update_status(
        build_tid, state="working", window_id="@2", pane_pid=NO_PID,
        permission_mode="dontAsk",
    )
    scheduler.tick(REVIEW_CONFIG, NOW)
    assert len(fakes.notice_calls) == 1
    assert fakes.notice_calls[0][0] == build_tid


def test_tick_refreshes_both_runners_when_codex_build_held_claude_review_working(monkeypatch):
    """S7.1 阻断四 Part A：tick() 一轮里，active_runners 收集只看顶层
    task["runner"] 会漏刷 Codex 施工 + Claude 审稿这类跨家组合——active
    build 顶层 runner 是 codex，但真正在跑的 review 是 claude，两家都要
    刷新，不能只刷 codex。"""
    fakes = Fakes(monkeypatch)
    codex_calls: list[int] = []
    monkeypatch.setattr(
        quota, "fetch_usage_codex",
        lambda config, timeout=15.0: codex_calls.append(1) or dict(fakes.usage),
    )
    cfg = dict(REVIEW_CONFIG, runners=CODEX_CONFIG["runners"])

    build_id = store.create_task({
        "title": "codex 施工", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high",
        "run_at": scheduler.to_iso(NOW), "task_text": "正文", "prompt_final": "提示词",
    }, cfg)
    store.update_status(build_id, state="held", window_id="@1", pane_pid=NO_PID)

    review_id = store.create_task({
        "title": "claude 审稿", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high",
        "run_at": scheduler.to_iso(NOW), "task_text": "正文", "prompt_final": "提示词",
        "review": {"enabled": True, "runner": "claude", "model": "claude-fable-5", "effort": "high"},
    }, cfg)
    data = store.load_task(review_id)
    data["role"] = "review"
    store.atomic_write_json(store.task_dir(review_id) / "task.json", data)
    store.update_status(review_id, state="working", window_id="@2", pane_pid=NO_PID)

    scheduler.tick(cfg, NOW)
    assert len(fakes.fetch_calls) == 1  # claude（review 的有效工人）被刷新
    assert len(codex_calls) == 1  # codex（build 的顶层 runner）也被刷新


def test_review_keepalive_marks_control_turn_before_poking(monkeypatch):
    """S7.1 阻断二：保活探针不要求正式 verdict，发之前要落
    review_awaiting_verdict=False，接下来的 Stop 才会走控制 turn 分支，
    不会被误记成协议缺失→fix。"""
    fakes = Fakes(monkeypatch)
    tid = make_review_task()
    data = store.load_task(tid)
    data["role"] = "review"
    store.atomic_write_json(store.task_dir(tid) / "task.json", data)
    stale = scheduler.to_iso(NOW - timedelta(minutes=51))
    store.update_status(
        tid, state="held", window_id="@5", pane_pid=NO_PID,
        last_event_at=stale, review_awaiting_verdict=True,
    )
    scheduler.tick(REVIEW_CONFIG, NOW)
    assert len(fakes.send_keys_calls) == 1
    status = store.read_status(tid)
    assert status["review_awaiting_verdict"] is False
    assert status["review_control_kind"] == "keepalive"
    assert status["state"] == "held"  # 保活不改流程状态


def test_review_keepalive_send_failure_does_not_pollute_control_state(monkeypatch):
    """S7.2 阻断五.2反例：保活探针 send-keys 失败时，不能照样落
    review_awaiting_verdict=False/review_control_kind/计数字段——那样会让
    接下来一次真实的 verdict Stop 被误判成"控制 turn"直接吞掉，且
    keepalive_count 会在什么都没发出去的情况下虚增。"""
    fakes = Fakes(monkeypatch)
    monkeypatch.setattr(
        launcher, "send_keys",
        lambda w, t: subprocess.CompletedProcess([], 1, "", "send-keys 失败"),
    )
    tid = make_review_task()
    data = store.load_task(tid)
    data["role"] = "review"
    store.atomic_write_json(store.task_dir(tid) / "task.json", data)
    stale = scheduler.to_iso(NOW - timedelta(minutes=51))
    store.update_status(
        tid, state="held", window_id="@5", pane_pid=NO_PID,
        last_event_at=stale, review_awaiting_verdict=True,
    )
    before = dict(store.read_status(tid))
    scheduler.tick(REVIEW_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["review_awaiting_verdict"] is True  # 没被污染成 False
    assert "review_control_kind" not in status
    assert status.get("keepalive_count") == before.get("keepalive_count")  # 没有虚增
    assert status.get("last_keepalive_at") == before.get("last_keepalive_at")
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "keepalive 控制消息投递失败" in events


def test_review_quota_resume_send_failure_does_not_pollute_awaiting_verdict(monkeypatch):
    """S7.2 阻断五.3反例：额度刷新恢复时 send-keys 失败，不能照样先落
    review_awaiting_verdict=True——那样在真正叫醒它之前，任何一次意外的
    控制回复（比如误触发的保活）都会被当成正式 verdict 尝试解析。"""
    fakes = Fakes(monkeypatch)
    monkeypatch.setattr(
        launcher, "send_keys",
        lambda w, t: subprocess.CompletedProcess([], 1, "", "send-keys 失败"),
    )
    tid = make_review_task()
    data = store.load_task(tid)
    data["role"] = "review"
    store.atomic_write_json(store.task_dir(tid) / "task.json", data)
    store.update_status(
        tid, state="held", window_id="@5", pane_pid=NO_PID,
        review_awaiting_verdict=False,
        quota_paused_until=scheduler.to_iso(NOW - timedelta(minutes=1)),
        quota_resume_sent=False,
    )
    scheduler.tick(REVIEW_CONFIG, NOW)
    status = store.read_status(tid)
    assert status["review_awaiting_verdict"] is False  # 没被提前污染成 True
    assert status.get("quota_resume_sent") is not True
    assert status["state"] == "held"  # 没被悄悄摁成 working
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "resume 控制消息投递失败" in events


def test_review_keepalive_fast_stop_before_send_returns_still_treated_as_control(
    monkeypatch,
):
    """S7.3 阻断二反例：假 send_keys 在"返回"之前就同步触发了 review 的
    Stop（模拟真实世界里 send-keys 系统调用返回与目标会话真的处理完/发出
    Stop 之间没有硬先后保证）。保活探针的 review_awaiting_verdict=False/
    review_control_kind=keepalive 必须在 send 之前落盘——旧写法"send 成功
    后才落盘"在这个窗口里是错的：这次 Stop 会被误判协议缺失、记成
    verdict=fix。"""
    fakes = Fakes(monkeypatch)
    tid = make_review_task()
    data = store.load_task(tid)
    data["role"] = "review"
    store.atomic_write_json(store.task_dir(tid) / "task.json", data)
    stale = scheduler.to_iso(NOW - timedelta(minutes=51))
    store.update_status(
        tid, state="held", window_id="@5", pane_pid=NO_PID,
        last_event_at=stale, review_awaiting_verdict=True,
    )

    from nightshift import hook

    def fake_send_keys(wid, text):
        hook.handle_event(tid, "Stop", {"last_assistant_message": "收到，我等着"})
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(launcher, "send_keys", fake_send_keys)
    scheduler.tick(REVIEW_CONFIG, NOW)
    status = store.read_status(tid)
    assert status.get("review_verdict") != "fix"
    assert "review_verdict" not in status


def test_review_quota_resume_fast_stop_before_send_returns_captured_as_verdict(
    monkeypatch,
):
    """S7.3 阻断二反例：resume 发送时假 send_keys 在返回之前同步触发一个
    带真实 NEXT:done 的 Stop（模拟真实世界的竞态）。resume 的
    review_awaiting_verdict=True 必须在 send 之前落盘，这样这次抢先到达
    的 Stop 才会被正确当成正式 verdict 解析、记下来，不会被吞掉；且
    finalize 阶段的 success_only_fields（state=working 等）不能把 Stop
    已经推进的更准确的结果（state=idle + verdict=done）覆盖回去。"""
    fakes = Fakes(monkeypatch)
    tid = make_review_task()
    data = store.load_task(tid)
    data["role"] = "review"
    store.atomic_write_json(store.task_dir(tid) / "task.json", data)
    store.update_status(
        tid, state="held", window_id="@5", pane_pid=NO_PID,
        review_awaiting_verdict=False,
        quota_paused_until=scheduler.to_iso(NOW - timedelta(minutes=1)),
        quota_resume_sent=False,
    )

    from nightshift import hook

    def fake_send_keys(wid, text):
        hook.handle_event(
            tid, "Stop",
            {"last_assistant_message": "额度刷新了，继续看。\n\nNEXT: done"},
        )
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(launcher, "send_keys", fake_send_keys)
    scheduler.tick(REVIEW_CONFIG, NOW)
    status = store.read_status(tid)
    assert status.get("review_verdict") == "done"
    assert status["state"] == "idle"


def test_review_pipeline_fix_then_done_reuses_held_session(tmp_path, monkeypatch):
    """held 会话还活着：fix 直接捎话续第 2 轮，不新开窗口；第 2 轮 done →
    manual 收工 awaiting_merge。全程 fix_count/round/checkpoint 历史正确。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    wt = _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("build round1\n", encoding="utf-8")
    _write_handover(tid, "第一轮写完了。\nNEXT: done")

    actions = scheduler.tick(cfg, NOW)
    parent_status = store.read_status(tid)
    assert parent_status["state"] == "held"
    c1 = parent_status["checkpoint_sha"]
    assert c1 and len(c1) == 40
    review_id = parent_status["successor_id"]
    assert any(review_id in a for a in actions)
    review_task = store.load_task(review_id)
    assert review_task["role"] == "review"
    assert review_task["round"] == 1
    assert review_task["pipeline_id"] == tid
    assert store.read_status(review_id)["state"] == "scheduled"

    # 审稿班起跑并给出 fix（模拟 hook 已落 verdict）
    _go_idle(review_id, window_id="@2")
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("有个边界条件没处理。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(review_id, review_verdict="fix", review_file=str(review_file),
                        review_recorded_round=1)

    scheduler.tick(cfg, NOW)
    build_status = store.read_status(tid)
    assert build_status["state"] == "working"
    assert store.load_task(tid)["round"] == 2
    assert build_status["checkpoint_history"] == [{"round": 1, "sha": c1}]
    assert build_status["checkpoint_sha"] is None
    assert build_status["chain_checked"] is False
    coordinator = store.read_status(tid)  # tid == pipeline_id（根任务）
    assert coordinator["fix_count"] == 1
    assert any("有个边界条件" in text for _, text in fakes.send_keys_calls)
    assert fakes.close_calls == []  # 没新开窗口，不该关任何窗口
    assert store.read_status(review_id)["state"] == "chained"

    # 第 2 轮返工完成
    (wt / "canary.txt").write_text("build round1\nbuild round2\n", encoding="utf-8")
    _go_idle(tid, window_id="@1")
    _write_handover(tid, "改好了。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    build_status2 = store.read_status(tid)
    assert build_status2["state"] == "held"
    c2 = build_status2["checkpoint_sha"]
    assert c2 and c2 != c1
    review2_id = build_status2["successor_id"]
    review2_task = store.load_task(review2_id)
    assert review2_task["round"] == 2
    assert "有个边界条件没处理" in review2_task["prompt_final"]  # 上一轮意见传给了这一轮 review

    # 第 2 轮审稿 done → manual 收工 awaiting_merge，build 会话被敲停
    _go_idle(review2_id, window_id="@3")
    review2_file = store.task_dir(review2_id) / "review-2.md"
    review2_file.write_text("都改好了，测试也过。\n\nNEXT: done", encoding="utf-8")
    store.update_status(review2_id, review_verdict="done", review_file=str(review2_file),
                        review_recorded_round=2)
    scheduler.tick(cfg, NOW)
    assert store.read_status(review2_id)["state"] == "awaiting_merge"
    assert store.read_status(tid)["state"] == "chained"
    assert any("done" in text.lower() or "审稿" in text for _, text in fakes.send_keys_calls)


def test_review_pipeline_fix_opens_new_build_when_held_window_gone(monkeypatch):
    """held 会话窗口已经不在了：fix 造新的返工班（新 task id），不是捎话。"""
    fakes = Fakes(monkeypatch)
    # 只让 build 的窗口 @1 消失；review 自己的窗口 @2 仍然活着（否则它自己
    # 的 idle 处理都进不去，会被通用 window_gone 分支抢答成 exited）
    import nightshift.launcher as launcher_mod
    monkeypatch.setattr(
        launcher_mod, "window_alive",
        lambda wid, config: str(wid) != "@1",
    )
    tid = make_review_task(worktree=True)
    store.update_status(tid, worktree_path="/tmp/does-not-matter",
                        branch="ns/x", base_ref="deadbeef")
    _go_idle(tid, window_id="@1")
    review_id = store.create_task({
        "title": "审稿流水线任务", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": scheduler.to_iso(NOW), "task_text": "正文",
        "prompt_final": "提示词", "runner": "claude",
        "review": {"enabled": True, "runner": "claude", "model": "claude-fable-5", "effort": "high"},
    }, REVIEW_CONFIG)
    data = store.load_task(review_id)
    data.update({"role": "review", "round": 1, "role_shift": 1,
                 "parent_id": tid, "pipeline_id": tid, "shift": 2})
    store.atomic_write_json(store.task_dir(review_id) / "task.json", data)
    store.update_status(tid, state="held", successor_id=review_id)
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("退回重做。\n\nNEXT: fix", encoding="utf-8")
    _go_idle(review_id, window_id="@2")
    store.update_status(review_id, review_verdict="fix", review_file=str(review_file),
                        review_recorded_round=1)

    actions = scheduler.tick(REVIEW_CONFIG, NOW)
    review_status = store.read_status(review_id)
    assert review_status["state"] == "chained"
    build2_id = review_status["successor_id"]
    assert build2_id != tid
    build2 = store.load_task(build2_id)
    assert build2["role"] == "build" and build2["round"] == 2
    assert build2["parent_id"] == review_id
    assert "退回重做" in build2["prompt_final"]
    assert any(build2_id in a for a in actions)


def test_review_done_stop_build_failure_blocks_finalize(tmp_path, monkeypatch):
    """S7.2 阻断七：审稿通过（NEXT: done）但叫停仍在跑的 build 失败
    （send-keys 返回非零）时，以前 build 虽然被标 needs_attention，但函数
    末尾仍无条件调用 `_finalize_done`——auto 策略会继续 merge/清树，跟那扇
    "可能还在跑"的施工窗口打架。改法：停工失败时 review 自己也转
    needs_attention，`_finalize_done` 一次都不该被调用（不是看返回值猜，
    用 monkeypatch 替身直接断言调用次数为 0）。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    _write_handover(tid, "写完了。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review_id = store.read_status(tid)["successor_id"]

    finalize_calls: list[str] = []
    monkeypatch.setattr(
        scheduler, "_finalize_done",
        lambda task, *a, **k: finalize_calls.append(task["id"]) or [],
    )
    monkeypatch.setattr(
        launcher, "send_keys",
        lambda w, t: subprocess.CompletedProcess([], 1, "", "send-keys 失败"),
    )
    _go_idle(review_id, window_id="@2")
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("都过了。\n\nNEXT: done", encoding="utf-8")
    store.update_status(review_id, review_verdict="done", review_file=str(review_file),
                        review_recorded_round=1)
    scheduler.tick(cfg, NOW)

    assert finalize_calls == []  # 一次都没被调用，不是被调用后返回失败
    build_status = store.read_status(tid)
    assert build_status["state"] == "needs_attention"
    assert "停下" in build_status["error"]
    review_status = store.read_status(review_id)
    assert review_status["state"] == "needs_attention"
    assert "暂缓" in review_status["error"]
    # 两边各自开过一次提醒窗（build 一次 + review 一次）
    assert len(fakes.notice_calls) == 2


def test_review_pipeline_round_limit_needs_confirmation_then_continue(tmp_path, monkeypatch):
    """max_rounds=1：第一次 fix 必须允许恰好一次返工；第二次 fix 到线告警，
    "继续"（round_limit_override）再放一轮。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = dict(_review_config_for(proj))
    cfg["review"] = {**cfg["review"], "max_rounds": 1}
    tid = make_review_task(review={
        "enabled": True, "runner": "claude", "model": "claude-fable-5",
        "effort": "high", "max_rounds": 1,
    })
    wt = _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "第一轮。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review_id = store.read_status(tid)["successor_id"]

    _go_idle(review_id, window_id="@2")
    rf1 = store.task_dir(review_id) / "review-1.md"
    rf1.write_text("第一次退回。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(review_id, review_verdict="fix", review_file=str(rf1), review_recorded_round=1)
    scheduler.tick(cfg, NOW)  # 第一次返工：允许（首轮不占返工次数）
    assert store.read_status(tid)["state"] == "working"
    assert store.read_status(tid)["fix_count"] == 1

    (wt / "canary.txt").write_text("r1\nr2\n", encoding="utf-8")
    _go_idle(tid, window_id="@1")
    _write_handover(tid, "第二轮。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review2_id = store.read_status(tid)["successor_id"]
    _go_idle(review2_id, window_id="@3")
    rf2 = store.task_dir(review2_id) / "review-2.md"
    rf2.write_text("又要退回。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(review2_id, review_verdict="fix", review_file=str(rf2), review_recorded_round=2)

    scheduler.tick(cfg, NOW)  # 第二次 fix：fix_count(1) >= max_rounds(1) → 到线
    review2_status = store.read_status(review2_id)
    assert review2_status["state"] == "needs_attention"
    assert "到线" in review2_status["error"]
    assert store.read_status(tid)["fix_count"] == 1  # 没有偷偷再 +1
    assert len(fakes.notice_calls) >= 1

    # 再 tick：安静等，不重复告警/不自动返工
    notice_count_before = len(fakes.notice_calls)
    scheduler.tick(cfg, NOW)
    assert len(fakes.notice_calls) == notice_count_before
    assert store.read_status(review2_id)["state"] == "needs_attention"

    # "继续"：网页控制 API 会做的事——放行一次 + 把这一班拨回 idle 重新评估
    store.update_status(tid, round_limit_override=True)
    store.update_status(review2_id, state="idle")
    scheduler.tick(cfg, NOW)
    assert store.read_status(tid)["state"] == "working"
    assert store.read_status(tid)["fix_count"] == 2
    assert store.read_status(tid)["round_limit_override"] is False  # 消耗掉了，不能永久取消上限


def test_review_pipeline_hold_blocks_before_starting_review(tmp_path, monkeypatch):
    """"我来看"在起审稿前拦截：build 转 held 但不产生审稿班；理由带"工头"。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    wt = _register_tree(proj, tid, "审稿流水线任务")
    store.update_status(tid, hold_requested=True)  # 根任务就是 coordinator（tid 自己）
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")

    scheduler.tick(cfg, NOW)
    status = store.read_status(tid)
    assert status["state"] == "held"
    assert "工头" in status["held_reason"]
    assert "successor_id" not in status  # 没有起审稿班


def test_review_pipeline_hold_blocks_before_fix_and_merge(monkeypatch):
    """"我来看"在返工前/合并前同样拦截，且是幂等的（重复 tick 不重复告警）。"""
    fakes = Fakes(monkeypatch)
    tid = make_review_task(worktree=True)
    store.update_status(tid, worktree_path="/tmp/x", branch="ns/x", base_ref="deadbeef",
                        state="held")
    review_id = store.create_task({
        "title": "审稿流水线任务", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": scheduler.to_iso(NOW), "task_text": "正文",
        "prompt_final": "提示词",
        "review": {"enabled": True, "runner": "claude", "model": "claude-fable-5", "effort": "high"},
    }, REVIEW_CONFIG)
    data = store.load_task(review_id)
    data.update({"role": "review", "round": 1, "role_shift": 1, "parent_id": tid,
                 "pipeline_id": tid, "shift": 2})
    store.atomic_write_json(store.task_dir(review_id) / "task.json", data)
    store.update_status(tid, successor_id=review_id)
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("都过了。\n\nNEXT: done", encoding="utf-8")
    _go_idle(review_id, window_id="@2")
    store.update_status(review_id, review_verdict="done", review_file=str(review_file),
                        review_recorded_round=1)
    store.update_status(tid, hold_requested=True)

    scheduler.tick(REVIEW_CONFIG, NOW)
    status = store.read_status(review_id)
    assert status["state"] == "held"
    assert status.get("review_routed_round") != 1  # 没有真的按 done 分流下去


def test_review_pipeline_pending_hold_and_release(tmp_path, monkeypatch):
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)

    cfg_hold = dict(_review_config_for(proj))
    cfg_hold["review"] = {**cfg_hold["review"], "on_no_quota": "hold"}
    tid = make_review_task(review={
        "enabled": True, "runner": "claude", "model": "claude-fable-5",
        "effort": "high", "on_no_quota": "hold",
    })
    wt = _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")
    scheduler.tick(cfg_hold, NOW)
    review_id = store.read_status(tid)["successor_id"]
    _go_idle(review_id, window_id="@2")
    rf = store.task_dir(review_id) / "review-1.md"
    rf.write_text("额度到线，没看完。\n\nNEXT: pending", encoding="utf-8")
    store.update_status(review_id, review_verdict="pending", review_file=str(rf),
                        review_recorded_round=1)
    scheduler.tick(cfg_hold, NOW)
    assert store.read_status(review_id)["state"] == "held"

    # release（默认）：另起同轮审稿
    (tmp_path / "release").mkdir()
    proj2 = _make_repo(tmp_path / "release")
    cfg_release = _review_config_for(proj2)
    tid2 = make_review_task(title="release流水线", review={
        "enabled": True, "runner": "claude", "model": "claude-fable-5",
        "effort": "high", "on_no_quota": "release",
    })
    wt2 = _register_tree(proj2, tid2, "release流水线")
    _go_idle(tid2, window_id="@11")
    (wt2 / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid2, "写完了。\nNEXT: done")
    scheduler.tick(cfg_release, NOW)
    review2_id = store.read_status(tid2)["successor_id"]
    _go_idle(review2_id, window_id="@12")
    rf2 = store.task_dir(review2_id) / "review-1.md"
    rf2.write_text("额度到线，没看完。\n\nNEXT: pending", encoding="utf-8")
    store.update_status(review2_id, review_verdict="pending", review_file=str(rf2),
                        review_recorded_round=1)
    scheduler.tick(cfg_release, NOW)
    review2_status = store.read_status(review2_id)
    assert review2_status["state"] == "chained"
    new_review_id = review2_status["successor_id"]
    new_review = store.load_task(new_review_id)
    assert new_review["role"] == "review" and new_review["round"] == 1
    assert new_review["role_shift"] == 1  # 全新会话，不是同角色续班


def test_review_pipeline_pending_hold_auto_resumes_after_quota_refresh(tmp_path, monkeypatch):
    """S7.1 阻断二/三：pending + hold 之前没有对应的"额度刷新后自动恢复"
    动作，必然永久 held。落 quota_paused_until 之后，到点要能被
    _check_running 新增的 review-hold 恢复分支敲醒——文案是 review 语气、
    review_awaiting_verdict 被置回 True，之后真正的 done 仍能被正常记录
    （不会被当成控制 turn 吞掉）。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg_hold = dict(_review_config_for(proj))
    cfg_hold["review"] = {**cfg_hold["review"], "on_no_quota": "hold"}
    tid = make_review_task(review={
        "enabled": True, "runner": "claude", "model": "claude-fable-5",
        "effort": "high", "on_no_quota": "hold",
    })
    wt = _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")
    scheduler.tick(cfg_hold, NOW)
    review_id = store.read_status(tid)["successor_id"]
    _go_idle(review_id, window_id="@2")
    rf = store.task_dir(review_id) / "review-1.md"
    rf.write_text("额度到线，没看完。\n\nNEXT: pending", encoding="utf-8")
    store.update_status(review_id, review_verdict="pending", review_file=str(rf),
                        review_recorded_round=1)
    scheduler.tick(cfg_hold, NOW)
    review_status = store.read_status(review_id)
    assert review_status["state"] == "held"
    paused_until = review_status.get("quota_paused_until")
    assert paused_until  # 阻断二/三的核心：以前这里是 None，永远等不到恢复

    # 还没到刷新时间：不该敲
    scheduler.tick(cfg_hold, NOW)
    assert fakes.send_keys_calls == []

    # build（tid）仍 held 着等审稿结果，跟这条 review-hold 恢复路径无关；
    # 暂停它的保活，不然时间跳到 later 时它自己的保活戳会混进 send_keys_calls
    store.update_status(tid, keepalive_paused=True)

    later = scheduler.parse_iso(paused_until) + timedelta(minutes=1)
    scheduler.tick(cfg_hold, later)
    assert len(fakes.send_keys_calls) == 1
    window_id, text = fakes.send_keys_calls[0]
    assert window_id == "@2"
    assert "继续完成这一轮审稿" in text
    assert "NEXT" in text
    review_status = store.read_status(review_id)
    assert review_status["quota_resume_sent"] is True
    assert review_status["quota_paused_until"] is None
    assert review_status["review_awaiting_verdict"] is True  # 重新要求真 verdict
    assert review_status["state"] == "working"

    # 再 tick 不重复敲
    scheduler.tick(cfg_hold, later)
    assert len(fakes.send_keys_calls) == 1

    # 恢复之后真正的 done 仍能被 hook 正常记录——不是被当成控制 turn 吞掉
    # （review_awaiting_verdict 已经被恢复分支置回 True）。
    from nightshift import hook

    hook.handle_event(
        review_id, "Stop",
        {"last_assistant_message": "接着看完了，都对。\n\nNEXT: done"},
    )
    final_status = store.read_status(review_id)
    assert final_status["review_verdict"] == "done"
    assert final_status["review_recorded_round"] == 1


def test_apply_review_no_quota_policy_release_closes_build_window(monkeypatch):
    """审稿方额度不足、预检直接拦下起跑（还没起窗口）：release 关闭 held
    着的 build 窗口；hold 什么都不做。"""
    import nightshift.scheduler as sched
    fakes = Fakes(monkeypatch)
    bad_usage = dict(fakes.usage)
    bad_usage["session_pct"] = 99
    fakes.usage = bad_usage

    tid = make_review_task(review={
        "enabled": True, "runner": "claude", "model": "claude-fable-5",
        "effort": "high", "on_no_quota": "release",
    }, guards={"session_pct_max": 10, "weekly_pct_max": 95})
    store.update_status(tid, state="held", window_id="@1", pane_pid=1)
    review_id = store.create_task({
        "title": "审稿流水线任务", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": scheduler.to_iso(NOW), "task_text": "正文",
        "prompt_final": "提示词", "guards": {"session_pct_max": 10, "weekly_pct_max": 95},
        "review": {"enabled": True, "runner": "claude", "model": "claude-fable-5", "effort": "high",
                   "on_no_quota": "release"},
    }, REVIEW_CONFIG)
    data = store.load_task(review_id)
    data.update({"role": "review", "round": 1, "role_shift": 1, "parent_id": tid,
                 "pipeline_id": tid, "shift": 2})
    store.atomic_write_json(store.task_dir(review_id) / "task.json", data)
    store.update_status(tid, successor_id=review_id)

    sched.tick(REVIEW_CONFIG, NOW)
    assert fakes.close_calls == [["@1"]]
    assert store.read_status(tid)["review_no_quota_released"] is True
    assert store.read_status(review_id)["state"] == "postponed"


REVIEW_CODEX_CONFIG = {**REVIEW_CONFIG, "runners": CODEX_CONFIG["runners"]}


def test_review_pipeline_mixed_cc_build_codex_review(tmp_path, monkeypatch):
    """施工=Claude、审稿=Codex：审稿班的有效工人/额度来源正确按 review
    自己的 runner 走，不被顶层 build runner 污染。"""
    fakes = Fakes(monkeypatch)
    monkeypatch.setattr(quota, "fetch_usage_codex", lambda config, timeout=15.0: dict(fakes.usage))
    proj = _make_repo(tmp_path)
    cfg = dict(REVIEW_CODEX_CONFIG)
    cfg["projects"] = {"demo": str(proj), "other": str(proj / "nope")}
    tid = store.create_task({
        "title": "混合流水线", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": scheduler.to_iso(NOW), "task_text": "正文",
        "prompt_final": "提示词",
        "review": {"enabled": True, "runner": "codex", "model": "gpt-5.6-luna", "effort": "high"},
    }, cfg)
    wt = _register_tree(proj, tid, "混合流水线")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")

    scheduler.tick(cfg, NOW)
    build_status = store.read_status(tid)
    assert build_status["state"] == "held"
    review_id = build_status["successor_id"]
    review_task = store.load_task(review_id)
    assert review_task["role"] == "review"
    assert review_task["runner"] == "claude"  # 顶层仍是这条流水线的建造配方
    assert store.effective_runner(review_task) == "codex"  # 但这一班真正用 codex
    assert store.effective_model(review_task) == "gpt-5.6-luna"
    # create_cross_role_successor 的 run_at 是真实墙钟时间，跟测试的固定
    # NOW 无关；这里手动拨回，只为了让下一次 tick 判定"到点"
    review_task["run_at"] = scheduler.to_iso(NOW)
    store.atomic_write_json(store.task_dir(review_id) / "task.json", review_task)

    scheduler.tick(cfg, NOW)  # 审稿班起跑：quota 走 codex 分支
    review_status = store.read_status(review_id)
    assert review_status["state"] == "launching"
    assert review_status["quota_at_launch"]["quota_source"] == "codex"
    quota_file = json.loads((store.home() / "quota.json").read_text(encoding="utf-8"))
    assert quota_file["codex"]["usage"]  # 查过 codex
    assert fakes.launch_calls == [review_id]  # build 是 held，没有被再次 launch

    # 审稿给出 fix：下一轮 build 仍是 Claude（build 顶层配方不受 review runner 影响）
    _go_idle(review_id, window_id="@2")
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("退回。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(review_id, review_verdict="fix", review_file=str(review_file),
                        review_recorded_round=1)
    scheduler.tick(cfg, NOW)
    build_status2 = store.read_status(tid)
    assert build_status2["state"] == "working"
    assert store.load_task(tid)["round"] == 2
    assert store.effective_runner(store.load_task(tid)) == "claude"


def test_review_pipeline_codex_build_claude_review_full_cycle(tmp_path, monkeypatch):
    """S7.2 兼容尾巴 3：验收单曾把
    `test_tick_refreshes_both_runners_when_codex_build_held_claude_review_working`
    当成"Codex build → Claude review 组合的端到端覆盖"，但那条测试造的是两个
    完全不相干的独立任务（没有共同 pipeline_id/parent_id），只测了 tick()
    额度刷新的收集逻辑，从没真正走过一条 Codex 施工→Claude 审稿的流水线。
    这里补一条真正的端到端：起跑→build 收工→Claude 审稿→fix 原地捎话返工
    （Codex build 复用，shift 单调领号）→第二轮 done→manual 收工
    awaiting_merge，全程 build 顶层/effective runner 恒为 codex、review 的
    effective runner 恒为 claude，两家额度分片各自独立被查。"""
    fakes = Fakes(monkeypatch)
    monkeypatch.setattr(quota, "fetch_usage_codex", lambda config, timeout=15.0: dict(fakes.usage))
    proj = _make_repo(tmp_path)
    cfg = dict(REVIEW_CODEX_CONFIG)
    cfg["projects"] = {"demo": str(proj), "other": str(proj / "nope")}
    tid = store.create_task({
        "title": "codex 施工 claude 审稿", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "run_at": scheduler.to_iso(NOW),
        "task_text": "正文", "prompt_final": "提示词",
        "review": {"enabled": True, "runner": "claude", "model": "claude-fable-5", "effort": "high"},
    }, cfg)
    build1_shift = store.load_task(tid)["shift"]
    wt = _register_tree(proj, tid, "codex 施工 claude 审稿")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")

    scheduler.tick(cfg, NOW)
    build_status = store.read_status(tid)
    assert build_status["state"] == "held"
    review_id = build_status["successor_id"]
    review_task = store.load_task(review_id)
    assert review_task["role"] == "review"
    assert review_task["runner"] == "codex"  # 顶层仍是这条流水线的建造配方
    assert store.effective_runner(review_task) == "claude"  # 但这一班真正用 claude
    assert store.load_task(review_id)["shift"] > build1_shift  # 单调

    review_task["run_at"] = scheduler.to_iso(NOW)
    store.atomic_write_json(store.task_dir(review_id) / "task.json", review_task)
    scheduler.tick(cfg, NOW)
    review_status = store.read_status(review_id)
    assert review_status["state"] == "launching"
    assert review_status["quota_at_launch"]["quota_source"] == "claude"
    assert fakes.launch_calls == [review_id]  # build 仍 held，同 pipeline 互斥没有拦住对侧起跑

    # 审稿退回：build（codex）仍 held → 原地捎话返工
    _go_idle(review_id, window_id="@2")
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("退回。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(review_id, review_verdict="fix", review_file=str(review_file),
                        review_recorded_round=1)
    scheduler.tick(cfg, NOW)
    build_status2 = store.read_status(tid)
    assert build_status2["state"] == "working"
    build_task2 = store.load_task(tid)
    assert build_task2["round"] == 2
    assert store.effective_runner(build_task2) == "codex"
    assert build_task2["shift"] > store.load_task(review_id)["shift"]

    # 第二轮通过：manual 收工 awaiting_merge
    (wt / "canary.txt").write_text("r1\nr2\n", encoding="utf-8")
    _go_idle(tid, window_id="@1")
    _write_handover(tid, "第二轮写完了。\nNEXT: done", shift=build_task2["shift"])
    scheduler.tick(cfg, NOW)
    review2_id = store.read_status(tid)["successor_id"]
    review2_task = store.load_task(review2_id)
    review2_task["run_at"] = scheduler.to_iso(NOW)
    store.atomic_write_json(store.task_dir(review2_id) / "task.json", review2_task)
    scheduler.tick(cfg, NOW)

    _go_idle(review2_id, window_id="@3")
    review_file2 = store.task_dir(review2_id) / "review-2.md"
    review_file2.write_text("都过了。\n\nNEXT: done", encoding="utf-8")
    store.update_status(review2_id, review_verdict="done", review_file=str(review_file2),
                        review_recorded_round=2)
    scheduler.tick(cfg, NOW)
    assert store.read_status(review2_id)["state"] == "awaiting_merge"
    assert store.read_status(tid)["state"] == "chained"


def test_review_pipeline_codex_build_codex_review_full_cycle(tmp_path, monkeypatch):
    """S7.1 阻断四 Part B/两个 Codex 组合端到端：施工=审稿都是 Codex——
    以前只测过"一家 Codex + 一家 Claude"的混合组合，纯 Codex×Codex 这条
    (阻断四同 pipeline 互斥 + effective_runner 权威源 + shift 单调) 没有
    任何覆盖。走一轮完整：build 收工起同轮 review（额度走 codex 分片）→
    review fix 原地捎话返工（① 的 held 复用 + shift 单调）→ 第二轮 done →
    manual 收工 awaiting_merge，全程两班顶层 runner 与 effective_runner
    都应该是 codex，没有一次误查 claude 分片。"""
    fakes = Fakes(monkeypatch)
    codex_calls: list[int] = []
    monkeypatch.setattr(
        quota, "fetch_usage_codex",
        lambda config, timeout=15.0: codex_calls.append(1) or dict(fakes.usage),
    )

    def _no_claude_fetch(config, timeout=120):
        raise AssertionError("纯 Codex×Codex 流水线不该查 claude 额度分片")

    monkeypatch.setattr(quota, "fetch_usage_claude", _no_claude_fetch)
    proj = _make_repo(tmp_path)
    cfg = dict(REVIEW_CODEX_CONFIG)
    cfg["projects"] = {"demo": str(proj), "other": str(proj / "nope")}
    tid = store.create_task({
        "title": "codex 流水线", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "run_at": scheduler.to_iso(NOW),
        "task_text": "正文", "prompt_final": "提示词",
        "review": {"enabled": True, "runner": "codex", "model": "gpt-5.6-luna", "effort": "high"},
    }, cfg)
    build1_shift = store.load_task(tid)["shift"]
    wt = _register_tree(proj, tid, "codex 流水线")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")

    scheduler.tick(cfg, NOW)
    build_status = store.read_status(tid)
    assert build_status["state"] == "held"
    review_id = build_status["successor_id"]
    review_task = store.load_task(review_id)
    assert review_task["role"] == "review"
    assert store.effective_runner(review_task) == "codex"
    assert store.load_task(review_id)["shift"] > build1_shift  # 单调

    review_task["run_at"] = scheduler.to_iso(NOW)  # 真实墙钟 → 拨回可被判到点
    store.atomic_write_json(store.task_dir(review_id) / "task.json", review_task)
    scheduler.tick(cfg, NOW)
    review_status = store.read_status(review_id)
    assert review_status["state"] == "launching"
    assert review_status["quota_at_launch"]["quota_source"] == "codex"
    assert len(codex_calls) >= 1
    assert fakes.launch_calls == [review_id]  # build 仍 held，同 pipeline 互斥没有拦住对侧起跑

    # 审稿退回：build 仍 held → 原地捎话返工（① 两阶段提交 + shift 领号）
    _go_idle(review_id, window_id="@2")
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("退回。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(review_id, review_verdict="fix", review_file=str(review_file),
                        review_recorded_round=1)
    scheduler.tick(cfg, NOW)
    build_status2 = store.read_status(tid)
    assert build_status2["state"] == "working"
    build_task2 = store.load_task(tid)
    assert build_task2["round"] == 2
    assert store.effective_runner(build_task2) == "codex"
    assert build_task2["shift"] > store.load_task(review_id)["shift"]  # 复用也领新号

    # 第二轮通过：manual 收工 awaiting_merge
    (wt / "canary.txt").write_text("r1\nr2\n", encoding="utf-8")
    _go_idle(tid, window_id="@1")
    _write_handover(tid, "第二轮写完了。\nNEXT: done", shift=build_task2["shift"])
    scheduler.tick(cfg, NOW)
    review2_id = store.read_status(tid)["successor_id"]
    review2_task = store.load_task(review2_id)
    review2_task["run_at"] = scheduler.to_iso(NOW)
    store.atomic_write_json(store.task_dir(review2_id) / "task.json", review2_task)
    scheduler.tick(cfg, NOW)

    _go_idle(review2_id, window_id="@3")
    review_file2 = store.task_dir(review2_id) / "review-2.md"
    review_file2.write_text("都过了。\n\nNEXT: done", encoding="utf-8")
    store.update_status(review2_id, review_verdict="done", review_file=str(review_file2),
                        review_recorded_round=2)
    scheduler.tick(cfg, NOW)
    assert store.read_status(review2_id)["state"] == "awaiting_merge"
    assert store.read_status(tid)["state"] == "chained"  # build 被正常敲停、标 chained（不是 needs_attention）


def test_held_keepalive_paused_skips_and_interval_by_runner(tmp_path, monkeypatch):
    """held 状态也走保活；keepalive_paused 时不戳；按 runner 的间隔（claude
    50 分钟）判断是否到点，不是一律戳。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    _register_tree(proj, tid, "审稿流水线任务")
    store.update_status(
        tid, state="held", window_id="@1", pane_pid=1,
        last_event_at=scheduler.to_iso(NOW - timedelta(minutes=10)),
        keepalive_paused=True,
    )
    scheduler.tick(cfg, NOW)
    assert fakes.send_keys_calls == []  # 暂停中，不该戳

    store.update_status(tid, keepalive_paused=False,
                        last_event_at=scheduler.to_iso(NOW - timedelta(minutes=10)))
    scheduler.tick(cfg, NOW)
    assert fakes.send_keys_calls == []  # 恢复了，但还没到 50 分钟

    store.update_status(tid, last_event_at=scheduler.to_iso(NOW - timedelta(minutes=51)))
    scheduler.tick(cfg, NOW)
    assert len(fakes.send_keys_calls) == 1  # 到点了，戳一次
    assert store.read_status(tid)["last_keepalive_at"]


def test_review_fix_send_keys_failure_is_fail_closed(tmp_path, monkeypatch):
    """held build 会话还活着，但把返工意见敲进去失败：不能假装已经继续了，
    必须 needs_attention 留痕，不能悄悄丢掉这条返工意见。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    wt = _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review_id = store.read_status(tid)["successor_id"]

    # S7.1 阻断一反例：记下 send-keys 之前的完整状态，失败后要能逐字段
    # 对比"调用前后完全一致，可以安全重试"——旧 bug 是先改 fix_count/
    # build.round 再 send-keys，失败后 round 已经变了、fix_count 已经加了、
    # checkpoint 字段却还停在上一轮，成了没法安全重试的半吊子状态。
    before_task = store.load_task(tid)
    before_status = store.read_status(tid)
    before_coordinator = store.read_status(tid)  # tid 本身就是这条流水线的 coordinator

    monkeypatch.setattr(
        launcher, "send_keys",
        lambda w, t: subprocess.CompletedProcess([], 1, "", "send-keys 失败"),
    )
    _go_idle(review_id, window_id="@2")
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("退回。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(review_id, review_verdict="fix", review_file=str(review_file),
                        review_recorded_round=1)
    scheduler.tick(cfg, NOW)
    review_status = store.read_status(review_id)
    assert review_status["state"] == "needs_attention"
    assert "失败" in review_status["error"]
    assert len(fakes.notice_calls) == 1
    # build 那边没被悄悄标成继续
    after_task = store.load_task(tid)
    after_status = store.read_status(tid)
    assert after_status["state"] == "held"
    assert after_task["round"] == before_task["round"]
    assert after_task["shift"] == before_task["shift"]
    assert after_status.get("checkpoint_sha") == before_status.get("checkpoint_sha")
    assert after_status.get("checkpoint_done") == before_status.get("checkpoint_done")
    assert after_status.get("checkpoint_history") == before_status.get("checkpoint_history")
    assert after_status.get("chain_checked") == before_status.get("chain_checked")
    assert after_status.get("fix_count") == before_coordinator.get("fix_count")  # 没有真的计数
    assert after_status.get("pending_fix_intent") is None  # 失败后意图标记已清干净


def test_review_fix_reuse_success_advances_shift_no_cycle(tmp_path, monkeypatch):
    """S7.1 阻断一反例：held build 原地复用两轮返工后——
    - shift 全局单调递增、不撞号（旧 bug：复用只改 round 不改 shift，
      两轮返工后两个 review 会撞到同一个 shift）；
    - chain_state() 对 build/两个 review 三方都能扫到真正最新一班的状态
      （旧 bug：max-shift 撞号时 chain_state 猜错，"当前班"卡在旧状态）；
    - review 复用 build 时不再写成环的 successor_id（旧 bug：
      review.successor_id 被设回它复用的 build id，跟 build 早先指向它的
      successor_id 形成两步环），改记只读的 reactivated_task_id /
      reactivated_from_review_id，且沿这条流水线所有 successor_id 边走
      不出现环。
    """
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    wt = _register_tree(proj, tid, "审稿流水线任务")
    build1_shift = store.load_task(tid)["shift"]
    assert build1_shift == 1

    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "第一轮。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review1_id = store.read_status(tid)["successor_id"]
    review1_shift = store.load_task(review1_id)["shift"]
    assert review1_shift > build1_shift

    _go_idle(review1_id, window_id="@2")
    rf1 = store.task_dir(review1_id) / "review-1.md"
    rf1.write_text("退回。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(review1_id, review_verdict="fix", review_file=str(rf1),
                        review_recorded_round=1)
    scheduler.tick(cfg, NOW)

    build_reused_shift = store.load_task(tid)["shift"]
    assert build_reused_shift > review1_shift  # 原地复用也领了新号，不是停在旧值
    review1_status = store.read_status(review1_id)
    assert review1_status["state"] == "chained"
    assert review1_status.get("reactivated_task_id") == tid
    assert review1_status.get("successor_id") is None  # 不再写成环的 successor_id
    build_status_after_reuse = store.read_status(tid)
    assert build_status_after_reuse.get("reactivated_from_review_id") == review1_id

    # 第二轮返工完成，起真正的第二个 review task
    (wt / "canary.txt").write_text("r1\nr2\n", encoding="utf-8")
    _go_idle(tid, window_id="@1")
    _write_handover(tid, "第二轮。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review2_id = store.read_status(tid)["successor_id"]
    review2_shift = store.load_task(review2_id)["shift"]
    assert review2_shift > build_reused_shift

    shifts = [build1_shift, review1_shift, build_reused_shift, review2_shift]
    assert len(set(shifts)) == 4 and shifts == sorted(shifts)  # 各不相同且单调递增

    review2_state = store.read_status(review2_id)["state"]
    assert review2_state == "scheduled"
    assert store.chain_state(tid) == review2_state
    assert store.chain_state(review1_id) == review2_state
    assert store.chain_state(review2_id) == review2_state

    # successor_id 是唯一该被当"流水线走向"遍历的字段（reactivated_task_id /
    # reactivated_from_review_id 是只读的历史标记，故意不参与遍历）——对
    # 这条流水线里所有任务的 successor_id 边做环检测，一步都不能兜回来。
    pipeline_tasks = [
        item["task"]["id"] for item in store.list_tasks()
        if store.pipeline_id_of(item["task"]) == tid
    ]
    assert set(pipeline_tasks) == {tid, review1_id, review2_id}
    edges = {t: store.read_status(t).get("successor_id") for t in pipeline_tasks}
    for start in pipeline_tasks:
        seen: set[str] = set()
        node = start
        while node:
            assert node not in seen, f"successor_id 链从 {start} 出发成环：{seen}"
            seen.add(node)
            node = edges.get(node)


def test_review_fix_reuse_clears_stale_round_bookkeeping_so_old_handover_is_ignored(
    tmp_path, monkeypatch
):
    """S7.2 阻断三反例：held build 原地复用成功推进 shift/round，但如果不清
    掉上一轮的运行期收尾标记（handover_path/context_warned_at/…），新一轮
    只要发生一次普通 Stop、还没来得及写新交接文件，调度器就会重新读到
    status.handover_path 指向的上一轮旧文件（写着 NEXT:done），把中间停顿
    误判成这一轮已经收工。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    wt = _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "第一轮。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review1_id = store.read_status(tid)["successor_id"]

    # 模拟第一轮 build 在收工前曾经被上下文/额度提醒过一次——status 上会
    # 留下 handover_path（指向 round1 那份写着 NEXT:done 的旧交接文件）与
    # 一堆"这一轮已经被提醒过什么"的标记。
    old_handover = store.task_dir(tid) / "handover-1.md"
    assert old_handover.is_file()
    old_handover_text = old_handover.read_text(encoding="utf-8")
    assert "NEXT: done" in old_handover_text
    store.update_status(
        tid, handover_path=str(old_handover), context_warned_at="2026-08-30T10:00:00Z",
        quota_warned_at="2026-08-30T10:00:00Z", context_warn_count=2, quota_warn_count=1,
        mode_warned=True, other_model_warned=["opus"],
    )

    _go_idle(review1_id, window_id="@2")
    rf1 = store.task_dir(review1_id) / "review-1.md"
    rf1.write_text("退回。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(
        review1_id, review_verdict="fix", review_file=str(rf1), review_recorded_round=1
    )
    scheduler.tick(cfg, NOW)  # 原地复用成功：build 转 working，进入第 2 轮

    build_status = store.read_status(tid)
    assert build_status["state"] == "working"
    assert build_status["round"] == 2
    # 上一轮的运行期收尾标记必须被显式清空，不能因为合并语义悄悄留着。
    assert build_status.get("handover_path") is None
    assert build_status.get("context_warned_at") is None
    assert build_status.get("quota_warned_at") is None
    assert build_status.get("context_warn_count") == 0
    assert build_status.get("quota_warn_count") == 0
    assert build_status.get("mode_warned") is False
    assert build_status.get("other_model_warned") == []

    # 新一轮还没写任何新交接文件（handover-<新shift>.md 不存在）；
    # _handover_file() 清掉 handover_path 后退回按当前 shift 计算的默认
    # 路径，_read_handover 读不到文件应该返回 None——不会误读 round1 那份
    # 写着 NEXT:done 的旧交接（旧 bug 恰恰是这里：status.handover_path 一直
    # 指着 round1 的文件，_read_handover 读到的是真实存在的 "NEXT: done"
    # 文本，被当成"这一轮也已经收工"直接触发 _finalize_done）。
    from nightshift import scheduler as sched_mod

    task = store.load_task(tid)
    status = store.read_status(tid)
    hpath = sched_mod._handover_file(task, status)
    assert not hpath.is_file()
    assert sched_mod._read_handover(hpath) is None

    # 更明确的行为反例：模拟第 2 轮自己也被提醒过一次（新落的
    # context_warned_at），但还没来得及写新交接就去 idle——正确行为是走
    # "被提醒过没交接 → chain.on_no_handover=continue"续班分支（state 变
    # chained，交接兜底文案），而不是（旧 bug 会发生的）把 status 上残留的
    # round1 旧文件当成本轮真交接、直接 NEXT:done 触发 _finalize_done 起
    # 第 2 轮审稿——那样会把"第 2 轮其实还没做完"悄悄当成"第 2 轮已经审过了"。
    store.update_status(tid, context_warned_at=scheduler.to_iso(NOW))
    _go_idle(tid, window_id="@1")
    scheduler.tick(cfg, NOW)
    after = store.read_status(tid)
    assert after["state"] == "chained"  # 走 on_no_handover=continue，不是误判收工
    events = (store.task_dir(tid) / "events.log").read_text(encoding="utf-8")
    assert "on_no_handover=continue" in events


def _setup_fix_intent_pipeline(tmp_path, monkeypatch):
    """给 5 个崩溃恢复反例共用的基础状态：held build（round=1）+ review 已
    经拿到 NEXT:fix verdict、还没被 _review_fix 处理（review_routed_round
    未设）。返回 (fakes, cfg, tid, review_id)。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    _write_handover(tid, "第一轮。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review_id = store.read_status(tid)["successor_id"]
    _go_idle(review_id, window_id="@2")
    rf = store.task_dir(review_id) / "review-1.md"
    rf.write_text("退回。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(
        review_id, review_verdict="fix", review_verdict_final=True,
        review_file=str(rf), review_recorded_round=1,
    )
    return fakes, cfg, tid, review_id


def _assert_fix_intent_reconciled_safely(fakes, cfg, tid, review_id, before_send_keys_count):
    """5 个切点共用的断言：这一 tick 不重发/不新起返工，build 与 review
    两边**都**要显示告警（不是只有先被 `_check_running` 巡检到的那一个），
    且第二次 tick 不重复告警（提醒窗口只开一次，但两边的 state 各自持续
    保持 needs_attention）。

    协调者自查补充：早期版本这里用的是 or 不是 and——只要求"build 或
    review 有一个转 needs_attention 就行"，掩盖了"提醒窗口只开一次"跟
    "把这个任务自己标成 needs_attention"没有分开处理导致的真实缺口：先被
    处理到的那个任务被标了，另一个因为 `pending_fix_intent_noted` 已经
    是 True 就直接安静跳过、自己的 state 从头到尾没被设过
    needs_attention，操作者盯着这一个任务的卡片完全看不出异常。"""
    review_before = dict(store.read_status(review_id))
    build_before = dict(store.read_status(tid))
    scheduler.tick(cfg, NOW)
    assert len(fakes.send_keys_calls) == before_send_keys_count  # 没有任何新的投递
    notice_count_after_first = len(fakes.notice_calls)
    assert notice_count_after_first == 1  # 提醒窗口只开一次（不是两次）
    assert store.read_status(tid).get("state") == "needs_attention"  # build 也要显示告警
    assert store.read_status(review_id).get("state") == "needs_attention"  # review 也要
    # 再跑一轮：不重复开提醒窗、不重复重发。
    scheduler.tick(cfg, NOW)
    assert len(fakes.send_keys_calls) == before_send_keys_count
    assert len(fakes.notice_calls) == notice_count_after_first
    # 除了这次 reconcile 自己写的 state/error/needs_attention 相关字段，
    # 其余字段跟"死掉那一刻"完全一致，没有被瞎猜"修好"或悄悄推进。
    review_after = store.read_status(review_id)
    build_after = store.read_status(tid)
    for key in ("review_verdict", "review_recorded_round", "review_file", "successor_id"):
        assert review_after.get(key) == review_before.get(key), key
    for key in ("round", "shift", "checkpoint_sha", "checkpoint_history"):
        assert build_after.get(key) == build_before.get(key), key


def test_check_running_fails_closed_when_coordinator_task_missing(monkeypatch):
    """S7.2 兼容尾巴 2：task.json 的 pipeline_id 指向一个不存在的任务时
    （数据损坏/坏字段），`_check_running` 要在最开头就 fail-closed 到
    needs_attention，不能让后面任何 `_update_coordinator()` 调用（散布在
    审稿流水线各处）凭空建出只有 status.json 的幽灵 coordinator 目录。"""
    fakes = Fakes(monkeypatch)
    tid = make_task()
    task = store.load_task(tid)
    task["pipeline_id"] = "20260101-000000-dead"  # 从未存在过的 id
    store.atomic_write_json(store.task_dir(tid) / "task.json", task)
    store.update_status(tid, state="idle", window_id="@1", pane_pid=1,
                        last_event_at=scheduler.to_iso(NOW))

    scheduler.tick(CONFIG, NOW)
    status = store.read_status(tid)
    assert status["state"] == "needs_attention"
    assert "coordinator" in status["error"] or "不存在" in status["error"]
    assert not (store.home() / "tasks" / "20260101-000000-dead").is_dir()  # 没建出幽灵目录
    assert len(fakes.notice_calls) == 1

    scheduler.tick(CONFIG, NOW)  # 再跑一轮：不重复开提醒窗
    assert len(fakes.notice_calls) == 1
    assert not (store.home() / "tasks" / "20260101-000000-dead").is_dir()


def test_try_launch_blocks_second_task_when_pipeline_already_working(monkeypatch):
    """S7.2 兼容尾巴 4：这是 Sol 两轮审查都用来验证阻断四的直接反例——同
    一条 pipeline 已经有一个任务 `working`，另一个同 pipeline 的任务到点
    尝试起跑，必须被拦（postponed，不是被放行成第二个 launching）。以前
    S7.1③的验收单只自称"同 pipeline 互斥拦第二班"，但实际新增测试只覆盖
    了"held+对侧角色起跑"的放行路径，从没有一条测试真正构造过"已经
    working、再起第二班"这个最直接的场景。"""
    fakes = Fakes(monkeypatch)
    working_id = make_task(title="正在跑的第一班")
    store.update_status(
        working_id, state="working", window_id="@1", pane_pid=1,
        last_event_at=scheduler.to_iso(NOW),
    )

    second_id = make_task(title="同流水线第二班")
    second_task = store.load_task(second_id)
    second_task["pipeline_id"] = working_id  # 手动挂进同一条 pipeline
    second_task["run_at"] = scheduler.to_iso(NOW)  # 到点
    store.atomic_write_json(store.task_dir(second_id) / "task.json", second_task)

    scheduler.tick(CONFIG, NOW)

    assert second_id not in fakes.launch_calls  # 没有被放行成第二个 launching
    second_status = store.read_status(second_id)
    assert second_status["state"] == "postponed"
    events = (store.task_dir(second_id) / "events.log").read_text(encoding="utf-8")
    assert working_id in events and "正在跑" in events
    # 第一班本身没被这个检查动到
    assert store.read_status(working_id)["state"] == "working"


def test_pending_fix_intent_crash_recovery_checkpoint_1_and_2_intent_before_and_after_send(
    tmp_path, monkeypatch,
):
    """S7.2 阻断二切点①②：intent 落盘之后、send-keys 之前 / send-keys 成功
    之后、next_pipeline_shift 之前——这两个切点在磁盘状态上无法区分（send
    本身不产生任何落盘副作用），合并成一条反例：coordinator 上只有
    pending_fix_intent，build/review 的其余字段都停在"最开始"。"""
    fakes, cfg, tid, review_id = _setup_fix_intent_pipeline(tmp_path, monkeypatch)
    before_send_keys_count = len(fakes.send_keys_calls)
    store.update_status(
        tid, pending_fix_intent={"build_id": tid, "review_id": review_id, "next_round": 2},
    )
    _assert_fix_intent_reconciled_safely(fakes, cfg, tid, review_id, before_send_keys_count)


def test_pending_fix_intent_crash_recovery_checkpoint_3_shift_taken_before_task_json(
    tmp_path, monkeypatch,
):
    """切点③：领完 shift（pipeline_shift_seq 已经被 next_pipeline_shift 推
    进）之后、写 build task.json 之前——build 自己的 round/shift 字段还没变，
    但 coordinator 的领号序列已经往前走了一格。"""
    fakes, cfg, tid, review_id = _setup_fix_intent_pipeline(tmp_path, monkeypatch)
    before_send_keys_count = len(fakes.send_keys_calls)
    build_task = store.load_task(tid)
    store.update_status(
        tid, pending_fix_intent={"build_id": tid, "review_id": review_id, "next_round": 2},
        pipeline_shift_seq=int(build_task.get("shift") or 1) + 1,
    )
    _assert_fix_intent_reconciled_safely(fakes, cfg, tid, review_id, before_send_keys_count)


def test_pending_fix_intent_crash_recovery_checkpoint_4_task_json_written_before_status(
    tmp_path, monkeypatch,
):
    """切点④：build task.json 已经写成新一轮（round/shift 已推进），但
    build 自己的 status.json 还没跟着改——账面 state 仍是 held、round 停在
    旧值，跟 task.json 已经不一致。"""
    fakes, cfg, tid, review_id = _setup_fix_intent_pipeline(tmp_path, monkeypatch)
    before_send_keys_count = len(fakes.send_keys_calls)
    build_task = store.load_task(tid)
    new_shift = int(build_task.get("shift") or 1) + 1
    build_task["round"] = 2
    build_task["shift"] = new_shift
    store.atomic_write_json(store.task_dir(tid) / "task.json", build_task)
    store.update_status(
        tid, pending_fix_intent={"build_id": tid, "review_id": review_id, "next_round": 2},
        pipeline_shift_seq=new_shift,
    )
    _assert_fix_intent_reconciled_safely(fakes, cfg, tid, review_id, before_send_keys_count)


def test_pending_fix_intent_crash_recovery_checkpoint_5_build_status_written_before_review(
    tmp_path, monkeypatch,
):
    """切点⑤：build 自己的 status 已经改成 working/第 2 轮，但 review 侧
    （chained + reactivated_task_id）与 coordinator 的 fix_count 还没提交
    ——review 仍停在 idle/verdict=fix 未消费的状态，build 却已经在"working"
    这个活跃状态，两边对不上。"""
    fakes, cfg, tid, review_id = _setup_fix_intent_pipeline(tmp_path, monkeypatch)
    before_send_keys_count = len(fakes.send_keys_calls)
    build_task = store.load_task(tid)
    new_shift = int(build_task.get("shift") or 1) + 1
    build_task["round"] = 2
    build_task["shift"] = new_shift
    store.atomic_write_json(store.task_dir(tid) / "task.json", build_task)
    store.update_status(
        tid, pending_fix_intent={"build_id": tid, "review_id": review_id, "next_round": 2},
        pipeline_shift_seq=new_shift, state="working", round=2, shift=new_shift,
        chain_checked=False, checkpoint_done=False, checkpoint_sha=None,
        checkpoint_history=[], reactivated_from_review_id=review_id,
    )
    _assert_fix_intent_reconciled_safely(fakes, cfg, tid, review_id, before_send_keys_count)


def test_review_pipeline_pending_release_carries_partial_review_text(tmp_path, monkeypatch):
    """S7.1 阻断三反例：pending（release）另起同轮审稿时，如果这一轮已经
    写了半截意见（review_file 已落盘），续班的 review 提示词里要能看到这
    半截内容，不能像旧写法一样传空串把已经审过的部分白白丢掉。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    wt = _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review_id = store.read_status(tid)["successor_id"]

    _go_idle(review_id, window_id="@2")
    rf = store.task_dir(review_id) / "review-1.md"
    rf.write_text("已经看了一半，这部分没问题。\n\nNEXT: pending", encoding="utf-8")
    store.update_status(review_id, review_verdict="pending", review_file=str(rf),
                        review_recorded_round=1)
    scheduler.tick(cfg, NOW)
    new_review_id = store.read_status(review_id)["successor_id"]
    new_review = store.load_task(new_review_id)
    assert "已经看了一半，这部分没问题" in new_review["prompt_final"]


def test_review_prompts_pass_worktree_not_main_project_dir(tmp_path, monkeypatch):
    """S7.5 阻断回归锁：真机 smoke 抓到审稿提示词把 {project_path} 填成了
    config.projects 里的主签出目录（未修的旧代码），不是施工班实际干活的
    工作树——审稿会话的 cwd/信任根是工作树，指错目录会让审稿人读到旧代码、
    永远判 fix，五轮都合并不了（死循环）。

    这条锁盯两处调用点：`_start_review_round`（build 收工起第 1 轮审稿）
    与 `_review_pending` 的 release 分支（额度到线另起同轮审稿）——spy 记录
    每次 store.render_review_prompt 实际收到的 workdir 关键字参数，断言
    等于登记在 status 里的 worktree_path、且不等于 config 主目录。

    在打 S7.5 补丁之前，`render_review_prompt` 没有 `workdir` 形参，这个
    spy（透传 workdir 给真实实现）会在两次调用处直接 TypeError，证明这条
    测试确实锁住了旧代码的缺陷，不是摆设。"""
    fakes = Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    calls: list[str] = []
    real_render = store.render_review_prompt

    def spy(config, task, *, workdir, **kw):
        calls.append(workdir)
        return real_render(config, task, workdir=workdir, **kw)

    monkeypatch.setattr(store, "render_review_prompt", spy)

    tid = make_review_task()
    wt = _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")
    scheduler.tick(cfg, NOW)  # _start_review_round 起第 1 轮审稿
    assert len(calls) == 1
    assert calls[0] == str(wt)
    assert calls[0] != str(proj)

    review_id = store.read_status(tid)["successor_id"]
    _go_idle(review_id, window_id="@2")
    rf = store.task_dir(review_id) / "review-1.md"
    rf.write_text("看到一半，先记着。\n\nNEXT: pending", encoding="utf-8")
    store.update_status(review_id, review_verdict="pending", review_file=str(rf),
                        review_recorded_round=1)
    scheduler.tick(cfg, NOW)  # _review_pending release 分支另起同轮审稿
    assert len(calls) == 2
    assert calls[1] == str(wt)
    assert calls[1] != str(proj)


def test_review_pending_release_needs_attention_when_worktree_metadata_missing(
    tmp_path, monkeypatch,
):
    """S7.5 阻断附带守卫：pending release 分支读不到 worktree_path 时必须
    fail-closed 到 needs_attention，不能像旧写法一样悄悄用主项目目录顶替
    ——同 `_start_review_round` 已有的"工作树元数据缺失"口径对齐。走一遍
    真实流水线起好第 1 轮审稿后，模拟崩溃恢复现场把 worktree_path 清掉
    （元数据本该跟着 review 走一份，这里故意抹掉这一份来触发缺失分支）。"""
    Fakes(monkeypatch)
    proj = _make_repo(tmp_path)
    cfg = _review_config_for(proj)
    tid = make_review_task()
    wt = _register_tree(proj, tid, "审稿流水线任务")
    _go_idle(tid, window_id="@1")
    (wt / "canary.txt").write_text("r1\n", encoding="utf-8")
    _write_handover(tid, "写完了。\nNEXT: done")
    scheduler.tick(cfg, NOW)
    review_id = store.read_status(tid)["successor_id"]
    assert store.read_status(review_id).get("worktree_path")  # 正常路径下应已继承

    store.update_status(review_id, worktree_path=None)  # 模拟元数据缺失现场
    _go_idle(review_id, window_id="@2")
    rf = store.task_dir(review_id) / "review-1.md"
    rf.write_text("看到一半。\n\nNEXT: pending", encoding="utf-8")
    store.update_status(review_id, review_verdict="pending", review_file=str(rf),
                        review_recorded_round=1)
    actions = scheduler.tick(cfg, NOW)
    status = store.read_status(review_id)
    assert status["state"] == "needs_attention"
    assert "worktree_path" in status.get("error", "")
    assert any("元数据缺失" in a for a in actions)


def test_atomic_write_text_concurrent_calls_do_not_collide(tmp_path):
    """S7.1 阻断二子问题：atomic_write_text 的临时文件名只用 pid 命名时，
    同进程内并发调用会撞同一个临时文件名；加 uuid nonce 后多线程并发写
    同一路径不再互相踩脚，每次调用都各自独立完成。"""
    import threading

    target = tmp_path / "concurrent.txt"
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            for _ in range(20):
                store.atomic_write_text(target, f"来自线程 {i}\n")
        except BaseException as exc:  # noqa: BLE001 - 就是要抓所有异常
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("来自线程 ")
