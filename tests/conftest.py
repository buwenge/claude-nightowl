"""跨测试文件的兜底夹具。

9/2 补：`_forbid_user_tmux_session`——所有 tmux 子进程都经过 `launcher._tmux`，
这里给它套一层守卫，目标会话不是 `ns-selftest` 就直接抛错。起因：`tests/
test_scheduler.py` 的 CONFIG 曾把 `tmux_session` 写成真实用户会话名 "claude"，
一条漏打桩 `open_notice_window` 的测试在 9/2 上午往用户的 tmux 里真开了六个
"(需要人工)"窗口。会话名兜底（下面那个 kill）只能收拾 `ns-selftest`，管不住
开到别的会话里的窗口，所以要在源头拦。

总review二 G18：`ns-selftest` 是测试专用的 tmux 会话名（AGENTS.md 硬规矩），
理论上每个测试自己该打桩 launcher 的 tmux 调用，不该真的往这个会话里开
窗口。但漏打桩是会发生的（`test_pipeline_skip_review_merge_failure_
returns_non_2xx` 就漏过一次，见 test_server.py 里的注释），漏打桩的后果
是一个挂着 `read` 不退出的通知窗口，会话越攒越多。这里加一个 session 级
兜底：整个 pytest 跑完后把 `ns-selftest` 杀掉——会话不存在时 tmux 会非
零退出，忽略即可（不是这次测试的责任范围）。
"""

import subprocess

import pytest


@pytest.fixture(scope="session", autouse=True)
def _kill_ns_selftest_session_when_done():
    yield
    subprocess.run(
        ["tmux", "kill-session", "-t", "ns-selftest"],
        check=False, capture_output=True,
    )


_ALLOWED_TMUX_SESSION = "ns-selftest"


@pytest.fixture(autouse=True)
def _forbid_user_tmux_session(monkeypatch):
    """任何测试都不许让 tmux 命令指向 `ns-selftest` 以外的会话。

    只看会话级目标：`-t <会话>:...`、`-t <会话>`、`-s <会话>`。窗口/pane 目标
    （`@12`、`%3`）不带会话名，放行。测试自己 monkeypatch 了 `_tmux` 的（如
    test_launcher 里的假 tmux）会覆盖这层，也不受影响。
    """
    from nightshift import launcher

    real_tmux = launcher._tmux

    def guarded(*args):
        for flag, target in zip(args, args[1:]):
            if flag not in ("-t", "-s"):
                continue
            target = str(target)
            if target.startswith(("@", "%")):
                continue
            # tmux 的精确匹配写法带前导 "="（`-t =ns-selftest`），比较前剥掉
            session = target.split(":", 1)[0].lstrip("=")
            if session and session != _ALLOWED_TMUX_SESSION:
                raise AssertionError(
                    f"测试试图操作 tmux 会话 {session!r}（tmux {' '.join(map(str, args))}）；"
                    f"测试只许用 {_ALLOWED_TMUX_SESSION!r}，请给 launcher 打桩或改测试配置"
                )
        return real_tmux(*args)

    monkeypatch.setattr(launcher, "_tmux", guarded)
