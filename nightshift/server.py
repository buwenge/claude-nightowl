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

from . import auth, background_runner, launcher, quota, scheduler, store, warmup, worktree

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
_RE_TASK_ACTION = re.compile(
    rf"^/api/tasks/({_TASK_ID_RE})/(run-now|cancel|merge|discard)$"
)
# S7④：流水线控制 action，接受这条流水线任一成员的 task id
_RE_PIPELINE_ACTION = re.compile(
    rf"^/api/tasks/({_TASK_ID_RE})/(hold|continue|keepalive|review-now|skip-review|fix-now)$"
)
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
# 可删除的终态（S5②：merged / discarded 加入；有树的删除被 409 挡住）
_TERMINAL_STATES = (
    "exited", "finished", "failed", "cancelled", "chain_exhausted", "needs_attention",
    "chained",  # 本班已把活交给后继，自己就是结束了（8/28 工头发现删不掉）
    "merged", "discarded",
)
# PUT 编辑允许的键，按状态分级（S4②）
# 未跑状态（scheduled/postponed/failed/cancelled）：全字段可改
_EDITABLE_UNRUN = (
    "title", "project", "runner", "model", "effort", "run_at",
    "task_text", "prompt_final", "guards", "chain", "trigger",
    "worktree", "review",  # S5：没起跑还能改工作树/审稿占位形状
)
# 活跃状态（launching/working/waiting_background/waiting_wakeup/idle）：
# 只许改标题/任务内容/额度与上下文线/换班设置/触发方式
_EDITABLE_ACTIVE = ("title", "task_text", "guards", "chain", "trigger")
# 编辑直接 409 的终态（failed/cancelled 仍算"未跑"可编辑；S5②：
# awaiting_merge / merged / discarded 也定死，不许再改）
_EDIT_TERMINAL_STATES = (
    "finished", "exited", "chained", "chain_exhausted", "needs_attention",
    "awaiting_merge", "merged", "discarded",
)
# 能按"合并进主线"的状态：等合并，或 needs_attention（工头清完主线后重试）
_MERGE_STATES = ("awaiting_merge", "needs_attention")
# 能按"丢弃"的状态：有树且不会再自动跑（活跃班正在树里施工，不许拆脚手架）
_DISCARD_STATES = (
    "awaiting_merge", "needs_attention", "failed", "cancelled",
    "chain_exhausted", "exited",
)
# config 缺 stop_background_text 时的兜底（与 config.example.json 保持一致）
DEFAULT_STOP_BACKGROUND_TEXT = (
    "来自nightshift：请立刻用 TaskStop 停掉所有后台任务和子 agent，"
    "后台起的命令行进程也一并杀掉，然后停下不要继续。"
)
# S6④：Codex 没有 TaskStop，改成让它自己调用 background_runner 的 list/stop
DEFAULT_CODEX_STOP_BACKGROUND_TEXT = (
    "来自nightshift：请用 `python3 -m nightshift.background_runner list` "
    "看一下登记在这个任务下的后台进程，然后逐个用 "
    "`python3 -m nightshift.background_runner stop <background_id>` 停掉，然后停下不要继续。"
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


def _background_summary(task_id: str) -> dict:
    """S6④：F12 后台项摘要——运行中 / 已完成待通知的数量，给卡片一眼看的数字。

    S6.1 A3：finished_pending 也要算上 stopped——stop 请求处理完之后落的是
    state=stopped，不是 finished，只数 finished 会让"停后台"操作完之后卡片
    上的数字看起来像没有任何待处理项，跟 scheduler 那边"stopped 也要通知"
    的口径对不上。
    """
    registry = background_runner.load_registry(task_id)
    running = sum(1 for r in registry.values() if r.get("state") == "running")
    finished_pending = sum(
        1 for r in registry.values()
        if r.get("state") in ("finished", "stopped") and r.get("notification_state") != "notified"
    )
    return {"running": running, "finished_pending": finished_pending}


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
        if path == "/api/worktrees":
            if not self._require_auth():
                return
            return self._api_worktrees()
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
            if action == "cancel":
                return self._api_cancel(task_id)
            if action == "merge":
                return self._api_merge(task_id)
            return self._api_discard(task_id)
        match = _RE_TASK_MESSAGE.match(path)
        if match:
            return self._api_message(match.group(1))
        match = _RE_TASK_SESSION.match(path)
        if match:
            task_id, action = match.group(1), match.group(2)
            if action == "interrupt":
                return self._api_interrupt(task_id)
            return self._api_stop_background(task_id)
        match = _RE_PIPELINE_ACTION.match(path)
        if match:
            task_id, action = match.group(1), match.group(2)
            if action == "hold":
                return self._api_pipeline_hold(task_id)
            if action == "continue":
                return self._api_pipeline_continue(task_id)
            if action == "keepalive":
                return self._api_pipeline_keepalive(task_id)
            if action == "review-now":
                return self._api_pipeline_review_now(task_id)
            if action == "skip-review":
                return self._api_pipeline_skip_review(task_id)
            return self._api_pipeline_fix_now(task_id)
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
        runners = {
            name: {
                "models": {
                    mname: {"context_limit": (mspec or {}).get("context_limit")}
                    for mname, mspec in (rc.get("models") or {}).items()
                },
                "efforts": rc.get("efforts") or [],
            }
            for name, rc in store.runner_config(cfg).items()
        }
        return self._send_json(200, {
            "projects": cfg.get("projects") or {},
            "models": models,
            "efforts": cfg.get("efforts") or [],
            # S6：按 runner 分家的模型/档位，前端"用谁施工"下拉据此换选项；
            # 旧版前端不认这个键，忽略即可，不影响上面两个兼容字段
            "runners": runners,
            "guards": cfg.get("guards") or {},
            "chain": cfg.get("chain") or {},
            "prompt_template": cfg.get("prompt_template", ""),
            "context_warn_text": cfg.get("context_warn_text", ""),
            "quota_pause_text": cfg.get("quota_pause_text", ""),
            "quota_wrapup_text": cfg.get("quota_wrapup_text", ""),
            "quota_other_model_text": cfg.get("quota_other_model_text", ""),
            "chain_template": cfg.get("chain_template", ""),
            "stop_background_text": cfg.get("stop_background_text", ""),
            "stuck_interrupt_text": cfg.get("stuck_interrupt_text", ""),
            "codex_quota_pause_text": cfg.get("codex_quota_pause_text", ""),
            "codex_resume_text": cfg.get("codex_resume_text", ""),
            "codex_stop_background_text": cfg.get("codex_stop_background_text", ""),
            # S7①：审稿流水线的七个模板键，生产 config 缺键时展示 fallback
            "review_template": cfg.get("review_template", ""),
            "review_fix_template": cfg.get("review_fix_template", ""),
            "review_criteria_text": cfg.get("review_criteria_text", ""),
            "review_wrapup_text": cfg.get("review_wrapup_text", ""),
            "review_stop_build_text": cfg.get("review_stop_build_text", ""),
            "hold_text": cfg.get("hold_text", ""),
            "resume_text": cfg.get("resume_text", ""),
            # S7①：config.review 的默认值（max_rounds/on_no_quota/merge_policy），
            # 新建页折叠区要展示；缺整个对象时走代码 fallback（跟 store.review_config 一致）
            "review_defaults": {
                "max_rounds": (cfg.get("review") or {}).get("max_rounds", 5),
                "on_no_quota": (cfg.get("review") or {}).get("on_no_quota", "release"),
                "merge_policy": (cfg.get("review") or {}).get("merge_policy", "manual"),
            },
            "display_tz_offset_hours": cfg.get("display_tz_offset_hours"),
            # 可选：顶栏"回主站"链接 {"text": "...", "href": "..."}，没配就不显示
            "home_link": (cfg.get("http") or {}).get("home_link"),
            "warmup": cfg.get("warmup") or {"enabled": False, "time_local": ""},
            "warmup_state": warmup.read_state(),
        })

    _TEMPLATE_KEYS = (
        "prompt_template", "context_warn_text", "quota_pause_text", "quota_wrapup_text",
        "quota_other_model_text", "chain_template", "stop_background_text", "stuck_interrupt_text",
        "codex_quota_pause_text", "codex_resume_text", "codex_stop_background_text",
        "review_template", "review_fix_template", "review_criteria_text",
        "review_wrapup_text", "review_stop_build_text", "hold_text", "resume_text",
    )

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
        runner = data.get("runner") if data.get("runner") in store.RUNNERS else "claude"
        prompt = store.build_prompt(
            cfg, title, str(project), str(model or ""), task_text,
            worktree=bool(data.get("worktree")), runner=runner,
        )
        return self._send_json(200, {"prompt_final": prompt})

    # ---------- 任务 ----------

    def _api_list_tasks(self) -> None:
        items = store.list_tasks()
        for item in items:
            task_id = item["task"]["id"]
            item["task"].setdefault("runner", "claude")  # 仅展示；不回写 task.json
            item["events_tail"] = _tail_lines(store.task_dir(task_id) / "events.log", 5)
            item["trigger_text"] = _trigger_text(item["task"])
            item["draft"] = self._read_draft(task_id)
            if item["task"]["runner"] == "codex":
                item["background_summary"] = _background_summary(task_id)
        return self._send_json(200, items)

    def _api_worktrees(self) -> None:
        """孤儿工作树列表（S5 只读：只提示，绝不自动删）。"""
        path = store.home() / "orphan_worktrees.json"
        orphans: list = []
        if path.is_file():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    orphans = data
            except (OSError, ValueError):
                orphans = []
        return self._send_json(200, orphans)

    def _api_task_detail(self, task_id: str) -> None:
        try:
            task = store.load_task(task_id)
        except (OSError, ValueError):
            return self._send_json(404, {"error": "任务不存在"})
        task.setdefault("runner", "claude")  # 仅展示；不回写 task.json
        out = {
            "task": task,
            "status": store.read_status(task_id),
            "events": _tail_lines(store.task_dir(task_id) / "events.log", 50),
        }
        if task["runner"] == "codex":
            out["background_summary"] = _background_summary(task_id)
            # S6.1 B1：不把整份 registry 原样透传出去——里面的 argv_summary/
            # output_tail/result_path/sandbox_pid 都可能带敏感内容，前端根本
            # 没用到这份明细，只需要 background_summary 那两个数字。
        return self._send_json(200, out)

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
        if data.get("runner") is not None:
            task["runner"] = data["runner"]
        for key in ("guards", "chain", "trigger", "review"):
            if data.get(key) is not None:
                if not isinstance(data[key], dict):
                    return self._send_json(400, {"error": f"{key} 必须是对象"})
                task[key] = data[key]
        if data.get("worktree") is not None:
            if not isinstance(data["worktree"], bool):
                return self._send_json(400, {"error": "worktree 必须是布尔值"})
            task["worktree"] = data["worktree"]
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
        current_status = store.read_status(task_id)
        if current_status.get("worktree_path"):
            if "project" in data and data["project"] != task.get("project"):
                return self._send_json(
                    409, {"error": "工作树已经建好，不能再换项目；请先合并或丢弃"}
                )
            if "worktree" in data and data["worktree"] is not True:
                return self._send_json(
                    409, {"error": "工作树已经建好，不能切回老式模式；请先合并或丢弃"}
                )
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
        """停后台（S4②，S6④ 按 runner 分文案）：Claude 用 config.stop_background_text
        （TaskStop）；Codex 改成明确调用 background_runner 的 list/stop 能力——
        Codex 没有 TaskStop 这个概念，裸敲同一句 Claude 文案它根本听不懂。"""
        task = self._load_existing(task_id)
        if task is None:
            return
        window_id = self._require_live_window(task_id)
        if window_id is None:
            return
        cfg = store.load_config()
        if (task.get("runner") or "claude") == "codex":
            text = cfg.get("codex_stop_background_text") or DEFAULT_CODEX_STOP_BACKGROUND_TEXT
        else:
            text = cfg.get("stop_background_text") or DEFAULT_STOP_BACKGROUND_TEXT
        launcher.send_keys(window_id, text)
        store.append_event(task_id, "停后台：已敲入停后台文案，让它自己清后台")
        logger.info("网页停后台：%s", task_id)
        return self._send_json(200, {"ok": True})

    def _api_merge(self, task_id: str) -> None:
        """合并进主线（S5②）：与 auto 收工共用 worktree.merge_task 同一套安全检查。"""
        if self._load_existing(task_id) is None:
            return
        status = store.read_status(task_id)
        state = status.get("state")
        if state not in _MERGE_STATES:
            return self._send_json(
                409,
                {"error": f"状态 {state or '-'} 不能合并"
                          f"（只允许 {'/'.join(_MERGE_STATES)}）"},
            )
        task = store.load_task(task_id)
        if not store.worktree_enabled(task):
            return self._send_json(409, {"error": "老式任务没有工作树，不需要合并"})
        cfg = store.load_config()
        project_path = (cfg.get("projects") or {}).get(task.get("project"))
        if not project_path:
            return self._send_json(409, {"error": "任务的项目已不在配置里，不能合并"})
        ok, note = worktree.merge_task(
            task, project_path, status, cfg,
            close_windows=lambda ids: launcher.close_windows(ids, cfg),
        )
        if ok:
            logger.info("网页合并进主线：%s（%s）", task_id, note)
            return self._send_json(200, {"ok": True, "note": note})
        # merge_task 已把状态落 needs_attention、原因写进 error；红字会留在卡片上
        return self._send_json(409, {"error": note})

    def _api_discard(self, task_id: str) -> None:
        """丢弃（S5②，破坏性）：树与 ns 分支删除，只按记录双核验后动手。"""
        if self._load_existing(task_id) is None:
            return
        status = store.read_status(task_id)
        state = status.get("state")
        if state not in _DISCARD_STATES:
            return self._send_json(
                409,
                {"error": f"状态 {state or '-'} 不能丢弃"
                          f"（只允许 {'/'.join(_DISCARD_STATES)}）"},
            )
        task = store.load_task(task_id)
        if not store.worktree_enabled(task):
            return self._send_json(409, {"error": "老式任务没有工作树，无从丢弃"})
        cfg = store.load_config()
        project_path = (cfg.get("projects") or {}).get(task.get("project"))
        if not project_path:
            return self._send_json(409, {"error": "任务的项目已不在配置里，不能丢弃"})
        ok, note = worktree.discard_task(
            task, project_path, status, cfg,
            close_windows=lambda ids: launcher.close_windows(ids, cfg),
        )
        if ok:
            logger.info("网页丢弃工作树：%s（%s）", task_id, note)
            return self._send_json(200, {"ok": True, "note": note})
        return self._send_json(409, {"error": note})

    # ---------- S7④：流水线控制（我来看/继续/保活/现在就审/跳过审稿/直接返工） ----------

    def _resolve_pipeline(self, task_id: str) -> str | None:
        """流水线任一成员 task id → coordinator id（pipeline_id）；任务不
        存在发 404 并返回 None。六个控制 action 共用这一步解析。"""
        try:
            task = store.load_task(task_id)
        except (OSError, ValueError):
            self._send_json(404, {"error": "任务不存在"})
            return None
        return store.pipeline_id_of(task)

    def _pipeline_members(self, pipeline_id: str) -> list[dict]:
        return [
            item for item in store.list_tasks()
            if store.pipeline_id_of(item["task"]) == pipeline_id
        ]

    def _api_pipeline_hold(self, task_id: str) -> None:
        """我来看：幂等设置 pipeline hold，向当前活窗口各敲一次 hold_text。"""
        pipeline_id = self._resolve_pipeline(task_id)
        if pipeline_id is None:
            return
        coordinator = store.read_status(pipeline_id)
        cfg = store.load_config()
        if not coordinator.get("hold_requested"):
            store.update_status(pipeline_id, hold_requested=True)
            text = cfg.get("hold_text") or "来自nightshift：工头要来看，停在这里别再动代码；有人问再答。"
            pinged = []
            for item in self._pipeline_members(pipeline_id):
                t, s = item["task"], item["status"] or {}
                wid = s.get("window_id")
                if wid and launcher.window_alive(str(wid), cfg):
                    if store.role_of(t) == "review":
                        # S7.1 阻断二：hold_text 不要求正式 verdict，发之前
                        # 先落 review_awaiting_verdict=False，接下来的 Stop
                        # 按控制 turn 处理，不会被误记成协议缺失→fix。
                        store.update_status(t["id"], review_awaiting_verdict=False)
                    launcher.send_keys(str(wid), text)
                    pinged.append(str(wid))
            store.append_event(
                pipeline_id,
                f"我来看：已请求，敲了 {len(pinged)} 个活窗口" if pinged
                else "我来看：已请求，当前没有活窗口",
            )
        logger.info("网页我来看：%s", pipeline_id)
        return self._send_json(200, {"ok": True, "hold_requested": True})

    def _api_pipeline_continue(self, task_id: str) -> None:
        """继续：清 hold_requested 并执行一个 resume_action——要么是从
        "我来看"恢复（重新评估被拦下的那一班），要么是从返工轮数上限恢复
        （放行一轮，不永久取消上限）。两者都找不到就 409。"""
        pipeline_id = self._resolve_pipeline(task_id)
        if pipeline_id is None:
            return
        coordinator = store.read_status(pipeline_id)
        if coordinator.get("hold_requested"):
            store.update_status(pipeline_id, hold_requested=False)
            blocked = None
            for item in self._pipeline_members(pipeline_id):
                t, s = item["task"], item["status"]
                if s.get("state") == "held" and "工头" in (s.get("held_reason") or ""):
                    blocked = (t, s)
            if blocked is None:
                store.append_event(pipeline_id, "继续：已清\"我来看\"请求")
                logger.info("网页继续（我来看）：%s（无阻塞班）", pipeline_id)
                return self._send_json(200, {"ok": True, "resumed": False})
            t, _ = blocked
            if store.role_of(t) == "build":
                store.update_status(t["id"], state="idle", chain_checked=False)
            else:
                store.update_status(t["id"], state="idle")
            store.append_event(t["id"], "继续：清\"我来看\"请求，重新评估这一班的下一步")
            logger.info("网页继续（我来看）：%s → %s", pipeline_id, t["id"])
            return self._send_json(200, {"ok": True, "resumed": True, "task_id": t["id"]})

        if coordinator.get("pipeline_phase") == "round_limit":
            blocked = None
            for item in self._pipeline_members(pipeline_id):
                t, s = item["task"], item["status"]
                if s.get("state") == "needs_attention" and "到线" in (s.get("error") or ""):
                    blocked = (t, s)
            if blocked is None:
                return self._send_json(409, {"error": "没有找到卡在返工轮数上限的审稿班"})
            t, _ = blocked
            store.update_status(pipeline_id, round_limit_override=True)
            store.update_status(t["id"], state="idle")
            store.append_event(t["id"], "继续：放行一轮返工上限（不永久取消上限）")
            logger.info("网页继续（返工上限）：%s → %s", pipeline_id, t["id"])
            return self._send_json(200, {"ok": True, "resumed": True, "task_id": t["id"]})

        return self._send_json(409, {"error": "当前没有等你\"继续\"的动作"})

    def _api_pipeline_keepalive(self, task_id: str) -> None:
        """暂停/恢复保活：只改运行期暂停位（status.keepalive_paused），不
        碰 held/reviewing/waiting_wakeup 等流程状态。"""
        pipeline_id = self._resolve_pipeline(task_id)
        if pipeline_id is None:
            return
        data = self._read_json()
        if data is None:
            return
        paused = data.get("paused")
        if not isinstance(paused, bool):
            return self._send_json(400, {"error": "paused 必须是布尔值"})
        target = None
        for item in self._pipeline_members(pipeline_id):
            t, s = item["task"], item["status"]
            if s.get("state") in ("held", "waiting_background"):
                target = t["id"]
        if target is None:
            return self._send_json(409, {"error": "当前没有在等保活的班"})
        store.update_status(target, keepalive_paused=paused)
        store.append_event(target, f"保活{'暂停' if paused else '恢复'}（网页按钮）")
        logger.info("网页%s保活：%s", "暂停" if paused else "恢复", target)
        return self._send_json(200, {"ok": True, "keepalive_paused": paused})

    def _api_pipeline_review_now(self, task_id: str) -> None:
        """现在就审：只对因审稿方额度被推迟（postponed）的审稿班跳过等待
        时间，仍走完整的额度预检（下一 tick 的 _try_launch 照常判），不是
        绕过额度守卫。"""
        pipeline_id = self._resolve_pipeline(task_id)
        if pipeline_id is None:
            return
        target = None
        for item in self._pipeline_members(pipeline_id):
            t, s = item["task"], item["status"]
            if store.role_of(t) == "review" and s.get("state") == "postponed":
                target = t["id"]
        if target is None:
            return self._send_json(409, {"error": "没有正在等额度的审稿班"})

        def mut(status: dict) -> None:
            status["next_attempt_at"] = store.utc_now_iso()

        store.modify_status(target, mut)
        store.append_event(target, "网页：现在就审（跳过等待，仍要过完整额度预检）")
        logger.info("网页现在就审：%s → %s", pipeline_id, target)
        return self._send_json(200, {"ok": True, "task_id": target})

    def _api_pipeline_skip_review(self, task_id: str) -> None:
        """跳过审稿：直接收工按 merge policy 分流；只在 build 已 checkpoint、
        尚无 done verdict 的边界允许（held 等审稿、审稿还没起跑/还在等额度）。
        """
        pipeline_id = self._resolve_pipeline(task_id)
        if pipeline_id is None:
            return
        build_item = None
        pending_review = None
        for item in self._pipeline_members(pipeline_id):
            t, s = item["task"], item["status"]
            if (
                store.role_of(t) == "build" and s.get("state") == "held"
                and s.get("checkpoint_done")
            ):
                build_item = (t, s)
            if store.role_of(t) == "review" and s.get("state") in ("scheduled", "postponed"):
                pending_review = (t, s)
        if build_item is None:
            return self._send_json(
                409, {"error": "没有已存档、等审稿的施工班可以跳过审稿"}
            )
        t, _ = build_item
        if pending_review is not None:
            rt, _ = pending_review
            store.update_status(rt["id"], state="cancelled", last_event_at=store.utc_now_iso())
            store.append_event(rt["id"], "网页：跳过审稿，本班取消")
        cfg = store.load_config()
        scheduler._finalize_done(
            t, cfg, datetime.now(timezone.utc), skip_review=True
        )
        store.append_event(t["id"], "网页：跳过审稿，直接按 merge policy 收工")
        logger.info("网页跳过审稿：%s → %s", pipeline_id, t["id"])
        return self._send_json(200, {"ok": True, "task_id": t["id"]})

    def _api_pipeline_fix_now(self, task_id: str) -> None:
        """直接返工：不等审稿，带用户给的非空 instruction（或复用这一轮已有
        的审稿意见）直接进入下一轮 build。仍受 max_rounds/单轮放行与"一个
        working"的约束。"""
        pipeline_id = self._resolve_pipeline(task_id)
        if pipeline_id is None:
            return
        data = self._read_json()
        if data is None:
            return
        instruction = data.get("instruction")
        if instruction is not None and not isinstance(instruction, str):
            return self._send_json(400, {"error": "instruction 必须是字符串"})

        members = self._pipeline_members(pipeline_id)
        for item in members:
            if (item["status"] or {}).get("state") == "working":
                return self._send_json(
                    409, {"error": "这条流水线正有一班在跑，不能现在插入返工"}
                )

        review_item = None
        for item in members:
            t, s = item["task"], item["status"]
            if store.role_of(t) != "review":
                continue
            if review_item is None or store.round_of(t) >= store.round_of(review_item[0]):
                review_item = (t, s)
        if review_item is None:
            return self._send_json(409, {"error": "这条流水线还没有审稿班，用不了直接返工"})
        rt, rs = review_item
        if rs.get("state") not in ("scheduled", "postponed", "idle", "held"):
            return self._send_json(
                409, {"error": f"审稿班当前状态 {rs.get('state')} 不能直接返工"}
            )

        text = (instruction or "").strip()
        if not text:
            latest = rs.get("review_file")
            if latest and Path(latest).is_file():
                text = Path(latest).read_text(encoding="utf-8", errors="replace")
        if not text:
            return self._send_json(
                400, {"error": "instruction 不能为空，且这一轮也没有可用的审稿意见"}
            )

        cfg = store.load_config()
        coordinator = store.read_status(pipeline_id)
        review_cfg = store.review_config(rt, cfg)
        max_rounds = int(review_cfg.get("max_rounds") or 5)
        fix_count = int(coordinator.get("fix_count") or 0)
        if fix_count >= max_rounds and not coordinator.get("round_limit_override"):
            return self._send_json(
                409,
                {"error": f"返工轮数已到线（{fix_count}/{max_rounds}），"
                          "请先在网页点\"继续\"放行一轮"},
            )

        round_ = store.round_of(rt)
        review_file = store.task_dir(rt["id"]) / f"review-{round_}.md"
        store.atomic_write_text(review_file, text)
        store.update_status(
            rt["id"], state="idle", review_verdict="fix", review_file=str(review_file),
            review_recorded_round=round_,
        )
        store.append_event(rt["id"], "网页：不等审稿，直接按给定意见返工（fix-now）")
        actions = scheduler._review_fix(store.load_task(rt["id"]), cfg, datetime.now(timezone.utc))
        logger.info("网页直接返工：%s → %s", pipeline_id, rt["id"])
        return self._send_json(200, {"ok": True, "task_id": rt["id"], "actions": actions})

    def _api_delete(self, task_id: str) -> None:
        if self._load_existing(task_id) is None:
            return
        status = store.read_status(task_id)
        state = status.get("state")
        # S5②：还占着工作树的任务不许直接删任务目录，避免人为制造孤儿
        # （这条先于终态判断：awaiting_merge 也不是终态，但提示该指向树）
        if status.get("worktree_path"):
            return self._send_json(
                409, {"error": "这条任务的工作树还没处理：先合并进主线或丢弃，再删任务"}
            )
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
        """用户手动现查一次额度，写 quota.json 对应分片后原样返回整份。

        `?runner=claude|codex` 只刷那一家（保留一期"单家失败就 502"的直给
        契约，方便前端单独重试）；不给 runner 就两家都刷——各自独立，一家
        失败绝不清空/覆盖另一家的好数据，失败原因进返回体的 `errors`。
        """
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        runner = (query.get("runner") or [None])[0]
        if runner is not None and runner not in store.RUNNERS:
            return self._send_json(
                400, {"error": f"runner 只认 {'/'.join(store.RUNNERS)}：{runner}"}
            )
        targets = [runner] if runner else list(store.RUNNERS)
        cfg = store.load_config()
        errors: dict[str, str] = {}
        for r in targets:
            try:
                usage = (
                    quota.fetch_usage_codex(cfg) if r == "codex"
                    else quota.fetch_usage_claude(cfg)
                )
            except (quota.UsageUnavailable, quota.UsageParseError) as exc:
                logger.warning("手动查额度失败（%s）：%s", r, exc)
                quota.write_quota_runner(
                    r, {"usage": None, "fetched_at": store.utc_now_iso(), "error": str(exc)}
                )
                errors[r] = str(exc)[:200]
                continue
            quota.write_quota_runner(
                r, {"usage": usage, "fetched_at": store.utc_now_iso(), "error": None}
            )
        logger.info("网页手动刷新额度：%s", "、".join(targets))
        if runner and runner in errors:
            # 明确只刷一家且失败：保留一期契约，502 让前端能单独画红/重试
            return self._send_json(502, {"error": f"额度查不到：{errors[runner]}"})
        return self._api_quota(errors=errors)

    def _api_quota(self, errors: dict[str, str] | None = None) -> None:
        """两家各一份：{"claude": {...}, "codex": {...}}，各自
        usage/fetched_at/error/age_seconds。一期旧 quota.json（整份就是
        claude 那份）由 quota.load_quota_file 兼容读出，这里不用再管。"""
        data = quota.load_quota_file()
        now = datetime.now(timezone.utc)
        out: dict = {}
        for runner in store.RUNNERS:
            entry = dict(data.get(runner) or {})
            entry.setdefault("usage", None)
            entry.setdefault("fetched_at", None)
            entry.setdefault("error", None)
            entry["age_seconds"] = None
            fetched = entry.get("fetched_at")
            if isinstance(fetched, str):
                try:
                    age = now - scheduler.parse_iso(fetched)
                    entry["age_seconds"] = int(age.total_seconds())
                except ValueError:
                    pass
            out[runner] = entry
        if errors:
            out["errors"] = errors
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
