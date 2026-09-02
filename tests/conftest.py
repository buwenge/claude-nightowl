"""跨测试文件的兜底夹具。

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
