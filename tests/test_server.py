"""server.py 的测试：临时端口起真服务器，urllib 打 HTTP。

- NIGHTSHIFT_HOME 指 tmp，secure_cookie=false（本机 http 打）；
- 静态页目录 monkeypatch 成 tmp 里的假文件（web/ 真文件另行人工验收）；
- 登录限速在进程内，每个测试起自己的服务器实例，状态互不串。
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http import cookies as http_cookies
from pathlib import Path

import pytest

from nightshift import __main__ as cli
from nightshift import auth, launcher, server, store

REPO_ROOT = Path(__file__).resolve().parent.parent

CONFIG = {
    "tmux_session": "ns-selftest",
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
    "guards": {
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_ratio": 0.8, "keepalive": True,
    },
    "chain": {"max_windows": 3, "on_no_handover": "continue"},
    "prompt_template": "项目 {project_path}｜任务：{title}\n\n{task}\n\n上下文上限 {context_limit}。",
    "context_warn_text": "到线了 {ctx_k}k/{limit_k}k，收尾写 {handover_path}。",
    "chain_template": "第 {shift} 班。交接：{handover}\n{task}",
    "review_template": (
        "REVIEW {title} round={round} base={base_ref}\ndiff: {diff_command}\n"
        "交接：{build_handover}\n上一轮：{previous_review}\n标准：{criteria}\n"
        "{stop_build_hint}只读，末行 NEXT。"
    ),
    "review_fix_template": (
        "FIX {title} round={round}\n审稿意见：{review}\n{worktree_instruction}{task}"
    ),
    "http": {
        "host": "127.0.0.1", "port": 0, "url_prefix": "/nightshift",
        "secure_cookie": False, "cookie_days": 365,
    },
}

PASSWORD = "right-horse-42"


class Client:
    """最小 HTTP 客户端：不跟随重定向、手动带 cookie、自动带 CSRF 头。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cookie: str | None = None

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        self.opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, path: str, body=None, headers=None, csrf=True):
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if csrf:
            hdrs["X-Requested-With"] = "nightshift"
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            resp = self.opener.open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            resp = exc
        raw = resp.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            parsed = raw
        return resp.status, dict(resp.headers), parsed

    def keep_cookie(self, headers: dict) -> None:
        raw = headers.get("Set-Cookie")
        if not raw:
            return
        jar = http_cookies.SimpleCookie()
        jar.load(raw)
        if "ns_auth" in jar:
            self.cookie = f"ns_auth={jar['ns_auth'].value}"


@pytest.fixture
def ns_home(tmp_path, monkeypatch):
    """数据目录指 tmp 并写入整份 config。"""
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    store.atomic_write_json(tmp_path / "config.json", CONFIG)
    return tmp_path


def start_server():
    srv = server.make_server(store.load_config())
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def api_url(ns_home):
    """未设口令的服务器（setup 流程用）。"""
    srv, url = start_server()
    yield url
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def authed(ns_home):
    """已设口令并登录好的客户端。"""
    auth.set_password(PASSWORD)
    srv, url = start_server()
    client = Client(url)
    status, headers, _ = client.request(
        "POST", "/api/login", {"password": PASSWORD}, csrf=False
    )
    assert status == 200, headers
    client.keep_cookie(headers)
    yield client
    srv.shutdown()
    srv.server_close()


# ---------- setup / login / logout / 鉴权 ----------


def test_root_redirects_to_setup_when_no_password(api_url):
    client = Client(api_url)
    status, headers, _ = client.request("GET", "/")
    assert status == 302
    assert headers["Location"] == "./setup"


def test_setup_flow_short_then_ok_then_403(api_url):
    client = Client(api_url)
    status, _, body = client.request("POST", "/api/setup", {"password": "短"}, csrf=False)
    assert status == 400
    assert "8" in body["error"]

    status, headers, body = client.request(
        "POST", "/api/setup", {"password": PASSWORD}, csrf=False
    )
    assert status == 200
    assert "ns_auth=" in headers["Set-Cookie"]
    assert auth.is_set_up()

    client.keep_cookie(headers)
    status, _, _ = client.request("GET", "/api/tasks")
    assert status == 200  # setup 顺手登录了

    status, _, body = client.request("POST", "/api/setup", {"password": "另一个口令啊"}, csrf=False)
    assert status == 403


def test_login_wrong_then_rate_limited(ns_home):
    auth.set_password(PASSWORD)
    srv, url = start_server()
    try:
        client = Client(url)
        for _ in range(5):
            status, _, body = client.request(
                "POST", "/api/login", {"password": "错的不对呀"}, csrf=False
            )
            assert status == 401
            assert body["error"] == "口令不对"
        # 窗口期内连对的口令也拒绝
        status, _, _ = client.request("POST", "/api/login", {"password": PASSWORD}, csrf=False)
        assert status == 429
    finally:
        srv.shutdown()
        srv.server_close()


def test_login_ok_cookie_attributes(ns_home):
    auth.set_password(PASSWORD)
    srv, url = start_server()
    try:
        client = Client(url)
        status, headers, _ = client.request(
            "POST", "/api/login", {"password": PASSWORD}, csrf=False
        )
        assert status == 200
        raw = headers["Set-Cookie"]
        assert "Path=/nightshift/" in raw
        assert "HttpOnly" in raw
        assert "SameSite=Lax" in raw
        assert "Max-Age=31536000" in raw
        assert "Secure" not in raw  # secure_cookie=false
        client.keep_cookie(headers)
        status, _, _ = client.request("GET", "/api/tasks")
        assert status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_needs_cookie(ns_home):
    auth.set_password(PASSWORD)
    srv, url = start_server()
    try:
        client = Client(url)
        status, _, _ = client.request("GET", "/api/tasks")
        assert status == 401
        status, _, _ = client.request("GET", "/api/tasks", headers={"Cookie": "ns_auth=badtoken.0"})
        assert status == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_logout_clears_cookie(authed):
    status, headers, _ = authed.request("POST", "/api/logout")
    assert status == 200
    assert "Max-Age=0" in headers["Set-Cookie"]
    # 服务器只下发布 cookie 的指令；不带 cookie 再打就是 401
    authed.cookie = None
    status, _, _ = authed.request("GET", "/api/tasks")
    assert status == 401


def test_bad_token_is_unauthorized(ns_home):
    auth.set_password(PASSWORD)
    srv, url = start_server()
    try:
        client = Client(url)
        client.cookie = "ns_auth=badtoken.0"
        status, _, _ = client.request("GET", "/api/tasks")
        assert status == 401
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- CSRF 头 ----------


def test_state_changing_requests_need_csrf_header(authed):
    status, _, _ = authed.request(
        "POST", "/api/tasks", {"title": "x"}, csrf=False
    )
    assert status == 403
    status, _, _ = authed.request("PUT", "/api/templates", {"prompt_template": "x"}, csrf=False)
    assert status == 403
    status, _, _ = authed.request("DELETE", "/api/tasks/20260827-120000-abcd", csrf=False)
    assert status == 403
    # login/setup 不要求这个头
    status, _, _ = authed.request("POST", "/api/login", {"password": "错的不对呀"}, csrf=False)
    assert status in (200, 401, 429)


# ---------- /api/config 与模板 ----------


def test_api_config_fields(authed):
    status, _, body = authed.request("GET", "/api/config")
    assert status == 200
    assert body["projects"] == CONFIG["projects"]
    assert body["models"]["claude-fable-5"] == {
        "context_limit": 500000, "usage_label": "Fable",
    }
    assert body["efforts"] == CONFIG["efforts"]
    assert body["guards"] == CONFIG["guards"]
    assert body["chain"] == CONFIG["chain"]
    assert body["prompt_template"] == CONFIG["prompt_template"]
    assert body["context_warn_text"] == CONFIG["context_warn_text"]
    assert body["chain_template"] == CONFIG["chain_template"]
    assert body["display_tz_offset_hours"] == 8


def test_put_templates_changes_only_three_keys(authed, ns_home):
    before = json.loads((ns_home / "config.json").read_text(encoding="utf-8"))
    status, _, body = authed.request("PUT", "/api/templates", {
        "prompt_template": "新的提示词模板 {task}",
        "context_warn_text": "新的到线文案",
        "chain_template": "新的续班模板",
        "projects": {"evil": "/tmp"},  # 多余的键必须被无视
    })
    assert status == 200, body
    after = json.loads((ns_home / "config.json").read_text(encoding="utf-8"))
    assert after["prompt_template"] == "新的提示词模板 {task}"
    assert after["context_warn_text"] == "新的到线文案"
    assert after["chain_template"] == "新的续班模板"
    assert after["projects"] == before["projects"]
    for key in before:
        if key not in ("prompt_template", "context_warn_text", "chain_template"):
            assert after[key] == before[key], key


