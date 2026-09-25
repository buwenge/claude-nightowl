#!/usr/bin/env python3
"""Claude Code statusLine 脚本示例：把每次收到的 rate_limits 合并写进 ~/.claude/rate_limits.json，
供 nightshift 在一年期令牌（CLAUDE_CODE_OAUTH_TOKEN）下读额度用。零额外请求。

settings.json 里挂上即可：
    "statusLine": {"type": "command", "command": "python3 /path/to/statusline_rate_limits.py"}
状态栏文字随便改；只要 record_rate_limits() 那段留着就行。

好几个窗口会同时刷新状态栏，别的程序也可能往这个文件合并读数，所以"读出→合并→写回"
要排队：所有写方都锁同一个 `~/.claude/.rate_limits.json.lock`（fcntl.flock 独占）。
不排队的话后写的会冲掉先写的（实测 50 条读数丢 7 条）。状态栏最多等 1 秒，拿不到锁就
跳过这次记录，不能让状态栏卡住。
"""

import fcntl
import json
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timezone

RATE_LIMITS_FILE = pathlib.Path.home() / ".claude" / "rate_limits.json"
RATE_LIMITS_LOCK = RATE_LIMITS_FILE.with_name(f".{RATE_LIMITS_FILE.name}.lock")
LOCK_WAIT_SECONDS = 1.0


def record_rate_limits(data: dict) -> None:
    """本次带的窗口覆盖，没带的保留；任何失败都吞掉，不影响状态栏。"""
    try:
        now = datetime.now(timezone.utc).timestamp()
        windows = {}
        for key, w in (data.get("rate_limits") or {}).items():
            if not isinstance(w, dict) or w.get("used_percentage") is None:
                continue
            resets = w.get("resets_at")
            try:
                resets_iso = datetime.fromtimestamp(int(resets), tz=timezone.utc).isoformat() if resets else None
            except Exception:
                resets_iso = None
            windows[key] = {"utilization": float(w["used_percentage"]), "resets_at": resets_iso, "at": now}
        if not windows:
            return
        model = (data.get("model") or {}).get("id") or (data.get("model") or {}).get("display_name")
        RATE_LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RATE_LIMITS_LOCK, "a+") as lock_file:
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return  # 别的写方占着：这次不记，下次刷新再记
                    time.sleep(0.01)
            _merge_and_write(windows, model, now)
    except Exception:
        pass


def _merge_and_write(windows: dict, model: str | None, now: float) -> None:
    """持锁期间调用：读出、合并、原子写回。"""
    try:
        try:
            doc = json.loads(RATE_LIMITS_FILE.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                doc = {}
            merged = dict(doc.get("windows") or {})
            models = dict(doc.get("models") or {})  # 各模型最近活动时刻（可选，给别的读方判断"谁在动"）
        except Exception:
            doc, merged, models = {}, {}, {}
        merged.update(windows)
        if model:
            models[model] = now
        # 在原文档上更新，别重建：别的写方放的字段（如探针的 fable_probe_at）要保留
        payload = dict(doc)
        payload.update({
            "updated_at": now,
            "source": "statusline",
            "model": model,
            "windows": merged,
            "models": models,
        })
        # 临时文件名每次不同（固定名的话两个写方会抢同一个临时文件），权限跟原文件一致
        fd, tmp_name = tempfile.mkstemp(dir=str(RATE_LIMITS_FILE.parent), prefix=f".{RATE_LIMITS_FILE.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            try:
                os.chmod(tmp_name, RATE_LIMITS_FILE.stat().st_mode & 0o777)
            except FileNotFoundError:
                os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, RATE_LIMITS_FILE)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        pass


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("quota: --")
        return
    record_rate_limits(data)
    rate = data.get("rate_limits") or {}
    parts = [f"[{(data.get('model') or {}).get('display_name', '?')}]"]
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        used = (rate.get(key) or {}).get("used_percentage")
        if used is not None:
            parts.append(f"{label} {100 - used:.0f}% left")
    print(" | ".join(parts))


if __name__ == "__main__":
    main()
