"""server.py 的测试：临时端口起真服务器，urllib 打 HTTP。

- NIGHTSHIFT_HOME 指 tmp，secure_cookie=false（本机 http 打）；
- 静态页目录 monkeypatch 成 tmp 里的假文件（web/ 真文件另行人工验收）；
- 登录限速在进程内，每个测试起自己的服务器实例，状态互不串。
"""

import json
import os
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
    store.update_status(task_id, state="scheduled", retries=2,
                        retry_at="2026-08-27T10:00:00Z")
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


# ---------- quota ----------


def test_quota_empty_then_loaded(authed, ns_home):
    status, _, body = authed.request("GET", "/api/quota")
    assert status == 200
    assert body == {}

    store.atomic_write_json(ns_home / "quota.json", {
        "usage": {"session_pct": 13, "week_all_pct": 19, "per_model": {"Fable": 35}},
        "fetched_at": store.utc_now_iso(),
    })
    status, _, body = authed.request("GET", "/api/quota")
    assert status == 200
    assert body["usage"]["session_pct"] == 13
    assert isinstance(body["age_seconds"], int)
    assert 0 <= body["age_seconds"] < 60


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
            assert headers["Cache-Control"] == "no-cache"
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