def test_put_templates_rejects_non_string(authed):
    status, _, _ = authed.request("PUT", "/api/templates", {"prompt_template": 123})
    assert status == 400
    status, _, _ = authed.request("PUT", "/api/templates", {" unrelated ": "x"})
    assert status == 400


# ---------- 预览与建任务 ----------


def test_preview_matches_cmd_add(authed, ns_home):
    argv = [
        "add", "--title", "渲染对照", "--project", "demo",
        "--model", "claude-fable-5", "--effort", "high",
        "--run-at", "2026-08-28 02:30", "--task-text", "正文有 {花括号} 也照发",
    ]
    assert cli.main(argv) == 0
    created = store.list_tasks()[0]["task"]

    status, _, body = authed.request("POST", "/api/preview", {
        "title": "渲染对照", "project": "demo",
        "model": "claude-fable-5", "task_text": "正文有 {花括号} 也照发",
    })
    assert status == 200
    assert body["prompt_final"] == created["prompt_final"]


def test_preview_bad_project(authed):
    status, _, _ = authed.request("POST", "/api/preview", {
        "title": "t", "project": "nope", "model": "m", "task_text": "x",
    })
    assert status == 400


def test_create_task_requires_run_at(authed):
    payload = {
        "title": "没时间", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "task_text": "正文", "prompt_final": "提示词",
    }
    status, _, body = authed.request("POST", "/api/tasks", payload)
    assert status == 400
    assert "run_at" in body["error"]
    status, _, _ = authed.request("POST", "/api/tasks", {**payload, "run_at": ""})
    assert status == 400


def test_create_task_ok_merges_guards_chain(authed, ns_home):
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "网页建的", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "干点活", "prompt_final": "完整提示词",
        "guards": {"context_warn_tokens": 400000},
        "chain": {"max_windows": 2},
    })
    assert status == 201, body
    task_id = body["id"]
    task = store.load_task(task_id)
    assert task["title"] == "网页建的"
    # 缺的键从 config 同名段合并，给的键覆盖
    assert task["guards"] == {
        "session_pct_max": 80, "weekly_pct_max": 95,
        "context_warn_ratio": 0.8, "keepalive": True,
        "context_warn_tokens": 400000,
    }
    assert task["chain"] == {"max_windows": 2, "on_no_handover": "continue"}
    status_data = store.read_status(task_id)
    assert status_data["state"] == "scheduled"
    assert (ns_home / "tasks" / task_id / "task.json").is_file()


def test_create_task_value_errors_are_400(authed):
    base = {
        "title": "非法", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    }
    status, _, body = authed.request("POST", "/api/tasks", {**base, "project": "nope"})
    assert status == 400
    assert "project" in body["error"]
    status, _, body = authed.request(
        "POST", "/api/tasks", {**base, "run_at": "2026-08-28T18:00:00+08:00"}
    )
    assert status == 400
    status, _, _ = authed.request("POST", "/api/tasks", {**base, "guards": "不是对象"})
    assert status == 400


# ---------- S5①：worktree / review 字段与孤儿工作树 API ----------


def test_create_task_worktree_fields(authed):
    # 缺省：服务器不塞字段，create_task 落盘时补 true + review 占位形状
    task_id = make_task(authed, "默认建树")
    task = store.load_task(task_id)
    assert task["worktree"] is True
    assert task["review"] == {"enabled": False, "merge_policy": "manual"}
    # 显式 worktree=false + auto
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "老式任务", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
        "worktree": False, "review": {"enabled": False, "merge_policy": "auto"},
    })
    assert status == 201, body
    task = store.load_task(body["id"])
    assert task["worktree"] is False
    assert task["review"] == {"enabled": False, "merge_policy": "auto"}
    # 非法值：400 带人话
    base = {
        "title": "非法", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    }
    status, _, body = authed.request(
        "POST", "/api/tasks", {**base, "worktree": "yes"})
    assert status == 400
    status, _, body = authed.request(
        "POST", "/api/tasks", {**base, "review": {"enabled": True}})
    assert status == 400
    assert "runner" in body["error"]  # S7：没给 runner，报缺项而不是"S7 才开放"
    status, _, body = authed.request(
        "POST", "/api/tasks", {**base, "review": {"enabled": False, "merge_policy": "yolo"}})
    assert status == 400
    # S7：review.enabled=true 且给全 runner/model/effort，worktree 缺省 true 时能建成
    status, _, body = authed.request(
        "POST", "/api/tasks", {**base, "title": "带审稿", "review": {
            "enabled": True, "runner": "claude", "model": "claude-fable-5", "effort": "high",
        }})
    assert status == 201, body
    review_task = store.load_task(body["id"])
    assert review_task["review"]["enabled"] is True
    assert review_task["review"]["max_rounds"] == 5
    assert review_task["pipeline_id"] == body["id"]


# ---------- S6：runner ----------


def test_api_config_exposes_runners_compat_view(authed):
    """base CONFIG 没有 runners 键：/api/config 合成只含 claude 的兼容视图。"""
    status, _, body = authed.request("GET", "/api/config")
    assert status == 200
    assert set(body["runners"]) == {"claude"}
    assert body["runners"]["claude"]["models"]["claude-fable-5"] == {"context_limit": 500000}
    assert body["runners"]["claude"]["efforts"] == CONFIG["efforts"]


def test_api_config_exposes_codex_runner_when_configured(authed, ns_home):
    cfg = store.load_config()
    cfg["runners"] = {
        "codex": {"bin": "codex", "models": {"gpt-5.6-luna": {"context_limit": None}},
                  "efforts": ["low", "medium", "high", "xhigh"]},
    }
    store.atomic_write_json(ns_home / "config.json", cfg)
    status, _, body = authed.request("GET", "/api/config")
    assert status == 200
    assert set(body["runners"]) == {"claude", "codex"}
    assert body["runners"]["codex"]["models"] == {"gpt-5.6-luna": {"context_limit": None}}
    # 顶层老字段（旧前端还在读的）原样保留，没被 runners 抢走
    assert body["models"]["claude-fable-5"] == {"context_limit": 500000, "usage_label": "Fable"}


def test_create_task_codex_runner_rejected_when_not_configured(authed):
    """base CONFIG 没配 codex：建 codex 任务被 400 拦下，带人话原因。"""
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "该建不成的 codex 任务", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    })
    assert status == 400
    assert "Codex" in body["error"]


def test_create_task_codex_runner_ok_when_configured(authed, ns_home):
    cfg = store.load_config()
    cfg["runners"] = {
        "codex": {"bin": "codex", "models": {"gpt-5.6-luna": {"context_limit": None}},
                  "efforts": ["low", "medium", "high", "xhigh"]},
    }
    store.atomic_write_json(ns_home / "config.json", cfg)
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "Codex 任务", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    })
    assert status == 201, body
    assert store.load_task(body["id"])["runner"] == "codex"


def test_put_task_can_change_runner_when_unrun_not_when_active(authed, ns_home):
    cfg = store.load_config()
    cfg["runners"] = {
        "codex": {"bin": "codex", "models": {"gpt-5.6-luna": {"context_limit": None}},
                  "efforts": ["low", "medium", "high", "xhigh"]},
    }
    store.atomic_write_json(ns_home / "config.json", cfg)
    task_id = make_task(authed, "未跑改 runner")
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {
        "runner": "codex", "model": "gpt-5.6-luna", "effort": "high",
    })
    assert status == 200, body
    assert store.load_task(task_id)["runner"] == "codex"
    # 活跃编辑：runner 不在允许键里，改不了
    store.update_status(task_id, state="working")
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {"runner": "claude"})
    assert status == 400
    assert store.load_task(task_id)["runner"] == "codex"


def test_list_tasks_shows_runner_claude_for_legacy_task_without_rewriting(authed, ns_home):
    """S6 上线前落盘的旧任务没有 runner 字段：列表显示 claude，task.json 不回写。"""
    task_id = make_task(authed, "旧任务")
    task = store.load_task(task_id)
    del task["runner"]
    store.atomic_write_json(store.task_dir(task_id) / "task.json", task)
    status, _, body = authed.request("GET", "/api/tasks")
    assert status == 200
    item = next(i for i in body if i["task"]["id"] == task_id)
    assert item["task"]["runner"] == "claude"
    on_disk = json.loads((store.task_dir(task_id) / "task.json").read_text(encoding="utf-8"))
    assert "runner" not in on_disk  # 展示归展示，磁盘没被偷偷迁移
    status, _, detail = authed.request("GET", f"/api/tasks/{task_id}")
    assert detail["task"]["runner"] == "claude"


