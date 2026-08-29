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

from . import auth, launcher, quota, scheduler, store, warmup

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
_RE_TASK_MESSAGE = re.compile(rf"^/api/tasks/({_TASK_ID_RE})/message$")
_RE_TASK_SESSION = re.compile(
    rf"^/api/tasks/({_TASK_ID_RE})/(interrupt|stop-background)$"
)
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
    "chained",  # 本班已把活交给后继，自己就是结束了（8/28 工头发现删不掉）
)
# PUT 编辑允许的键，按状态分级（S4②）
# 未跑状态（scheduled/postponed/failed/cancelled）：全字段可改
_EDITABLE_UNRUN = (
    "title", "project", "model", "effort", "run_at",
    "task_text", "prompt_final", "guards", "chain", "trigger",
)
# 活跃状态（launching/working/waiting_background/waiting_wakeup/idle）：
# 只许改标题/任务内容/额度与上下文线/换班设置/触发方式
_EDITABLE_ACTIVE = ("title", "task_text", "guards", "chain", "trigger")
# 编辑直接 409 的终态（failed/cancelled 仍算"未跑"可编辑）
_EDIT_TERMINAL_STATES = (
    "finished", "exited", "chained", "chain_exhausted", "needs_attention",
)
# config 缺 stop_background_text 时的兜底（与 config.example.json 保持一致）
DEFAULT_STOP_BACKGROUND_TEXT = (
    "来自nightshift：请立刻用 TaskStop 停掉所有后台任务和子 agent，"
    "后台起的命令行进程也一并杀掉，然后停下不要继续。"
)


def _trigger_text(task: dict) -> str:
    """给前端的一句话触发说明（卡片上替代"计划 … 还有 …"那行）。"""
    trigger = task.get("trigger") or {}
    if trigger.get("type") != "after":
        return "按时间"
    pre_id = str(trigger.get("task") or "")
    try:
        pre = store.load_task(pre_id)
    except (OSError, ValueError):
        return "前置任务不存在"
    title = pre.get("title") or pre_id
    if trigger.get("when") == "ended":
        return f"等「{title}」结束后"
    return f"等「{title}」完工后"


def _tail_lines(path: Path, count: int) -> list[str]:
    """文本文件末 N 行；没有/读不了返回空表。"""
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()[-count:]
    except OSError:
        return []


