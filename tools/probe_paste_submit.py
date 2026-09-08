#!/usr/bin/env python3
"""零额度靶测：launcher.send_keys 敲进真 TUI 的长多行文本能不能被提交。

起因（9/8）：Codex TUI 0.151.0 收裸多行粘贴时 paste-burst 启发式把尾巴留在缓冲里、
吞掉后面的 Enter，第 2 轮返工意见在施工窗口输入框里躺了 50 分钟。修法是 paste-buffer
加 -p（括号粘贴）。Codex / Claude Code 任一升级后跑一遍这个脚本复验，两家都不会真发
请求：Codex 指到一个连不上的 provider，CC 把 ANTHROPIC_BASE_URL 指到死端口。

用法（仓库根目录）：
    PYTHONPATH=. python3 tools/probe_paste_submit.py codex
    PYTHONPATH=. python3 tools/probe_paste_submit.py claude
    PYTHONPATH=. python3 tools/probe_paste_submit.py codex --text 某个文件   # 换别的文本

判定：TUI 的会话记录（Codex rollout / CC session JSONL）里出现一条正文与发送文本
一字不差的用户消息 → PASS；等满超时没出现 → FAIL，并把屏幕最后几行打出来。
只用自己的 tmux 会话 `ns-paste-probe`，绝不碰名为 claude 的会话。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nightshift import launcher

SESSION = "ns-paste-probe"
DEAD_CODEX = [
    "-c", "model_provider=dead",
    "-c", "model_providers.dead.name=dead",
    "-c", "model_providers.dead.base_url=http://127.0.0.1:9/v1",
    "-c", "model_providers.dead.wire_api=responses",
]


def default_text() -> str:
    body = "\n".join(
        f"{i}. 审稿意见第 {i} 条：[文件](/tmp/x/y.py:{i}) `代码` -flag; 尾分号" for i in range(1, 120)
    )
    return "你在无人值守的定时会话里工作。\n\n阻断项：\n\n" + body + "\n\nNEXT: fix\n\n只在当前工作树里施工，不要切回主签出目录。"


def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)


def screen() -> str:
    return tmux("capture-pane", "-p", "-t", f"{SESSION}:0").stdout


def wait_screen(needle: str, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if needle in screen():
            return True
        time.sleep(0.5)
    return False


def start_tui(runner: str, workdir: str) -> str:
    tmux("kill-session", "-t", SESSION)
    if runner == "codex":
        cmd = (
            "codex --sandbox read-only --ask-for-approval never "
            + " ".join(launcher._sq(a) for a in DEAD_CODEX)
            + "; echo EXIT $?; read"
        )
    else:
        cmd = (
            "[ -f /root/.claude-oauth-token.env ] && source /root/.claude-oauth-token.env; "
            "ANTHROPIC_BASE_URL=http://127.0.0.1:9 claude --model claude-haiku-4-5-20251001 "
            "--permission-mode dontAsk; echo EXIT $?; read"
        )
    proc = tmux("new-session", "-d", "-s", SESSION, "-x", "120", "-y", "30", "-c", workdir, cmd)
    if proc.returncode != 0:
        sys.exit(f"tmux new-session 失败：{proc.stderr}")
    wid = tmux("display", "-p", "-t", f"{SESSION}:0", "#{window_id}").stdout.strip()
    # 各家启动时可能弹的交互框：Codex 升级提示选 2（跳过）、Codex 临时目录信任选 1、CC 信任
    # 目录选"是"。每个框只按一次——Codex 是行内渲染，旧提示的文字会留在屏幕上方，按第二次
    # 就会敲进输入框。
    dismissed: set[str] = set()
    deadline = time.time() + 40
    while time.time() < deadline:
        s = screen()
        if ("Ask Codex" in s) if runner == "codex" else ("❯" in s):
            time.sleep(1.0)
            return wid
        if runner == "codex" and "Update available" in s and "update" not in dismissed:
            dismissed.add("update")
            tmux("send-keys", "-t", wid, "2")
        elif runner == "codex" and "trust the contents of this directory" in s and "trust" not in dismissed:
            dismissed.add("trust")
            tmux("send-keys", "-t", wid, "1")
            time.sleep(0.3)
            tmux("send-keys", "-t", wid, "Enter")
        elif runner == "claude" and "trust this folder" in s and "trust" not in dismissed:
            dismissed.add("trust")
            tmux("send-keys", "-t", wid, "Down")
            time.sleep(0.3)
            tmux("send-keys", "-t", wid, "Enter")
        time.sleep(0.5)
    sys.exit("TUI 40 秒内没就绪，屏幕：\n" + screen())


def find_submitted(runner: str, workdir: str, since: float, text: str) -> bool:
    if runner == "codex":
        files = [f for f in glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl"))
                 if os.path.getmtime(f) >= since]
    else:
        mangled = workdir.replace("/", "-")
        files = [f for f in glob.glob(os.path.expanduser(f"~/.claude/projects/{mangled}/*.jsonl"))
                 if os.path.getmtime(f) >= since]
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if runner == "codex":
                    p = d.get("payload") or {}
                    if p.get("role") != "user":
                        continue
                    got = "".join(x.get("text", "") for x in p.get("content", []) if isinstance(x, dict))
                else:
                    if d.get("type") != "user":
                        continue
                    c = (d.get("message") or {}).get("content")
                    got = c if isinstance(c, str) else "".join(
                        x.get("text", "") for x in (c or []) if isinstance(x, dict)
                    )
                if got == text:
                    return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runner", choices=["codex", "claude"])
    ap.add_argument("--text", help="要发的文本文件；默认造一段 120 行的审稿意见")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()
    text = Path(args.text).read_text(encoding="utf-8") if args.text else default_text()
    workdir = tempfile.mkdtemp(prefix="ns-paste-probe-")
    since = time.time() - 1
    wid = start_tui(args.runner, workdir)
    proc = launcher.send_keys(wid, text)
    if proc.returncode != 0:
        print("send_keys 失败：", proc.stderr)
        return 2
    deadline = time.time() + args.timeout
    ok = False
    while time.time() < deadline and not ok:
        time.sleep(1.0)
        ok = find_submitted(args.runner, workdir, since, text)
    tail = "\n".join(l for l in screen().splitlines() if l.strip())[-1200:]
    tmux("kill-session", "-t", SESSION)
    if ok:
        print(f"PASS：{args.runner} 收到并提交了 {len(text)} 字的多行文本（一字不差）")
        return 0
    print(f"FAIL：{args.runner} {args.timeout:.0f} 秒内没提交，屏幕尾部：\n{tail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