def test_preview_worktree_instruction(authed, ns_home):
    # {worktree_instruction} 是可选占位符：模板里有才会渲染
    cfg = store.load_config()
    cfg["prompt_template"] = "项目 {project_path}｜任务：{title}\n\n{task}\n\n{worktree_instruction}上下文上限 {context_limit}。"
    store.atomic_write_json(ns_home / "config.json", cfg)
    body = {
        "title": "T", "project": "demo", "model": "claude-fable-5",
        "task_text": "正文",
    }
    status, _, data = authed.request("POST", "/api/preview", body)
    assert status == 200
    plain = data["prompt_final"]
    status, _, data = authed.request("POST", "/api/preview", {**body, "worktree": True})
    assert status == 200
    assert store.WORKTREE_INSTRUCTION in data["prompt_final"]
    assert store.WORKTREE_INSTRUCTION not in plain


def test_put_task_worktree_review_edit_rules(authed):
    # 未跑：可改 worktree / review
    task_id = make_task(authed, "未跑可改树")
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {
        "worktree": False, "review": {"enabled": False, "merge_policy": "auto"},
    })
    assert status == 200, body
    task = store.load_task(task_id)
    assert task["worktree"] is False
    assert task["review"]["merge_policy"] == "auto"
    # review.enabled=true 但 worktree 仍是上一步设的 false：400 拦下
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {
        "review": {"enabled": True},
    })
    assert status == 400 and "worktree" in body["error"]
    assert store.load_task(task_id)["review"]["enabled"] is False
    # 补全 runner/model/effort + worktree=true：S7 起可以真正开启
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {
        "worktree": True,
        "review": {"enabled": True, "runner": "claude", "model": "claude-fable-5", "effort": "high"},
    })
    assert status == 200, body
    assert store.load_task(task_id)["review"]["enabled"] is True
    # 活跃编辑：worktree / review 不在允许键里
    store.update_status(task_id, state="working")
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {
        "worktree": False,
    })
    assert status == 400
    assert store.load_task(task_id)["worktree"] is True


def test_worktrees_api_requires_auth_and_lists_orphans(authed, ns_home):
    status, _, _ = Client(authed.base).request("GET", "/api/worktrees", csrf=False)
    assert status == 401
    # 没有文件 → 空列表
    status, _, body = authed.request("GET", "/api/worktrees")
    assert status == 200 and body == []
    store.atomic_write_json(ns_home / "orphan_worktrees.json", [
        {"project": "demo", "path": "/p/.claude/worktrees/x",
         "branch": "ns/x", "reason": "夜班工作树没有任务引用它（任务被删了？），只提示不自动删"},
    ])
    status, _, body = authed.request("GET", "/api/worktrees")
    assert status == 200
    assert isinstance(body, list) and len(body) == 1
    assert set(body[0]) == {"project", "path", "branch", "reason"}


# ---------- 任务操作：run-now / cancel / delete / 详情 / 列表 / screen ----------


def make_task(authed, title="列表任务"):
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": title, "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    })
    assert status == 201
    return body["id"]


def test_run_now_on_scheduled_resets_state(authed):
    task_id = make_task(authed, "现在就跑")
    store.update_status(task_id, state="failed", retries=2,
                        retry_at="2026-08-27T10:00:00Z",
                        error="旧错误", postpone_reason="旧原因")
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/run-now")
    assert status == 200, body
    task = store.load_task(task_id)
    status_data = store.read_status(task_id)
    # run_at 被改成当前（用户动作允许），回到 scheduled 且计数清零
    assert task["run_at"] > "2026-08-27"
    assert status_data["state"] == "scheduled"
    assert status_data["retries"] == 0
    assert "retry_at" not in status_data
    assert "next_attempt_at" not in status_data
    assert "error" not in status_data and "postpone_reason" not in status_data


def test_run_now_rejects_idle(authed):
    task_id = make_task(authed, "空转不跑")
    store.update_status(task_id, state="idle")
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/run-now")
    assert status == 409
    assert "idle" in body["error"]


def test_cancel_follows_cli_rules(authed):
    task_id = make_task(authed, "要取消的")
    status, _, _ = authed.request("POST", f"/api/tasks/{task_id}/cancel")
    assert status == 200
    assert store.read_status(task_id)["state"] == "cancelled"
    # cancelled 不能再取消
    status, _, _ = authed.request("POST", f"/api/tasks/{task_id}/cancel")
    assert status == 409


def test_delete_terminal_ok_busy_conflict(authed):
    task_id = make_task(authed, "要删的")
    store.update_status(task_id, state="exited", exit_reason="other")
    status, _, _ = authed.request("DELETE", f"/api/tasks/{task_id}")
    assert status == 200
    assert not store.task_dir(task_id).exists()

    task_id2 = make_task(authed, "还在跑的")
    store.update_status(task_id2, state="idle")
    status, _, _ = authed.request("DELETE", f"/api/tasks/{task_id2}")
    assert status == 409
    assert store.task_dir(task_id2).exists()


def test_task_detail_and_list_with_events_tail(authed):
    task_id = make_task(authed, "带日志的")
    store.append_event(task_id, "第一条")
    store.append_event(task_id, "第二条")

    status, _, body = authed.request("GET", f"/api/tasks/{task_id}")
    assert status == 200
    assert body["task"]["id"] == task_id
    assert body["events"][-1].endswith("第二条")

    status, _, items = authed.request("GET", "/api/tasks")
    assert status == 200
    assert isinstance(items, list)
    item = next(i for i in items if i["task"]["id"] == task_id)
    assert item["events_tail"][-1].endswith("第二条")
    assert "state" in item["status"]


def test_screen_no_window_404_with_window_monkeypatched(authed, monkeypatch):
    task_id = make_task(authed, "看屏幕")
    status, _, body = authed.request("GET", f"/api/tasks/{task_id}/screen")
    assert status == 404

    store.update_status(task_id, state="working", window_id="@7", pane_pid=1)
    calls = {}

    def fake_capture(window_id, lines=200):
        calls["window_id"] = window_id
        calls["lines"] = lines
        return "假屏幕内容"

    monkeypatch.setattr(launcher, "capture_pane", fake_capture)
    status, _, body = authed.request("GET", f"/api/tasks/{task_id}/screen?lines=50")
    assert status == 200
    assert body["text"] == "假屏幕内容"
    assert calls == {"window_id": "@7", "lines": 50}


# ---------- S4① 触发方式：建 after 任务与 trigger_text ----------


def test_create_after_task_201_and_trigger_text(authed):
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "前置任务甲", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    })
    assert status == 201
    pre = body["id"]
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "后继任务乙", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T19:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
        "trigger": {"type": "after", "task": pre, "when": "finished"},
    })
    assert status == 201, body
    after = body["id"]
    assert store.load_task(after)["trigger"] == {
        "type": "after", "task": pre, "when": "finished",
    }

    status, _, items = authed.request("GET", "/api/tasks")
    by_id = {i["task"]["id"]: i for i in items}
    assert by_id[after]["trigger_text"] == "等「前置任务甲」完工后"
    assert by_id[pre]["trigger_text"] == "按时间"

    # when=ended 的文案
    task = store.load_task(after)
    task["trigger"] = {"type": "after", "task": pre, "when": "ended"}
    store.atomic_write_json(store.task_dir(after) / "task.json", task)
    _, _, items = authed.request("GET", "/api/tasks")
    by_id = {i["task"]["id"]: i for i in items}
    assert by_id[after]["trigger_text"] == "等「前置任务甲」结束后"

    # 前置被删了
    shutil.rmtree(store.task_dir(pre))
    _, _, items = authed.request("GET", "/api/tasks")
    by_id = {i["task"]["id"]: i for i in items}
    assert by_id[after]["trigger_text"] == "前置任务不存在"

    # 坏 trigger → 400
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "坏触发", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T19:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
        "trigger": {"type": "after", "task": "20990101-000000-ffff", "when": "finished"},
    })
    assert status == 400
    assert "trigger" in body["error"]


def test_create_after_task_without_run_at_ok(authed):
    """前端 after 模式不发 run_at：服务器要放行，create_task 补创建时刻。"""
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "前置任务丙", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    })
    assert status == 201
    pre = body["id"]
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "没给时间", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "task_text": "正文", "prompt_final": "提示词",
        "trigger": {"type": "after", "task": pre, "when": "finished"},
    })
    assert status == 201, body
    task = store.load_task(body["id"])
    assert task["run_at"].endswith("Z")  # 补成创建时刻，只当排序用
    # 按时间的任务缺 run_at 仍然 400
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "按时间没给", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "task_text": "正文", "prompt_final": "提示词",
    })
    assert status == 400
    assert "run_at" in body["error"]


