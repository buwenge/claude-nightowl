"""口令与登录会话：pbkdf2 口令散列、HMAC 签名的过期 token、登录失败限速。

凭据放数据目录 auth.json（0600），绝不进 config.json：
    {"salt": hex, "password_hash": hex, "token_secret": hex,
     "token_generation": int, "created_at": iso}
token 形如 "<exp_unix>.<token_generation>.<hmac_sha256(token_secret, exp, generation)>"，
过期即失效；改口令（passwd）会换新 token_secret，旧登录全部失效。

总review二 G7：token 本身是无状态的，一年有效期内单靠过期时间撤不掉——
`token_generation` 签进 token payload，logout/passwd 都会让它 +1；校验时
代次跟 auth.json 里当前的不一致就判无效，不用等它自然过期。单用户系统，
"退出登录"就是全设备退出，工头已接受这个语义。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time

from . import store

__all__ = [
    "AlreadySetUp",
    "LoginRateLimiter",
    "bump_token_generation",
    "issue_token",
    "is_set_up",
    "reset_password",
    "set_password",
    "verify_password",
    "verify_token",
]

# pbkdf2 迭代次数（OWASP 2023 起的建议量级）
PBKDF2_ITERATIONS = 200_000
# 口令最短长度（网页 setup 与 passwd 同一道门槛）
MIN_PASSWORD_LENGTH = 8


class AlreadySetUp(Exception):
    """口令已设过：set_password 只许初始化一次。"""


def _cred_path():
    """凭据文件路径：home()/auth.json。"""
    return store.home() / "auth.json"


def _load_creds() -> dict | None:
    """读凭据；没有/坏了返回 None。

    总review二 G15（C④-6）：docstring 原来还有半句"坏了等同没设过，可重新
    setup"——这跟 is_set_up() 只看 is_file()（不解析内容）不一致：auth.json
    存在但坏了时，is_set_up() 仍然 True，setup 端点会拒绝重跑；实际后果是
    "谁也登不上、也不能重 setup"，不是"等同没设过"。这是安全方向没错（不
    因为文件损坏就悄悄放行重新设密码），但 docstring 说错了，删掉。
    """
    path = _cred_path()
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            creds = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(creds, dict):
        return None
    return creds


def is_set_up() -> bool:
    """是否已设过口令。"""
    return _cred_path().is_file()


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    ).hex()


def reset_password(password: str) -> None:
    """写入/覆盖口令（新盐 + 新 token_secret，旧会话随之全部失效）。

    passwd 子命令用这个；网页 setup 走 set_password（只许一次）。
    G7：token_generation 顺带 +1（新 token_secret 已经让旧 token 签名对不上，
    这里再 +1 只是让"代次"这条线自己也保持单调，不依赖 secret 是否真的变了）。
    """
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"口令至少 {MIN_PASSWORD_LENGTH} 个字符")
    store.ensure_dirs()
    old_generation = int((_load_creds() or {}).get("token_generation") or 0)
    salt = secrets.token_bytes(16)
    creds = {
        "salt": salt.hex(),
        "password_hash": _hash_password(password, salt),
        "token_secret": secrets.token_bytes(32).hex(),
        "token_generation": old_generation + 1,
        "created_at": store.utc_now_iso(),
    }
    # mode=0o600：临时文件就按 0600 建，落盘即收紧，不留旁观窗口
    store.atomic_write_json(_cred_path(), creds, mode=0o600)


def set_password(password: str) -> None:
    """首次设口令；已设过抛 AlreadySetUp（改口令请走命令行 passwd）。"""
    if is_set_up():
        raise AlreadySetUp("口令已设过，只能初始化一次；改口令请用命令行 passwd")
    reset_password(password)


def verify_password(password: str) -> bool:
    """口令对不对；没设过一律 False。常数时间比较。"""
    creds = _load_creds()
    if not creds:
        return False
    try:
        salt = bytes.fromhex(creds["salt"])
        stored = creds["password_hash"]
    except (KeyError, ValueError):
        return False
    calc = _hash_password(password, salt)
    return hmac.compare_digest(calc, stored)


def _sign(token_secret_hex: str, exp: int, generation: int) -> str:
    msg = f"{exp}.{generation}".encode("ascii")
    return hmac.new(bytes.fromhex(token_secret_hex), msg, hashlib.sha256).hexdigest()


def issue_token(days: int) -> str:
    """签发登录 token："<exp_unix>.<token_generation>.<hmac_sha256(secret, exp, generation)>"。

    G7：generation 签进消息体里一起做 HMAC，不能只是拼在后面——否则伪造者
    能拿一个旧 token 的签名配上新的 generation 数字混过去。
    """
    creds = _load_creds()
    if not creds or "token_secret" not in creds:
        raise RuntimeError("还没有口令，先 setup/passwd 再签 token")
    exp = int(time.time()) + int(days) * 86400
    generation = int(creds.get("token_generation") or 0)
    return f"{exp}.{generation}.{_sign(creds['token_secret'], exp, generation)}"


def verify_token(token: str | None) -> bool:
    """token 签名对、未过期、代次跟 auth.json 里当前的一致才算登录。

    口令重设后 secret 换了，旧 token 全失效；G7：logout/passwd 让代次 +1，
    旧 token 即使签名和过期时间都还对，代次不等也判无效——不用等它自然过期。
    """
    if not token:
        return False
    creds = _load_creds()
    if not creds or "token_secret" not in creds:
        return False
    parts = str(token).split(".", 2)
    if len(parts) != 3:
        return False
    exp_text, gen_text, sig = parts
    try:
        exp = int(exp_text)
        generation = int(gen_text)
    except ValueError:
        return False
    if exp < time.time():
        return False
    if generation != int(creds.get("token_generation") or 0):
        return False
    return hmac.compare_digest(sig, _sign(creds["token_secret"], exp, generation))


def bump_token_generation() -> None:
    """代次 +1：单用户系统，退出登录即让所有设备上的旧 token 失效。

    没设过口令时是 no-op（没有 auth.json 也就没有可失效的 token）。
    """
    creds = _load_creds()
    if not creds:
        return
    creds["token_generation"] = int(creds.get("token_generation") or 0) + 1
    store.atomic_write_json(_cred_path(), creds, mode=0o600)


class LoginRateLimiter:
    """进程内登录失败限速：同一来源 15 分钟窗口内失败 ≥5 次 → 一律拒绝。

    "同一来源"由 server 决定（X-Real-IP 头，没有则对端地址；nginx 会覆盖
    该头，直连时伪造它没意义）。状态只在内存里，重启即清零——够用，
    前面还有 nginx limit_req 这道闸。
    """

    WINDOW_SECONDS = 15 * 60
    MAX_FAILURES = 5

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, source: str, now: float) -> None:
        """清掉窗口外的失败记录（调用方需已持锁）。"""
        stamps = self._failures.get(source)
        if stamps is None:
            return
        stamps[:] = [
            t for t in stamps if now - t < self.WINDOW_SECONDS
        ]
        if not stamps:
            del self._failures[source]

    def allowed(self, source: str) -> bool:
        """该来源现在还允许尝试登录吗。"""
        now = time.time()
        with self._lock:
            self._prune(source, now)
            return len(self._failures.get(source, ())) < self.MAX_FAILURES

    def record_failure(self, source: str) -> None:
        """记一次登录失败。"""
        now = time.time()
        with self._lock:
            self._prune(source, now)
            self._failures.setdefault(source, []).append(now)
