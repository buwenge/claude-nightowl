#!/usr/bin/env python3
"""假 codex app-server：只答 initialize → initialized → account/rateLimits/read
三步，JSONL over stdio；供 nightshift 的 quota 测试用，绝不联网。

行为按环境变量控制：
- NIGHTSHIFT_FAKE_CODEX_RATELIMITS_FILE：给了就读该 JSON 文件当 result 回传；
  没给就回一份写死的正常样例。
- NIGHTSHIFT_FAKE_CODEX_HANG=1：initialize 之后不回应，模拟真机卡死超时。
- NIGHTSHIFT_FAKE_CODEX_EXIT_EARLY=1：initialize 答完就直接退出（模拟 EOF）。
"""
import json
import os
import sys
import time

_DEFAULT_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1788099565},
        "secondary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1788653052},
        "rateLimitReachedType": None,
        "planType": "plus",
    },
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1788099565},
            "secondary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1788653052},
            "rateLimitReachedType": None,
            "planType": "plus",
        }
    },
    "rateLimitResetCredits": {"availableCount": 1, "credits": []},
}


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        if method == "initialize":
            send({"id": msg.get("id"), "result": {"userAgent": "fake", "codexHome": "/tmp"}})
            if os.environ.get("NIGHTSHIFT_FAKE_CODEX_HANG"):
                time.sleep(60)
            if os.environ.get("NIGHTSHIFT_FAKE_CODEX_EXIT_EARLY"):
                return 0
        elif method == "initialized":
            continue
        elif method == "account/rateLimits/read":
            fake_file = os.environ.get("NIGHTSHIFT_FAKE_CODEX_RATELIMITS_FILE")
            if fake_file:
                with open(fake_file, encoding="utf-8") as f:
                    result = json.load(f)
            else:
                result = _DEFAULT_RESULT
            send({"id": msg.get("id"), "result": result})
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