# ---------- S4② 编辑：PUT /api/tasks/<id> 按状态分级 ----------


def test_put_task_unrun_full_edit_and_event_log(authed):
    task_id = make_task(authed, "可编辑的")
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {
        "title": "改了标题", "project": "demo", "model": "claude-fable-5",
        "effort": "low", "run_at": "2026-08-28T20:00:00Z",
        "task_text": "改了正文", "prompt_final": "改了提示词",
        "guards": {"keepalive": False}, "chain": {"max_windows": 5},
    })
    assert status == 200, body
    task = store.load_task(task_id)
    assert task["title"] == "改了标题"
    assert task["effort"] == "low"
    assert task["run_at"] == "2026-08-28T20:00:00Z"
    assert task["task_text"] == "改了正文"
    assert task["prompt_final"] == "改了提示词"
    assert task["guards"] == {"keepalive": False}
    assert task["chain"] == {"max_windows": 5}
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "网页编辑" in events and "chain" in events


def test_put_task_postponed_back_to_scheduled(authed):
    task_id = make_task(authed, "推迟中编辑")
    store.update_status(task_id, state="postponed",
                        next_attempt_at="2026-08-27T10:00:00Z", postpone_reason="x")
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {"title": "新标题"})
    assert status == 200, body
    status_data = store.read_status(task_id)
    assert status_data["state"] == "scheduled"
    assert "next_attempt_at" not in status_data
    assert "postpone_reason" not in status_data


def test_put_task_postponed_trigger_reset(authed):
    task_id = make_task(authed, "换前置")
    pre = make_task(authed, "新前置")
    store.update_status(task_id, state="postponed",
                        next_attempt_at="2026-08-27T10:00:00Z",
                        trigger_met_at="2026-08-27T09:00:00Z")
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {
        "trigger": {"type": "after", "task": pre, "when": "ended"},
    })
    assert status == 200, body
    assert store.read_status(task_id)["state"] == "scheduled"
    assert store.load_task(task_id)["trigger"]["task"] == pre
    status_data = store.read_status(task_id)
    assert "trigger_met_at" not in status_data  # 换了前置，老的满足时刻作废


def test_put_task_active_restricted(authed):
    task_id = make_task(authed, "在跑的")
    store.update_status(task_id, state="working", window_id="@7", pane_pid=1)
    # 允许的四个维度 + 触发方式
    status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {
        "title": "跑着改标题", "task_text": "跑着改正文",
        "guards": {"session_pct_max": 70}, "chain": {"max_windows": 4},
    })
    assert status == 200, body
    task = store.load_task(task_id)
    assert task["title"] == "跑着改标题"
    assert task["task_text"] == "跑着改正文"
    assert task["guards"] == {"session_pct_max": 70}
    assert task["chain"] == {"max_windows": 4}
    # 带了别的键 → 400，原文件不动
    for key, value in (
        ("run_at", "2026-08-28T20:00:00Z"),
        ("prompt_final", "改提示词"),
        ("model", "claude-haiku-4-5-20251001"),
        ("project", "demo"),
        ("effort", "low"),
    ):
        status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {key: value})
        assert status == 400, key
        assert "这一班正在跑" in body["error"]
    assert store.load_task(task_id)["model"] == "claude-fable-5"


def test_put_task_terminal_conflict(authed):
    task_id = make_task(authed, "完了的")
    for state in ("finished", "exited", "chained", "chain_exhausted", "needs_attention"):
        store.update_status(task_id, state=state)
        status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", {"title": "x"})
        assert status == 409, state


def test_put_task_failed_cancelled_still_editable(authed):
    task_id = make_task(authed, "失败还能改")
    for state in ("failed", "cancelled"):
        store.update_status(task_id, state=state)
        status, _, body = authed.request(
            "PUT", f"/api/tasks/{task_id}", {"title": f"{state}改的"}
        )
        assert status == 200, state
        assert store.load_task(task_id)["title"] == f"{state}改的"


def test_put_task_validation_400_and_404(authed):
    task_id = make_task(authed, "校验")
    for payload in ({"effort": "ultra"}, {"project": "nope"},
                    {"run_at": "2026-08-28 20:00"},
                    {"title": ""}, {"model": ""}):
        status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", payload)
        assert status == 400, payload
    status, _, _ = authed.request("PUT", "/api/tasks/20990101-000000-ffff", {"title": "x"})
    assert status == 404


# ---------- S4② 捎话：草稿存/读/删/发 ----------


def test_message_draft_save_read_delete(authed):
    task_id = make_task(authed, "捎话对象")
    status, _, body = authed.request(
        "POST", f"/api/tasks/{task_id}/message", {"text": "记得先跑测试", "send": False}
    )
    assert status == 200 and body == {"saved": True}
    assert (store.task_dir(task_id) / "draft.txt").read_text(
        encoding="utf-8") == "记得先跑测试"
    _, _, items = authed.request("GET", "/api/tasks")
    item = next(i for i in items if i["task"]["id"] == task_id)
    assert item["draft"] == "记得先跑测试"
    # 覆盖存
    authed.request("POST", f"/api/tasks/{task_id}/message", {"text": "改了主意", "send": False})
    _, _, items = authed.request("GET", "/api/tasks")
    item = next(i for i in items if i["task"]["id"] == task_id)
    assert item["draft"] == "改了主意"
    # 删草稿
    status, _, body = authed.request("DELETE", f"/api/tasks/{task_id}/message")
    assert status == 200
    assert not (store.task_dir(task_id) / "draft.txt").exists()
    _, _, items = authed.request("GET", "/api/tasks")
    item = next(i for i in items if i["task"]["id"] == task_id)
    assert item["draft"] is None


def test_message_send_calls_send_keys_and_clears_draft(authed, monkeypatch):
    task_id = make_task(authed, "要发的")
    store.atomic_write_text(store.task_dir(task_id) / "draft.txt", "旧草稿")
    store.update_status(task_id, state="working", window_id="@9", pane_pid=1)
    calls = []
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: calls.append((wid, text)))
    status, _, body = authed.request(
        "POST", f"/api/tasks/{task_id}/message",
        {"text": "第一行\n第二行\r\n第三行", "send": True},
    )
    assert status == 200 and body == {"sent": True}
    # 多行折成单行进输入框
    assert calls == [("@9", "第一行 第二行 第三行")]
    assert not (store.task_dir(task_id) / "draft.txt").exists()  # 发完清草稿
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "捎话：" in events and "第一行" in events


def test_message_send_without_live_window_409(authed):
    task_id = make_task(authed, "没窗口")
    status, _, body = authed.request(
        "POST", f"/api/tasks/{task_id}/message", {"text": "hi", "send": True}
    )
    assert status == 409
    # 会话已结束的窗口也发不了
    store.update_status(task_id, state="exited", window_id="@9",
                        session_ended_at="2026-08-27T10:00:00Z")
    status, _, _ = authed.request(
        "POST", f"/api/tasks/{task_id}/message", {"text": "hi", "send": True}
    )
    assert status == 409
    # 但可以存草稿
    status, _, body = authed.request(
        "POST", f"/api/tasks/{task_id}/message", {"text": "hi", "send": False}
    )
    assert status == 200


def test_message_bad_text_400(authed):
    task_id = make_task(authed, "空文本")
    for payload in ({"text": "   ", "send": False}, {"send": False},
                    {"text": 123, "send": True}):
        status, _, _ = authed.request("POST", f"/api/tasks/{task_id}/message", payload)
        assert status == 400, payload


# ---------- S4② 中止 / 停后台 ----------


def test_interrupt_sends_escape_once_and_keeps_state(authed, monkeypatch):
    task_id = make_task(authed, "要中止的")
    escapes = []
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    monkeypatch.setattr(launcher, "send_escape", lambda wid: escapes.append(wid))
    store.update_status(task_id, state="working", window_id="@10", pane_pid=1)
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/interrupt")
    assert status == 200 and body == {"ok": True}
    assert escapes == ["@10"]
    assert store.read_status(task_id)["state"] == "working"  # 不改 state，等 hook 报
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "中止：Esc" in events
    # 没窗口 → 409
    other = make_task(authed, "没窗中止")
    status, _, _ = authed.request("POST", f"/api/tasks/{other}/interrupt")
    assert status == 409


