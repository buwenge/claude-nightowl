"""HTTP 服务：自带登录的静态页 + JSON API（ThreadingHTTPServer，纯标准库）。

部署形态（按 nginx 反代设计，本模块不管 nginx）：
    location /nightshift/ { proxy_pass http://127.0.0.1:8190/; }
前缀会被剥掉，本服务看到的路径一律不带 /nightshift——所以：
- 页面里的资源引用一律相对路径（./app.js）；
- cookie 的 Path 按 config.http.url_prefix + "/" 下发。

安全三道闸：
- 口令 pbkdf2（auth.py），cookie 是 HMAC 签名的过期 token；
- 改状态的请求（POST/PUT/DELETE，login/setup 除外）必须带
  X-Requested-With: nightshift 头（配 SameSite=Lax 防 CSRF）；
- 登录失败进程内限速，同一来源 15 分钟窗口内失败 ≥5 次一律 429。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import urllib.parse
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import auth, launcher, scheduler, store

__all__ = ["make_server", "serve_http"]

logger = logging.getLogger("nightshift.http")

# 静态页目录（仓库根/web）；测试里可整目录替换
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

COOKIE_NAME = "ns_auth"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "nightshift"
MAX_BODY_BYTES = 256 * 1024

# 任务 id 只认这个形状，杜绝路径拼接
_TASK_ID_RE = r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}"
_RE_TASK_DETAIL = re.compile(rf"^/api/tasks/({_TASK_ID_RE})$")
_RE_TASK_ACTION = re.compile(rf"^/api/tasks/({_TASK_ID_RE})/(run-now|cancel)$")
_RE_TASK_DELETE = re.compile(rf"^/api/tasks/({_TASK_ID_RE})$")

# 静态文件白名单：文件名写死，其余一律 404
_STATIC_FILES = {
    "/app.js": "app.js",
    "/style.css": "style.css",
    "/login.html": "login.html",
    "/setup.html": "setup.html",
    "/index.html": "index.html",
}
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

# "现在就跑"允许的状态（设计稿：让调度器下一 tick 走完整预检，不直接 launch）
_RUN_NOW_STATES = ("scheduled", "postponed", "failed", "cancelled")
# 可删除的终态
_TERMINAL_STATES = (
    "exited", "finished", "failed", "cancelled", "chain_exhausted", "needs_attention",
)


def _tail_lines(path: Path, count: int) -> list[str]:
    """文本文件末 N 行；没有/读不了返回空表。"""
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()[-count:]
    except OSError:
        return []


class _Handler(BaseHTTPRequestHandler):
    """所有路由都在这一个类里；config/limiter 由 make_server 注入子类。"""

    config: dict
    limiter: auth.LoginRateLimiter
    protocol_version = "HTTP/1.1"
    server_version = "nightshift"
    sys_version = ""

    # ---------- 入口与异常兜底 ----------

    def do_GET(self) -> None:
        self._safe(self._route_get)

    def do_POST(self) -> None:
        # 带请求体的请求：只有真把 body 读干净了才允许复用连接，
        # 否则残留的字节会被当成下一个请求行（keep-alive 下的经典脏读）
        self.close_connection = True
        self._safe(self._route_post)

    def do_PUT(self) -> None:
        self.close_connection = True
        self._safe(self._route_put)

    def do_DELETE(self) -> None:
        self.close_connection = True
        self._safe(self._route_delete)

    def _safe(self, route) -> None:
        """单请求兜底：任何没接住的异常都变 500 JSON，不让线程带炸服务器。"""
        self._sent = False
        try:
            route()
        except Exception:  # noqa: BLE001 - 兜底口
            logger.exception("处理请求出错：%s %s", self.command, self.path)
            if not self._sent:
                self._send_json(500, {"error": "服务器内部错误"})

    def log_message(self, format, *args):  # noqa: A002 - 标准库签名
        pass  # 默认 stderr 访问日志关掉，统一走 nightshift.http

    # ---------- 回写与日志 ----------

    def _source(self) -> str:
        """请求来源：X-Real-IP 头优先（nginx 会覆盖它），否则对端地址。"""
        return self.headers.get("X-Real-IP") or (self.client_address[0] or "-")

    def _access_log(self, code: int) -> None:
        logger.info("%s %s %s 来源=%s", self.command, self.path, code, self._source())

    def _send_json(self, code: int, obj, extra_headers: dict | None = None) -> None:
        body = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._sent = True
        self._access_log(code)

    def _send_file(self, filename: str) -> None:
        path = WEB_DIR / filename
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "文件不存在"})
            return
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)
        self._sent = True
        self._access_log(200)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self._sent = True
        self._access_log(302)

    # ---------- 鉴权 / CSRF / 请求体 ----------

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except Exception:  # Cookie.CookieError 及各种怪格式
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _authed(self) -> bool:
        return auth.verify_token(self._cookie_token())

    def _csrf_ok(self) -> bool:
        return self.headers.get(CSRF_HEADER) == CSRF_VALUE

    def _require_auth(self) -> bool:
        """没登录就回 401 并返回 False。"""
        if self._authed():
            return True
        self._send_json(401, {"error": "未登录或登录已过期"})
        return False

    def _read_json(self) -> dict | None:
        """读 JSON 请求体；出错时已回错误响应，返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            self._send_json(400, {"error": "Content-Length 不合法"})
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True  # 身子没读完，连接不能复用
            self._send_json(413, {"error": "请求体超过 256 KB 上限"})
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json(415, {"error": "Content-Type 必须是 application/json"})
            return None
        raw = self.rfile.read(length) if length > 0 else b""
        self.close_connection = False  # body 读干净了，连接可以复用
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return None
        if not isinstance(data, dict):
            self._send_json(400, {"error": "请求体必须是 JSON 对象"})
            return None
        return data

    # ---------- cookie 下发 ----------

    def _http_cfg(self) -> dict:
        return self.config.get("http") or {}

    def _cookie_header(self, token: str, max_age: int) -> str:
        prefix = (self._http_cfg().get("url_prefix") or "").rstrip("/")
        parts = [
            f"{COOKIE_NAME}={token}",
            f"Path={prefix}/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={max_age}",
        ]
        if self._http_cfg().get("secure_cookie"):
            parts.append("Secure")
        return "; ".join(parts)

    def _login_cookie(self) -> str:
        days = int(self._http_cfg().get("cookie_days", 365))
        return self._cookie_header(auth.issue_token(days), days * 86400)

    # ---------- GET 路由 ----------

    def _route_get(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            if not auth.is_set_up():
                return self._redirect("./setup")
            if self._authed():
                return self._send_file("index.html")
            return self._send_file("login.html")
        if path == "/setup":
            if auth.is_set_up():
                return self._send_json(403, {"error": "口令已设过，改口令请用命令行 passwd"})
            return self._send_file("setup.html")
        if path in _STATIC_FILES:
            return self._send_file(_STATIC_FILES[path])
        if path == "/api/config":
            if not self._require_auth():
                return
            return self._api_config()
        if path == "/api/tasks":
            if not self._require_auth():
                return
            return self._api_list_tasks()
        if path == "/api/quota":
            if not self._require_auth():
                return
            return self._api_quota()
        match = _RE_TASK_DETAIL.match(path)
        if match:
            if not self._require_auth():
                return
            return self._api_task_detail(match.group(1))
        match = re.match(rf"^/api/tasks/({_TASK_ID_RE})/screen$", path)
        if match:
            if not self._require_auth():
                return
            return self._api_screen(match.group(1))
        self._send_json(404, {"error": "没有这个路径"})

    # ---------- POST / PUT / DELETE 路由 ----------

    def _route_post(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/setup":
            return self._api_setup()
        if path == "/api/login":
            return self._api_login()
        # 其余改状态的请求：先 CSRF 头再登录（两道都过才干活）
        if not self._csrf_ok():
            return self._send_json(403, {"error": f"缺少 {CSRF_HEADER}: {CSRF_VALUE} 头"})
        if not self._require_auth():
            return
        if path == "/api/logout":
            return self._api_logout()
        if path == "/api/preview":
            return self._api_preview()
        if path == "/api/tasks":
            return self._api_create_task()
        match = _RE_TASK_ACTION.match(path)
        if match:
            task_id, action = match.group(1), match.group(2)
            if action == "run-now":
                return self._api_run_now(task_id)
            return self._api_cancel(task_id)
        self._send_json(404, {"error": "没有这个路径"})

    def _route_put(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._csrf_ok():
            return self._send_json(403, {"error": f"缺少 {CSRF_HEADER}: {CSRF_VALUE} 头"})
        if not self._require_auth():
            return
        if path == "/api/templates":
            return self._api_templates()
        self._send_json(404, {"error": "没有这个路径"})

    def _route_delete(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._csrf_ok():
            return self._send_json(403, {"error": f"缺少 {CSRF_HEADER}: {CSRF_VALUE} 头"})
        if not self._require_auth():
            return
        match = _RE_TASK_DELETE.match(path)
        if match:
            return self._api_delete(match.group(1))
        self._send_json(404, {"error": "没有这个路径"})

    # ---------- 登录 / setup / logout ----------

    def _api_setup(self) -> None:
        if auth.is_set_up():
            return self._send_json(403, {"error": "口令已设过，只能设一次；改口令请用命令行 passwd"})
        data = self._read_json()
        if data is None:
            return
        try:
            auth.set_password(data.get("password"))
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        except auth.AlreadySetUp:
            return self._send_json(403, {"error": "口令已设过，只能设一次"})
        return self._send_json(200, {"ok": True}, {"Set-Cookie": self._login_cookie()})

    def _api_login(self) -> None:
        source = self._source()
        if not self.limiter.allowed(source):
            return self._send_json(429, {"error": "失败次数太多，请 15 分钟后再试"})
        data = self._read_json()
        if data is None:
            return
        password = data.get("password")
        if not isinstance(password, str) or not auth.verify_password(password):
            self.limiter.record_failure(source)
            return self._send_json(401, {"error": "口令不对"})
        return self._send_json(200, {"ok": True}, {"Set-Cookie": self._login_cookie()})

    def _api_logout(self) -> None:
        token = self._cookie_token() or ""
        return self._send_json(
            200, {"ok": True}, {"Set-Cookie": self._cookie_header(token, 0)}
        )

    # ---------- 配置 / 模板 / 预览 ----------

    def _api_config(self) -> None:
        cfg = store.load_config()
        models = {
            name: {
                "context_limit": (spec or {}).get("context_limit"),
                "usage_label": (spec or {}).get("usage_label"),
            }
            for name, spec in (cfg.get("models") or {}).items()
        }
        return self._send_json(200, {
            "projects": cfg.get("projects") or {},
            "models": models,
            "efforts": cfg.get("efforts") or [],
            "guards": cfg.get("guards") or {},
            "chain": cfg.get("chain") or {},
            "prompt_template": cfg.get("prompt_template", ""),
            "context_warn_text": cfg.get("context_warn_text", ""),
            "chain_template": cfg.get("chain_template", ""),
            "display_tz_offset_hours": cfg.get("display_tz_offset_hours"),
            # 可选：顶栏"回主站"链接 {"text": "...", "href": "..."}，没配就不显示
            "home_link": (cfg.get("http") or {}).get("home_link"),
        })

    _TEMPLATE_KEYS = ("prompt_template", "context_warn_text", "chain_template")

    def _api_templates(self) -> None:
        data = self._read_json()
        if data is None:
            return
        updates = {k: data[k] for k in self._TEMPLATE_KEYS if k in data}
        if not updates:
            return self._send_json(400, {"error": "没有要改的模板键"})
        for key, value in updates.items():
            if not isinstance(value, str):
                return self._send_json(400, {"error": f"{key} 必须是字符串"})
        cfg = store.load_config()  # 先读全量再改，别的键一个不碰
        cfg.update(updates)
        store.atomic_write_json(store.home() / "config.json", cfg)
        logger.info("模板已更新：%s", "、".join(updates))
        return self._send_json(200, {"ok": True})

    def _api_preview(self) -> None:
        data = self._read_json()
        if data is None:
            return
        title = data.get("title")
        project = data.get("project")
        model = data.get("model")
        task_text = data.get("task_text")
        if not isinstance(title, str) or not isinstance(task_text, str):
            return self._send_json(400, {"error": "title 与 task_text 必须是字符串"})
        cfg = store.load_config()
        if project not in (cfg.get("projects") or {}):
            return self._send_json(400, {"error": f"project 不在 config.projects 里：{project}"})
        prompt = store.build_prompt(cfg, title, str(project), str(model or ""), task_text)
        return self._send_json(200, {"prompt_final": prompt})

    # ---------- 任务 ----------

    def _api_list_tasks(self) -> None:
        items = store.list_tasks()
        for item in items:
            item["events_tail"] = _tail_lines(
                store.task_dir(item["task"]["id"]) / "events.log", 5
            )
        return self._send_json(200, items)

    def _api_task_detail(self, task_id: str) -> None:
        try:
            task = store.load_task(task_id)
        except (OSError, ValueError):
            return self._send_json(404, {"error": "任务不存在"})
        return self._send_json(200, {
            "task": task,
            "status": store.read_status(task_id),
            "events": _tail_lines(store.task_dir(task_id) / "events.log", 50),
        })

    def _api_create_task(self) -> None:
        data = self._read_json()
        if data is None:
            return
        if not data.get("run_at"):
            return self._send_json(400, {"error": "缺少开跑时间 run_at（不给默认时间）"})
        task = {
            key: data.get(key)
            for key in ("title", "project", "model", "effort", "run_at", "task_text", "prompt_final")
        }
        for key in ("guards", "chain"):
            if data.get(key) is not None:
                if not isinstance(data[key], dict):
                    return self._send_json(400, {"error": f"{key} 必须是对象"})
                task[key] = data[key]
        try:
            task_id = store.create_task(task, store.load_config())
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        logger.info("网页建任务：%s（%s）", task_id, task.get("title"))
        return self._send_json(201, {"id": task_id})

    def _load_existing(self, task_id: str) -> dict | None:
        """任务在就返回 task.json 内容，不在就回 404 并返回 None。"""
        try:
            return store.load_task(task_id)
        except (OSError, ValueError):
            self._send_json(404, {"error": "任务不存在"})
            return None

    def _api_run_now(self, task_id: str) -> None:
        if self._load_existing(task_id) is None:
            return
        state = store.read_status(task_id).get("state")
        if state not in _RUN_NOW_STATES:
            return self._send_json(
                409,
                {"error": f"状态 {state or '-'} 不能现在就跑"
                          f"（只允许 {'/'.join(_RUN_NOW_STATES)}）"},
            )
        task = store.load_task(task_id)
        task["run_at"] = store.utc_now_iso()  # 用户动作，允许改 run_at
        store.atomic_write_json(store.task_dir(task_id) / "task.json", task)

        def mut(status: dict) -> None:
            status["state"] = "scheduled"
            status["retries"] = 0
            status["last_event_at"] = store.utc_now_iso()
            status.pop("next_attempt_at", None)
            status.pop("retry_at", None)
            status.pop("error", None)
            status.pop("postpone_reason", None)

        store.modify_status(task_id, mut)
        store.append_event(task_id, "网页：现在就跑（run_at 改为当前，下一轮 tick 走完整预检）")
        logger.info("网页 run-now：%s", task_id)
        return self._send_json(200, {"ok": True})

    def _api_cancel(self, task_id: str) -> None:
        if self._load_existing(task_id) is None:
            return
        state = store.read_status(task_id).get("state")
        if state not in ("scheduled", "postponed"):
            return self._send_json(
                409,
                {"error": f"只有 scheduled/postponed 的任务能取消，当前是 {state or '-'}"},
            )
        store.update_status(task_id, state="cancelled", last_event_at=store.utc_now_iso())
        store.append_event(task_id, "网页：已取消")
        logger.info("网页 cancel：%s", task_id)
        return self._send_json(200, {"ok": True})

    def _api_delete(self, task_id: str) -> None:
        if self._load_existing(task_id) is None:
            return
        state = store.read_status(task_id).get("state")
        if state not in _TERMINAL_STATES:
            return self._send_json(
                409, {"error": f"只允许删除终态任务，当前是 {state or '-'}"}
            )
        shutil.rmtree(store.task_dir(task_id))
        logger.info("网页删除任务：%s", task_id)
        return self._send_json(200, {"ok": True})

    def _api_screen(self, task_id: str) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            lines = int(query.get("lines", ["200"])[0])
        except ValueError:
            return self._send_json(400, {"error": "lines 必须是整数"})
        lines = max(1, min(lines, 2000))
        status = store.read_status(task_id)
        window_id = status.get("window_id")
        if not window_id:
            return self._send_json(404, {"error": "这个任务还没有开过窗口"})
        text = launcher.capture_pane(str(window_id), lines=lines)
        return self._send_json(200, {"text": text})

    def _api_quota(self) -> None:
        path = store.home() / "quota.json"
        if not path.is_file():
            return self._send_json(200, {})
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return self._send_json(200, {})
        if not isinstance(data, dict):
            return self._send_json(200, {})
        out = dict(data)
        out["age_seconds"] = None
        fetched = data.get("fetched_at")
        if isinstance(fetched, str):
            try:
                age = datetime.now(timezone.utc) - scheduler.parse_iso(fetched)
                out["age_seconds"] = int(age.total_seconds())
            except ValueError:
                pass
        return self._send_json(200, out)


def make_server(config: dict) -> ThreadingHTTPServer:
    """按 config.http 起一个 ThreadingHTTPServer（测试用 port 0 拿临时端口）。"""
    http_cfg = config.get("http") or {}
    handler = type(
        "NightshiftHandler",
        (_Handler,),
        {"config": config, "limiter": auth.LoginRateLimiter()},
    )
    server = ThreadingHTTPServer(
        (http_cfg.get("host", "127.0.0.1"), int(http_cfg.get("port", 8190))),
        handler,
    )
    server.daemon_threads = True
    return server


def serve_http(config: dict) -> None:
    """起 HTTP 服务并阻塞（调度循环由调用方在别的线程/进程跑）。"""
    scheduler._setup_logging()  # 访问日志与调度日志共用 scheduler.log
    server = make_server(config)
    http_cfg = config.get("http") or {}
    logger.info(
        "HTTP 服务监听 %s:%s（URL 前缀 %s）",
        http_cfg.get("host", "127.0.0.1"), server.server_address[1],
        http_cfg.get("url_prefix", ""),
    )
    server.serve_forever()