def _version_assets(html: bytes) -> bytes:
    """把 html 里的 ./app.js、./style.css 引用改成 ./app.js?v=<mtime>，缓存穿透。"""
    text = html.decode("utf-8", errors="replace")
    for name in ("app.js", "style.css"):
        try:
            stamp = int((WEB_DIR / name).stat().st_mtime)
        except OSError:
            continue
        text = text.replace(f'"./{name}"', f'"./{name}?v={stamp}"')
    return text.encode("utf-8")


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
        if path.suffix == ".html":
            # 资源引用带上版本号（文件 mtime），让 CDN/浏览器缓存的旧 js/css 立刻失效
            data = _version_assets(data)
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        # no-store：Cloudflare 之类的 CDN 会把 no-cache 改写成"浏览器缓存 4 小时"
        # （8/28 真机踩到），no-store 它不碰，浏览器也不留副本
        self.send_header("Cache-Control", "no-store")
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
        if path == "/api/quota/refresh":
            return self._api_quota_refresh()
        match = _RE_TASK_ACTION.match(path)
        if match:
            task_id, action = match.group(1), match.group(2)
            if action == "run-now":
                return self._api_run_now(task_id)
            return self._api_cancel(task_id)
        match = _RE_TASK_MESSAGE.match(path)
        if match:
            return self._api_message(match.group(1))
        match = _RE_TASK_SESSION.match(path)
        if match:
            task_id, action = match.group(1), match.group(2)
            if action == "interrupt":
                return self._api_interrupt(task_id)
            return self._api_stop_background(task_id)
        self._send_json(404, {"error": "没有这个路径"})

    def _route_put(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._csrf_ok():
            return self._send_json(403, {"error": f"缺少 {CSRF_HEADER}: {CSRF_VALUE} 头"})
        if not self._require_auth():
            return
        if path == "/api/templates":
            return self._api_templates()
        if path == "/api/warmup":
            return self._api_warmup()
        match = _RE_TASK_DETAIL.match(path)
        if match:
            return self._api_update_task(match.group(1))
        self._send_json(404, {"error": "没有这个路径"})

    def _route_delete(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._csrf_ok():
            return self._send_json(403, {"error": f"缺少 {CSRF_HEADER}: {CSRF_VALUE} 头"})
        if not self._require_auth():
            return
        match = _RE_TASK_MESSAGE.match(path)
        if match:
            return self._api_delete_message(match.group(1))
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
            "quota_pause_text": cfg.get("quota_pause_text", ""),
            "quota_wrapup_text": cfg.get("quota_wrapup_text", ""),
            "quota_other_model_text": cfg.get("quota_other_model_text", ""),
            "chain_template": cfg.get("chain_template", ""),
            "stop_background_text": cfg.get("stop_background_text", ""),
            "display_tz_offset_hours": cfg.get("display_tz_offset_hours"),
            # 可选：顶栏"回主站"链接 {"text": "...", "href": "..."}，没配就不显示
            "home_link": (cfg.get("http") or {}).get("home_link"),
            "warmup": cfg.get("warmup") or {"enabled": False, "time_local": ""},
            "warmup_state": warmup.read_state(),
        })

    _TEMPLATE_KEYS = ("prompt_template", "context_warn_text", "quota_pause_text", "quota_wrapup_text", "quota_other_model_text", "chain_template", "stop_background_text")

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

    def _api_warmup(self) -> None:
        """预热设置：{enabled: bool, time_local: "HH:MM"}，写回 config.warmup。"""
        data = self._read_json()
        if data is None:
            return
        enabled = bool(data.get("enabled"))
        raw = data.get("times")
        if raw is None:
            raw = data.get("time_local") or ""
        if isinstance(raw, str):
            raw = re.split(r"[\s,，、;；]+", raw.strip())
        times = []
        for t in raw:
            t = str(t).strip()
            if not t:
                continue
            m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", t)
            if not m:
                return self._send_json(400, {"error": f"时间要写成 HH:MM（本地时间），认不出：{t}"})
            times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        times = sorted(set(times))
        if enabled and not times:
            return self._send_json(400, {"error": "开着预热就得给至少一个时刻，如 06:00 18:00"})
        cfg = store.load_config()
        cfg["warmup"] = {**(cfg.get("warmup") or {}), "enabled": enabled, "times": times,
                         "time_local": times[0] if times else ""}
        store.atomic_write_json(store.home() / "config.json", cfg)
        logger.info("预热设置已更新：enabled=%s times=%s", enabled, times)
        return self._send_json(200, {"ok": True, "warmup": cfg["warmup"]})

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
            task_id = item["task"]["id"]
            item["events_tail"] = _tail_lines(store.task_dir(task_id) / "events.log", 5)
            item["trigger_text"] = _trigger_text(item["task"])
            item["draft"] = self._read_draft(task_id)
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
        # after 触发的任务可以不给 run_at（create_task 补创建时刻，只当排序用）
        trigger = data.get("trigger") or {}
        if not data.get("run_at") and trigger.get("type") != "after":
            return self._send_json(400, {"error": "缺少开跑时间 run_at（不给默认时间）"})
        task = {
            key: data.get(key)
            for key in ("title", "project", "model", "effort", "run_at", "task_text", "prompt_final")
        }
        for key in ("guards", "chain", "trigger"):
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

    def _api_update_task(self, task_id: str) -> None:
        """编辑任务（S4②）：按状态分级——未跑全字段、活跃只四个维度、终态 409。"""
        if self._load_existing(task_id) is None:
            return
        state = store.read_status(task_id).get("state")
        if state in _EDIT_TERMINAL_STATES:
            return self._send_json(409, {"error": f"任务已结束（{state}），不能再改"})
        unrun = state in _RUN_NOW_STATES
        allowed = _EDITABLE_UNRUN if unrun else _EDITABLE_ACTIVE
        data = self._read_json()
        if data is None:
            return
        bad = sorted(k for k in data if k not in allowed)
        if bad:
            if unrun:
                msg = f"不能改这些字段：{'、'.join(bad)}"
            else:
                msg = "这一班正在跑，只能改标题/任务内容/额度与上下文线/换班设置"
            return self._send_json(400, {"error": msg})
        task = store.load_task(task_id)
        task.update(data)
        try:
            store.validate_task(task, store.load_config(), task_id=task_id)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        store.atomic_write_json(store.task_dir(task_id) / "task.json", task)
        if state == "postponed":
            # 改完回到 scheduled，旧的"下次尝试"与推迟原因作废
            def mut(status: dict) -> None:
                status["state"] = "scheduled"
                status["last_event_at"] = store.utc_now_iso()
                status.pop("next_attempt_at", None)
                status.pop("postpone_reason", None)
                status.pop("error", None)

            store.modify_status(task_id, mut)
        if "trigger" in data:
            # 换了前置/条件：老的满足时刻与提醒标记作废，重新按新 trigger 判
            def mut_trigger(status: dict) -> None:
                status.pop("trigger_met_at", None)
                status.pop("attention_noted", None)

            store.modify_status(task_id, mut_trigger)
        keys = "、".join(sorted(data.keys()))
        store.append_event(task_id, f"网页编辑：改了 {keys}")
        logger.info("网页编辑任务：%s（%s）", task_id, keys)
        return self._send_json(200, {"ok": True})

    def _read_draft(self, task_id: str) -> str | None:
        """读捎话草稿；没有/读不了返回 None。"""
        path = store.task_dir(task_id) / "draft.txt"
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _api_message(self, task_id: str) -> None:
        """捎话（S4②）：send=false 存草稿；send=true 敲进会话窗口并清草稿。"""
        if self._load_existing(task_id) is None:
            return
        data = self._read_json()
        if data is None:
            return
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._send_json(400, {"error": "text 必须是非空字符串"})
        if not data.get("send"):
            store.atomic_write_text(store.task_dir(task_id) / "draft.txt", text)
            logger.info("网页捎话存草稿：%s", task_id)
            return self._send_json(200, {"saved": True})
        # S4.1：账面之外还要现查 tmux；窗口已消失 → 409，
        # 不 send_keys、不删草稿、不记"已发出"
        window_id = self._require_live_window(
            task_id, error="会话没在跑，发不了；可以先存草稿"
        )
        if window_id is None:
            return
        # 多行折成单行：tmux send-keys 一次敲进去，换行会变成提前回车
        single_line = " ".join(text.splitlines()) or text
        launcher.send_keys(str(window_id), single_line)
        (store.task_dir(task_id) / "draft.txt").unlink(missing_ok=True)
        # S4.1：事件日志记折行后的单行文本，多行捎话不再把 events.log 拆成多行
        store.append_event(task_id, f"捎话：{single_line[:80]}")
        logger.info("网页捎话已发送：%s", task_id)
        return self._send_json(200, {"sent": True})

    def _api_delete_message(self, task_id: str) -> None:
        if self._load_existing(task_id) is None:
            return
        (store.task_dir(task_id) / "draft.txt").unlink(missing_ok=True)
        logger.info("网页删捎话草稿：%s", task_id)
        return self._send_json(200, {"ok": True})

    def _require_live_window(
        self, task_id: str, error: str = "会话没在跑（没有活着的窗口）"
    ) -> str | None:
        """会话真活着才返回 window_id；否则回 409 并返回 None。

        S4.1：账面（window_id 在 + session_ended_at 空）只说明"上次见过这个
        窗口"；窗口刚死、SessionEnd hook 还没报到的瞬间账面还是旧的——所以
        动手前必须用 launcher.window_alive 现查 tmux，查不到一律 409，
        调用方不许敲键、不删草稿、不记"已发出"。
        """
        status = store.read_status(task_id)
        window_id = status.get("window_id")
        if not window_id or status.get("session_ended_at"):
            self._send_json(409, {"error": error})
            return None
        if not launcher.window_alive(str(window_id), store.load_config()):
            self._send_json(409, {"error": error})
            return None
        return str(window_id)

    def _api_interrupt(self, task_id: str) -> None:
        """中止（S4②）：往窗口按一下 Esc。不改 state——hook 会自己报 Stop。"""
        if self._load_existing(task_id) is None:
            return
        window_id = self._require_live_window(task_id)
        if window_id is None:
            return
        launcher.send_escape(window_id)
        store.append_event(task_id, "中止：Esc")
        logger.info("网页中止：%s", task_id)
        return self._send_json(200, {"ok": True})

    def _api_stop_background(self, task_id: str) -> None:
        """停后台（S4②）：把 config.stop_background_text 敲进会话窗口。"""
        if self._load_existing(task_id) is None:
            return
        window_id = self._require_live_window(task_id)
        if window_id is None:
            return
        text = (
            store.load_config().get("stop_background_text")
            or DEFAULT_STOP_BACKGROUND_TEXT
        )
        launcher.send_keys(window_id, text)
        store.append_event(task_id, "停后台：已敲入停后台文案，让它自己清后台")
        logger.info("网页停后台：%s", task_id)
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

    def _api_quota_refresh(self) -> None:
        """用户手动现查一次 /usage（约 10 秒、一次 haiku 无头调用），写 quota.json 后原样返回。"""
        cfg = store.load_config()
        try:
            usage = quota.fetch_usage(cfg)
        except (quota.UsageUnavailable, quota.UsageParseError) as exc:
            logger.warning("手动查额度失败：%s", exc)
            return self._send_json(502, {"error": f"额度查不到：{str(exc)[:200]}"})
        store.atomic_write_json(
            store.home() / "quota.json",
            {"usage": usage, "fetched_at": store.utc_now_iso()},
        )
        logger.info("网页手动刷新额度")
        return self._api_quota()

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