def test_stop_background_sends_config_text(authed, monkeypatch):
    task_id = make_task(authed, "停后台")
    store.update_status(task_id, state="waiting_background", window_id="@11", pane_pid=1)
    sent = []
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: sent.append((wid, text)))
    # 文案来自 config（模板页可改）
    status, _, _ = authed.request(
        "PUT", "/api/templates", {"stop_background_text": "自定义停后台文案"}
    )
    assert status == 200
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/stop-background")
    assert status == 200 and body == {"ok": True}
    assert sent == [("@11", "自定义停后台文案")]
    events = (store.task_dir(task_id) / "events.log").read_text(encoding="utf-8")
    assert "停后台" in events
    # /api/config 暴露该键
    _, _, cfg = authed.request("GET", "/api/config")
    assert cfg["stop_background_text"] == "自定义停后台文案"
    # 没窗口 → 409
    other = make_task(authed, "没窗停后台")
    status, _, _ = authed.request("POST", f"/api/tasks/{other}/stop-background")
    assert status == 409


def test_task_detail_and_list_expose_background_summary_for_codex(authed, ns_home):
    from nightshift import background_runner
    cfg = store.load_config()
    cfg["runners"] = {
        "codex": {"bin": "codex", "models": {"gpt-5.6-luna": {"context_limit": None}},
                  "efforts": ["low", "medium", "high", "xhigh"]},
    }
    store.atomic_write_json(ns_home / "config.json", cfg)
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "Codex后台摘要", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    })
    task_id = body["id"]
    # S6.1 A3：finished_pending 也要算上 stopped（bg-4）
    background_runner.modify_registry(task_id, lambda d: d.update({
        "bg-1": {"state": "running"},
        "bg-2": {"state": "finished", "notification_state": "pending"},
        "bg-3": {"state": "finished", "notification_state": "notified"},
        "bg-4": {"state": "stopped", "notification_state": "pending"},
    }))
    status, _, detail = authed.request("GET", f"/api/tasks/{task_id}")
    assert status == 200
    assert detail["background_summary"] == {"running": 1, "finished_pending": 2}

    status, _, items = authed.request("GET", "/api/tasks")
    item = next(i for i in items if i["task"]["id"] == task_id)
    assert item["background_summary"] == {"running": 1, "finished_pending": 2}


def test_task_detail_does_not_leak_raw_background_registry(authed, ns_home):
    """S6.1 B1：任务详情只返回 background_summary（两个数字），绝不原样透传
    整份 registry——argv/output_tail/result_path 都可能带敏感内容，前端也
    没用到这份明细。放一个 canary secret 进登记簿，断言响应体里找不到它，
    也确认 background_items 这个键彻底不存在了。"""
    from nightshift import background_runner
    cfg = store.load_config()
    cfg["runners"] = {
        "codex": {"bin": "codex", "models": {"gpt-5.6-luna": {"context_limit": None}},
                  "efforts": ["low", "medium", "high", "xhigh"]},
    }
    store.atomic_write_json(ns_home / "config.json", cfg)
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "Codex泄密检查", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    })
    task_id = body["id"]
    canary = "CANARY-SECRET-do-not-leak-9f3a"
    background_runner.modify_registry(task_id, lambda d: d.update({
        "bg-1": {
            "state": "finished", "notification_state": "pending",
            "argv_summary": canary, "output_tail": canary,
            "result_path": f"/root/.nightshift/tasks/{task_id}/background/{canary}.log",
            "sandbox_pid": 12345,
        },
    }))
    status, _, detail = authed.request("GET", f"/api/tasks/{task_id}")
    assert status == 200
    assert "background_items" not in detail
    assert canary not in json.dumps(detail, ensure_ascii=False)

    # Claude 任务不该带这个键（没有后台登记簿这个概念）
    claude_id = make_task(authed, "普通claude任务")
    status, _, claude_detail = authed.request("GET", f"/api/tasks/{claude_id}")
    assert "background_summary" not in claude_detail


def test_stop_background_codex_uses_background_runner_text(authed, ns_home, monkeypatch):
    """S6④：Codex 没有 TaskStop，停后台文案要明确指向 background_runner 的 list/stop。"""
    cfg = store.load_config()
    cfg["runners"] = {
        "codex": {"bin": "codex", "models": {"gpt-5.6-luna": {"context_limit": None}},
                  "efforts": ["low", "medium", "high", "xhigh"]},
    }
    store.atomic_write_json(ns_home / "config.json", cfg)
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "Codex停后台", "project": "demo", "runner": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
    })
    assert status == 201, body
    task_id = body["id"]
    store.update_status(task_id, state="waiting_background", window_id="@11", pane_pid=1)
    sent = []
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: sent.append((wid, text)))
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/stop-background")
    assert status == 200, body
    assert len(sent) == 1
    assert "background_runner" in sent[0][1]
    assert "TaskStop" not in sent[0][1]


# ---------- S4.1 必修1/必修3：窗口真存活现查 tmux；事件一行 ----------


def test_message_send_window_gone_409_no_side_effects(authed, monkeypatch):
    """S4.1：账面 window_id 在、session_ended_at 空，但 tmux 里窗口已消失
    → 409；不 send_keys、不删草稿、不记"已发出"事件。"""
    task_id = make_task(authed, "窗口早没了")
    store.atomic_write_text(store.task_dir(task_id) / "draft.txt", "还没发的话")
    store.update_status(task_id, state="working", window_id="@13", pane_pid=1)
    calls = []
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: False)
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: calls.append((wid, text)))
    log = store.task_dir(task_id) / "events.log"
    before = log.read_text(encoding="utf-8") if log.is_file() else ""
    status, _, body = authed.request(
        "POST", f"/api/tasks/{task_id}/message", {"text": "到了吗", "send": True}
    )
    assert status == 409
    assert calls == []  # 不敲键
    assert (store.task_dir(task_id) / "draft.txt").read_text(
        encoding="utf-8") == "还没发的话"  # 不删草稿
    after = log.read_text(encoding="utf-8") if log.is_file() else ""
    assert after == before  # 不记"已发出"


def test_interrupt_and_stop_background_window_gone_409(authed, monkeypatch):
    """S4.1：窗口真没了时，中止与停后台一律 409，不碰 tmux、不留事件。"""
    task_id = make_task(authed, "假活窗口")
    store.update_status(task_id, state="working", window_id="@14", pane_pid=1)
    escapes, keys = [], []
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: False)
    monkeypatch.setattr(launcher, "send_escape", lambda wid: escapes.append(wid))
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: keys.append((wid, text)))
    status, _, _ = authed.request("POST", f"/api/tasks/{task_id}/interrupt")
    assert status == 409
    status, _, _ = authed.request("POST", f"/api/tasks/{task_id}/stop-background")
    assert status == 409
    assert escapes == [] and keys == []
    log = store.task_dir(task_id) / "events.log"
    events = log.read_text(encoding="utf-8") if log.is_file() else ""
    assert "中止" not in events and "停后台" not in events


