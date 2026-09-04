#!/usr/bin/env python3
"""Claude Code statusLine 脚本示例：把每次收到的 rate_limits 合并写进 ~/.claude/rate_limits.json，
供 nightshift 在一年期令牌（CLAUDE_CODE_OAUTH_TOKEN）下读额度用。零额外请求。

settings.json 里挂上即可：
    "statusLine": {"type": "command", "command": "python3 /path/to/statusline_rate_limits.py"}
状态栏文字随便改；只要 record_rate_limits() 那段留着就行。
"""

import json
import pathlib
import sys
from datetime import datetime, timezone

RATE_LIMITS_FILE = pathlib.Path.home() / ".claude" / "rate_limits.json"


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
        try:
            doc = json.loads(RATE_LIMITS_FILE.read_text(encoding="utf-8"))
            merged = dict(doc.get("windows") or {})
            models = dict(doc.get("models") or {})  # 各模型最近活动时刻（可选，给别的读方判断"谁在动"）
        except Exception:
            merged, models = {}, {}
        merged.update(windows)
        if model:
            models[model] = now
        payload = {
            "updated_at": now,
            "source": "statusline",
            "model": model,
            "windows": merged,
            "models": models,
        }
        tmp = RATE_LIMITS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(RATE_LIMITS_FILE)
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