def test_message_send_event_stays_one_line(authed, monkeypatch):
    """S4.1 必修3：多行捎话发送后，events.log 新增事件只占一行、不含 \\r \\n。"""
    task_id = make_task(authed, "多行捎话")
    store.update_status(task_id, state="working", window_id="@15", pane_pid=1)
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: None)
    log = store.task_dir(task_id) / "events.log"
    before = len(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else 0
    status, _, body = authed.request(
        "POST", f"/api/tasks/{task_id}/message",
        {"text": "第一行\n第二行\r\n第三行", "send": True},
    )
    assert status == 200 and body == {"sent": True}
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == before + 1  # 一个事件一行，多行文本不许拆行
    assert "\r" not in lines[-1]
    assert "捎话：" in lines[-1] and "第一行 第二行 第三行" in lines[-1]


# ---------- S4.1 必修4：PUT 不许把 guards / chain 写坏 ----------


def test_put_task_rejects_bad_guards_chain_and_keeps_old_file(authed):
    task_id = make_task(authed, "guards防坏")
    before = store.load_task(task_id)
    bad_payloads = (
        {"guards": "不是对象"},
        {"chain": [1, 2]},
        {"guards": {"auto_interrupt_minutes": 0}},
        {"guards": {"auto_interrupt_minutes": -2}},
        {"guards": {"auto_interrupt_minutes": True}},   # bool 不算整数
        {"guards": {"auto_interrupt_minutes": "5"}},
        {"guards": {"auto_interrupt_minutes": 2.5}},
    )
    for payload in bad_payloads:
        status, _, body = authed.request("PUT", f"/api/tasks/{task_id}", payload)
        assert status == 400, payload
        assert "auto_interrupt" in body["error"] or "对象" in body["error"], payload
        assert store.load_task(task_id) == before  # 旧 task.json 一字不改
    # 正常值仍可整体替换
    status, _, body = authed.request(
        "PUT", f"/api/tasks/{task_id}", {"guards": {"auto_interrupt_minutes": 5}}
    )
    assert status == 200, body
    assert store.load_task(task_id)["guards"] == {"auto_interrupt_minutes": 5}


# ---------- quota ----------


def test_quota_empty_then_loaded(authed, ns_home):
    """S6：两家各一份，缺失时是显式的空壳（usage=None），不是裸 {}。"""
    status, _, body = authed.request("GET", "/api/quota")
    assert status == 200
    assert body == {
        "claude": {"usage": None, "fetched_at": None, "error": None, "age_seconds": None},
        "codex": {"usage": None, "fetched_at": None, "error": None, "age_seconds": None},
    }

    # 一期旧形状（quota.json 整份就是 claude 那份）按 claude 解释
    store.atomic_write_json(ns_home / "quota.json", {
        "usage": {"session_pct": 13, "week_all_pct": 19, "per_model": {"Fable": 35}},
        "fetched_at": store.utc_now_iso(),
    })
    status, _, body = authed.request("GET", "/api/quota")
    assert status == 200
    assert body["claude"]["usage"]["session_pct"] == 13
    assert isinstance(body["claude"]["age_seconds"], int)
    assert 0 <= body["claude"]["age_seconds"] < 60
    assert body["codex"]["usage"] is None


# ---------- 静态文件与路径安全 ----------


def test_static_files_and_traversal(ns_home, tmp_path, monkeypatch):
    web = tmp_path / "webroot"
    web.mkdir()
    (web / "app.js").write_text("// js", encoding="utf-8")
    (web / "style.css").write_text("body{}", encoding="utf-8")
    (web / "index.html").write_text("<html></html>", encoding="utf-8")
    (web / "login.html").write_text("<html></html>", encoding="utf-8")
    (web / "setup.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(server, "WEB_DIR", web)

    auth.set_password(PASSWORD)
    srv, url = start_server()
    try:
        client = Client(url)
        for name, ctype in (
            ("app.js", "text/javascript"), ("style.css", "text/css"),
            ("index.html", "text/html"), ("login.html", "text/html"),
        ):
            status, headers, raw = client.request("GET", f"/{name}")
            assert status == 200, name
            assert headers["Content-Type"].startswith(ctype)
            assert headers["Cache-Control"] == "no-store"
            assert raw
        # 白名单外与路径穿越一律 404
        for path in ("/nope.js", "/etc/passwd", "/../etc/passwd",
                     "/../../etc/passwd", "/%2e%2e/etc/passwd"):
            status, _, _ = client.request("GET", path)
            assert status == 404, path
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- 路径形状安全 ----------


def test_bad_task_ids_are_404(authed):
    for path in (
        "/api/tasks/xyz",
        "/api/tasks/2026-8-27-abcd",
        "/api/tasks/20260827-120000-abcdXYZ",
        "/api/tasks/20260827-120000-abcd/../../etc",
        "/api/tasks/20260827-120000-abcd/run-now/extra",
    ):
        status, _, _ = authed.request("GET", path)
        assert status == 404, path
    status, _, _ = authed.request(
        "POST", "/api/tasks/99999999-999999-zzzz/run-now"
    )
    assert status == 404
    status, _, _ = authed.request("DELETE", "/api/tasks/nope/cancel")
    assert status == 404


def test_unknown_path_is_404_json(authed):
    status, headers, body = authed.request("GET", "/api/nothing")
    assert status == 404
    assert "error" in body


# ---------- serve 冒烟：--once / --no-http 不受 HTTP 影响 ----------


def test_serve_once_smoke_still_one_tick(ns_home):
    """子进程 `serve --once`（含 --no-http 组合）只跑一轮 tick，不起网页。"""
    env = {**os.environ, "NIGHTSHIFT_HOME": str(ns_home)}
    for extra in (["--once"], ["--no-http", "--once"]):
        proc = subprocess.run(
            [sys.executable, "-m", "nightshift", "serve", *extra],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == ""  # 空数据目录一轮 tick 无动作


def test_config_exposes_optional_home_link(authed):
    cfg = store.load_config()
    cfg.setdefault("http", {})["home_link"] = {"text": "← 主站", "href": "/"}
    store.atomic_write_json(store.home() / "config.json", cfg)
    status, _, body = authed.request("GET", "/api/config")
    assert status == 200
    assert body["home_link"] == {"text": "← 主站", "href": "/"}


def test_delete_allows_chained(authed):
    task_id = make_task(authed, "已续班的父任务")
    store.update_status(task_id, state="chained", successor_id="20260101-000000-0000")
    status, _, _ = authed.request("DELETE", f"/api/tasks/{task_id}")
    assert status == 200
    assert not store.task_dir(task_id).exists()


def test_static_no_store_and_versioned_assets(authed):
    status, headers, body = authed.request("GET", "/index.html")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"
    text = body if isinstance(body, str) else str(body)
    assert "./app.js?v=" in text and "./style.css?v=" in text


def test_quota_refresh_endpoint_default_refreshes_both(authed, monkeypatch):
    from nightshift import server as server_mod
    claude_fake = {"session_pct": 12, "week_all_pct": 34, "per_model": {"Fable": 56}, "raw": ""}
    codex_fake = {"session_pct": 7, "week_all_pct": 2, "per_model": {},
                  "rate_limit_reached_type": None, "reset_credits_available": 1, "windows": {}}
    monkeypatch.setattr(server_mod.quota, "fetch_usage_claude", lambda cfg: claude_fake)
    monkeypatch.setattr(server_mod.quota, "fetch_usage_codex", lambda cfg: codex_fake)
    status, _, body = authed.request("POST", "/api/quota/refresh")
    assert status == 200, body
    assert body["claude"]["usage"]["session_pct"] == 12 and body["claude"]["age_seconds"] is not None
    assert body["codex"]["usage"]["session_pct"] == 7
    assert "errors" not in body
    assert (store.home() / "quota.json").is_file()


def test_quota_refresh_endpoint_single_runner_failure_502_without_touching_other(authed, monkeypatch):
    from nightshift import quota as quota_mod
    from nightshift import server as server_mod
    monkeypatch.setattr(server_mod.quota, "fetch_usage_codex", lambda cfg: {"session_pct": 9, "week_all_pct": 1, "per_model": {}})
    authed.request("POST", "/api/quota/refresh?runner=codex")

    def boom(cfg):
        raise quota_mod.UsageUnavailable("x")
    monkeypatch.setattr(server_mod.quota, "fetch_usage_claude", boom)
    status, _, body = authed.request("POST", "/api/quota/refresh?runner=claude")
    assert status == 502 and "额度查不到" in body["error"]
    # 明确只刷 claude 失败：不该动到刚刷好的 codex 那份
    status, _, body = authed.request("GET", "/api/quota")
    assert body["codex"]["usage"]["session_pct"] == 9
    assert body["claude"]["error"] == "x"


def test_quota_refresh_endpoint_bad_runner_400(authed):
    status, _, body = authed.request("POST", "/api/quota/refresh?runner=gemini")
    assert status == 400


def test_warmup_settings_roundtrip(authed):
    status, _, body = authed.request("PUT", "/api/warmup", {"enabled": True, "times": "18:00, 7:30"})
    assert status == 200, body
    w = json.load(open(store.home() / "config.json", encoding="utf-8"))["warmup"]
    assert w["enabled"] is True and w["times"] == ["07:30", "18:00"] and w["time_local"] == "07:30"
    status, _, body = authed.request("GET", "/api/config")
    assert body["warmup"]["times"] == ["07:30", "18:00"]
    status, _, body = authed.request("PUT", "/api/warmup", {"enabled": True, "times": "25:99"})
    assert status == 400
    status, _, body = authed.request("PUT", "/api/warmup", {"enabled": True, "times": ""})
    assert status == 400


# ---------- S5②：合并 / 丢弃 API 与删除保护 ----------


def _make_git_repo(tmp_path) -> Path:
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


def _worktree_task(authed, proj, tmp_path, *, policy="manual", state="awaiting_merge"):
    """网页建 worktree 任务 → 真建树并打一颗存档 → 停在指定状态。"""
    status_code, _, body = authed.request("POST", "/api/tasks", {
        "title": "工作树任务", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
        "review": {"enabled": False, "merge_policy": policy},
    })
    assert status_code == 201, body
    task_id = body["id"]
    from nightshift import worktree as wt_mod
    meta = wt_mod.ensure_worktree(store.load_task(task_id), proj)
    store.update_status(task_id, **meta)
    wt = Path(meta["worktree_path"])
    (wt / "canary.txt").write_text("活\n", encoding="utf-8")
    wt_mod.checkpoint(store.load_task(task_id), wt)
    store.update_status(task_id, state=state)
    return task_id, wt, meta


def test_merge_api_requires_csrf_then_success(authed, ns_home, tmp_path):
    proj = _make_git_repo(tmp_path)
    cfg = store.load_config()
    cfg["projects"] = {"demo": str(proj)}
    store.atomic_write_json(ns_home / "config.json", cfg)
    task_id, wt, meta = _worktree_task(authed, proj, tmp_path)

    # 两道闸：缺 CSRF 头先 403
    status, _, _ = authed.request(
        "POST", f"/api/tasks/{task_id}/merge", {}, csrf=False)
    assert status == 403

    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/merge")
    assert status == 200, body
    task_status = store.read_status(task_id)
    assert task_status["state"] == "merged"
    assert task_status.get("merge_sha")
    assert not wt.exists()  # 树清掉
    assert meta["branch"] not in subprocess.run(
        ["git", "-C", str(proj), "branch", "--list", meta["branch"]],
        capture_output=True, text=True, check=True).stdout
    # 主线多一颗 --no-ff merge commit
    parents = subprocess.run(
        ["git", "-C", str(proj), "rev-list", "--parents", "-n", "1", "HEAD"],
        capture_output=True, text=True, check=True).stdout.split()
    assert len(parents) == 3
    # merged 可删除
    status, _, body = authed.request("DELETE", f"/api/tasks/{task_id}")
    assert status == 200, body


def test_merge_api_dirty_main_409_keeps_everything(authed, ns_home, tmp_path):
    proj = _make_git_repo(tmp_path)
    cfg = store.load_config()
    cfg["projects"] = {"demo": str(proj)}
    store.atomic_write_json(ns_home / "config.json", cfg)
    task_id, wt, meta = _worktree_task(authed, proj, tmp_path)
    (proj / "工头的改动.txt").write_text("别动\n", encoding="utf-8")

    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/merge")
    assert status == 409
    assert "主线有你没提交的改动" in body["error"]
    task_status = store.read_status(task_id)
    assert task_status["state"] == "needs_attention"
    assert task_status["error"] == body["error"]  # 卡片红字与接口一致
    assert Path(wt).exists() and "worktree_path" in task_status
    # 处理完主线 → needs_attention 也能重试同一 API
    (proj / "工头的改动.txt").unlink()
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/merge")
    assert status == 200, body
    assert store.read_status(task_id)["state"] == "merged"


def test_merge_api_wrong_state_409(authed, ns_home, tmp_path):
    proj = _make_git_repo(tmp_path)
    cfg = store.load_config()
    cfg["projects"] = {"demo": str(proj)}
    store.atomic_write_json(ns_home / "config.json", cfg)
    task_id, _, _ = _worktree_task(
        authed, proj, tmp_path, state="awaiting_merge")
    store.update_status(task_id, state="scheduled")
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/merge")
    assert status == 409
    assert "不能合并" in body["error"]


def test_discard_api_success_and_state_guards(authed, ns_home, tmp_path):
    proj = _make_git_repo(tmp_path)
    cfg = store.load_config()
    cfg["projects"] = {"demo": str(proj)}
    store.atomic_write_json(ns_home / "config.json", cfg)
    task_id, wt, meta = _worktree_task(
        authed, proj, tmp_path, state="failed")
    # failed 但有树：可丢弃
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/discard")
    assert status == 200, body
    assert store.read_status(task_id)["state"] == "discarded"
    assert not wt.exists()
    assert meta["branch"] not in subprocess.run(
        ["git", "-C", str(proj), "branch", "--list", meta["branch"]],
        capture_output=True, text=True, check=True).stdout
    # 已丢弃再丢：409
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/discard")
    assert status == 409
    # discarded 可删除
    status, _, _ = authed.request("DELETE", f"/api/tasks/{task_id}")
    assert status == 200


def test_discard_api_refuses_foreign_path(authed, ns_home, tmp_path):
    proj = _make_git_repo(tmp_path)
    cfg = store.load_config()
    cfg["projects"] = {"demo": str(proj)}
    store.atomic_write_json(ns_home / "config.json", cfg)
    task_id, wt, meta = _worktree_task(authed, proj, tmp_path)
    store.update_status(task_id, worktree_path="/tmp/opencode/not-mine")
    status, _, body = authed.request("POST", f"/api/tasks/{task_id}/discard")
    assert status == 409
    assert "拒绝动它" in body["error"]
    assert Path(wt).exists()  # 什么都没删


def test_delete_blocked_while_tree_exists(authed, ns_home, tmp_path):
    proj = _make_git_repo(tmp_path)
    cfg = store.load_config()
    cfg["projects"] = {"demo": str(proj)}
    store.atomic_write_json(ns_home / "config.json", cfg)
    task_id, wt, _ = _worktree_task(authed, proj, tmp_path)
    status, _, body = authed.request("DELETE", f"/api/tasks/{task_id}")
    assert status == 409
    assert "先合并进主线或丢弃" in body["error"]
    # 丢弃后即可删
    status, _, _ = authed.request("POST", f"/api/tasks/{task_id}/discard")
    assert status == 200
    status, _, _ = authed.request("DELETE", f"/api/tasks/{task_id}")
    assert status == 200


def test_edit_cannot_move_or_disable_an_existing_tree(authed, ns_home, tmp_path):
    proj = _make_git_repo(tmp_path)
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    other = _make_git_repo(other_root)
    cfg = store.load_config()
    cfg["projects"] = {"demo": str(proj), "other": str(other)}
    store.atomic_write_json(ns_home / "config.json", cfg)
    task_id, wt, _ = _worktree_task(authed, proj, tmp_path, state="failed")

    status, _, body = authed.request(
        "PUT", f"/api/tasks/{task_id}", {"project": "other"},
    )
    assert status == 409 and "不能再换项目" in body["error"]
    status, _, body = authed.request(
        "PUT", f"/api/tasks/{task_id}", {"worktree": False},
    )
    assert status == 409 and "不能切回老式模式" in body["error"]
    task = store.load_task(task_id)
    assert task["project"] == "demo" and task["worktree"] is True
    assert wt.exists()


# ---------- S7④：流水线控制 API（我来看/继续/保活/现在就审/跳过审稿/直接返工） ----------


def make_review_pipeline(authed, *, build_state="held", review_state="working"):
    """建一条最小审稿流水线：build（held，已存档）→ review（指定状态）。
    正常这条链应由调度器 tick 造出来，这里直接在磁盘上摆好现场，专测控制
    API 本身，不依赖跑一遍真实调度循环。返回 (build_id, review_id)。"""
    status, _, body = authed.request("POST", "/api/tasks", {
        "title": "审稿流水线", "project": "demo", "model": "claude-fable-5",
        "effort": "high", "run_at": "2026-08-28T18:00:00Z",
        "task_text": "正文", "prompt_final": "提示词",
        "review": {"enabled": True, "runner": "claude",
                   "model": "claude-fable-5", "effort": "high"},
    })
    assert status == 201, body
    build_id = body["id"]
    store.update_status(
        build_id, state=build_state, window_id="@1", pane_pid=1,
        checkpoint_done=True, checkpoint_sha="a" * 40,
        worktree_path="/tmp/wt", branch="ns/x", base_ref="deadbeef",
    )
    build_task = store.load_task(build_id)
    review_task = {
        "title": build_task["title"], "project": build_task["project"],
        "runner": build_task["runner"], "model": build_task["model"],
        "effort": build_task["effort"], "run_at": "2026-08-28T18:00:00Z",
        "task_text": build_task["task_text"], "prompt_final": "REVIEW",
        "review": dict(build_task["review"]), "worktree": True,
    }
    review_id = store.create_task(review_task, store.load_config())
    data = store.load_task(review_id)
    data.update({"role": "review", "round": 1, "role_shift": 1,
                 "parent_id": build_id, "pipeline_id": build_id, "shift": 2})
    store.atomic_write_json(store.task_dir(review_id) / "task.json", data)
    store.update_status(
        review_id, state=review_state, window_id="@2", pane_pid=2,
        worktree_path="/tmp/wt", branch="ns/x", base_ref="deadbeef",
    )
    store.update_status(build_id, successor_id=review_id)
    return build_id, review_id


def test_pipeline_action_404_on_unknown_task(authed):
    for action in ("hold", "continue", "keepalive", "review-now", "skip-review", "fix-now"):
        status, _, body = authed.request(
            "POST", f"/api/tasks/20260101-000000-dead/{action}",
            {"paused": True} if action == "keepalive" else None,
        )
        assert status == 404, (action, body)


def test_pipeline_hold_pings_alive_windows_and_is_idempotent(authed, monkeypatch):
    build_id, review_id = make_review_pipeline(authed)
    sent = []
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: sent.append((wid, text)))

    status, _, body = authed.request("POST", f"/api/tasks/{review_id}/hold")
    assert status == 200 and body["hold_requested"] is True
    assert store.read_status(build_id)["hold_requested"] is True  # 记在 pipeline_id（=build_id）上
    assert {w for w, _ in sent} == {"@1", "@2"}  # 两个活窗口都敲过

    sent.clear()
    status, _, body = authed.request("POST", f"/api/tasks/{build_id}/hold")  # 用另一个成员 id 再点一次
    assert status == 200
    assert sent == []  # 幂等：已经请求过，不重复敲


def test_pipeline_hold_marks_review_member_awaiting_verdict_false(authed, monkeypatch):
    """S7.1 阻断二：hold_text 不要求正式 verdict，敲给 review 成员之前要落
    review_awaiting_verdict=False，接下来它的 Stop 会走控制 turn 分支，
    不会被误记成协议缺失→fix。build 成员没有这个字段（build 不认
    review_awaiting_verdict 这个概念），不该被凭空加上。"""
    build_id, review_id = make_review_pipeline(authed)
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: None)

    status, _, body = authed.request("POST", f"/api/tasks/{review_id}/hold")
    assert status == 200 and body["hold_requested"] is True
    assert store.read_status(review_id)["review_awaiting_verdict"] is False
    assert "review_awaiting_verdict" not in store.read_status(build_id)


def test_pipeline_hold_blocks_next_review_verdict_routing(authed, monkeypatch):
    """先按"我来看"，再让审稿给出 done：下一 tick 应该被拦在 held，不直接合并。"""
    from nightshift import scheduler
    build_id, review_id = make_review_pipeline(authed, review_state="idle")
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: None)

    status, _, _ = authed.request("POST", f"/api/tasks/{review_id}/hold")
    assert status == 200
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("都过了。\n\nNEXT: done", encoding="utf-8")
    store.update_status(review_id, review_verdict="done", review_file=str(review_file),
                        review_recorded_round=1)

    from datetime import datetime, timezone
    scheduler.tick(store.load_config(), datetime.now(timezone.utc))
    assert store.read_status(review_id)["state"] == "held"
    assert store.read_status(review_id).get("review_routed_round") != 1


def test_pipeline_continue_after_hold_reevaluates_blocked_band(authed, monkeypatch):
    from nightshift import scheduler
    from datetime import datetime, timezone
    build_id, review_id = make_review_pipeline(authed, review_state="idle")
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    monkeypatch.setattr(launcher, "send_keys", lambda wid, text: None)
    authed.request("POST", f"/api/tasks/{review_id}/hold")
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("都过了。\n\nNEXT: done", encoding="utf-8")
    store.update_status(review_id, review_verdict="done", review_file=str(review_file),
                        review_recorded_round=1)
    scheduler.tick(store.load_config(), datetime.now(timezone.utc))
    assert store.read_status(review_id)["state"] == "held"

    status, _, body = authed.request("POST", f"/api/tasks/{build_id}/continue")
    assert status == 200 and body["resumed"] is True
    assert store.read_status(build_id)["hold_requested"] is False
    assert store.read_status(review_id)["state"] == "idle"
    scheduler.tick(store.load_config(), datetime.now(timezone.utc))
    assert store.read_status(review_id)["state"] == "awaiting_merge"  # 真的往下走了


def test_pipeline_continue_without_hold_or_round_limit_409(authed):
    build_id, _ = make_review_pipeline(authed)
    status, _, body = authed.request("POST", f"/api/tasks/{build_id}/continue")
    assert status == 409 and "继续" in body["error"]


def test_pipeline_continue_round_limit_sets_override_and_reevaluates(authed):
    build_id, review_id = make_review_pipeline(authed, review_state="idle")
    store.update_status(
        build_id, pipeline_phase="round_limit", fix_count=1,
    )
    store.update_status(
        review_id, state="needs_attention",
        error="返工轮数已到线（1/1），继续需要工头确认",
    )
    status, _, body = authed.request("POST", f"/api/tasks/{build_id}/continue")
    assert status == 200 and body["resumed"] is True
    assert store.read_status(build_id)["round_limit_override"] is True
    assert store.read_status(review_id)["state"] == "idle"


def test_pipeline_keepalive_pause_and_resume(authed):
    build_id, review_id = make_review_pipeline(authed)
    status, _, body = authed.request(
        "POST", f"/api/tasks/{review_id}/keepalive", {"paused": True}
    )
    assert status == 200 and body["keepalive_paused"] is True
    assert store.read_status(build_id)["keepalive_paused"] is True  # build 是当前 held 着的那班

    status, _, body = authed.request(
        "POST", f"/api/tasks/{build_id}/keepalive", {"paused": False}
    )
    assert status == 200 and body["keepalive_paused"] is False
    assert store.read_status(build_id)["keepalive_paused"] is False


def test_pipeline_keepalive_bad_body(authed):
    build_id, _ = make_review_pipeline(authed)
    status, _, body = authed.request(
        "POST", f"/api/tasks/{build_id}/keepalive", {"paused": "yes"}
    )
    assert status == 400


def test_pipeline_review_now_only_targets_postponed_reviewer(authed):
    build_id, review_id = make_review_pipeline(authed, review_state="postponed")
    store.update_status(review_id, next_attempt_at="2099-01-01T00:00:00Z")
    status, _, body = authed.request("POST", f"/api/tasks/{build_id}/review-now")
    assert status == 200 and body["task_id"] == review_id
    assert store.read_status(review_id)["next_attempt_at"] <= store.utc_now_iso()

    build_id2, review_id2 = make_review_pipeline(authed, review_state="working")
    status, _, body = authed.request("POST", f"/api/tasks/{build_id2}/review-now")
    assert status == 409


def test_pipeline_skip_review_cancels_pending_review_and_finalizes(authed):
    build_id, review_id = make_review_pipeline(authed, review_state="scheduled")
    status, _, body = authed.request("POST", f"/api/tasks/{build_id}/skip-review")
    assert status == 200 and body["task_id"] == build_id
    assert store.read_status(review_id)["state"] == "cancelled"
    assert store.read_status(build_id)["state"] == "awaiting_merge"  # manual merge_policy


def test_pipeline_skip_review_requires_checkpointed_held_build(authed):
    build_id, review_id = make_review_pipeline(authed, build_state="working")
    status, _, body = authed.request("POST", f"/api/tasks/{build_id}/skip-review")
    assert status == 409


def test_pipeline_fix_now_with_instruction_advances_round(authed, monkeypatch):
    build_id, review_id = make_review_pipeline(authed, review_state="idle")
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    sent = []

    def fake_send_keys(wid, text):
        sent.append((wid, text))
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(launcher, "send_keys", fake_send_keys)

    status, _, body = authed.request(
        "POST", f"/api/tasks/{build_id}/fix-now", {"instruction": "不等审稿了，先改这个 bug"}
    )
    assert status == 200, body
    assert store.load_task(build_id)["round"] == 2
    assert store.read_status(build_id)["state"] == "working"
    assert store.read_status(build_id)["fix_count"] == 1
    assert any("不等审稿了" in text for _, text in sent)


def test_pipeline_fix_now_empty_instruction_reuses_latest_review(authed, monkeypatch):
    build_id, review_id = make_review_pipeline(authed, review_state="idle")
    monkeypatch.setattr(launcher, "window_alive", lambda wid, config: True)
    sent = []

    def fake_send_keys(wid, text):
        sent.append((wid, text))
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(launcher, "send_keys", fake_send_keys)
    review_file = store.task_dir(review_id) / "review-1.md"
    review_file.write_text("上一轮意见：漏了个边界。\n\nNEXT: fix", encoding="utf-8")
    store.update_status(review_id, review_file=str(review_file))

    status, _, body = authed.request("POST", f"/api/tasks/{build_id}/fix-now", {})
    assert status == 200, body
    assert any("漏了个边界" in text for _, text in sent)


def test_pipeline_fix_now_blank_instruction_and_no_review_400(authed):
    build_id, _ = make_review_pipeline(authed, review_state="idle")
    status, _, body = authed.request("POST", f"/api/tasks/{build_id}/fix-now", {"instruction": "   "})
    assert status == 400


def test_pipeline_fix_now_blocked_while_something_working(authed):
    build_id, review_id = make_review_pipeline(authed, review_state="working")
    status, _, body = authed.request(
        "POST", f"/api/tasks/{build_id}/fix-now", {"instruction": "改一下"}
    )
    assert status == 409


def test_pipeline_fix_now_respects_round_limit(authed):
    build_id, review_id = make_review_pipeline(authed, review_state="idle")
    store.update_status(build_id, fix_count=5)  # 默认 max_rounds=5，已到线
    status, _, body = authed.request(
        "POST", f"/api/tasks/{build_id}/fix-now", {"instruction": "再改改"}
    )
    assert status == 409 and "到线" in body["error"]
