"""一次性核对各模型的上下文窗口（S4② 追加，8/29 工头要求）。

背景：config 的 models 表里的 context_limit 除个别外没有实证；官方 /v1/models
不给上下文大小，而 Claude Code 状态栏 JSON 里有
context_window.context_window_size（当前模型的上下文窗口）。

做法：对 config models 里每个模型在 tmux 会话 ns-selftest 里起一次交互式
claude（不发任何提示词、零 API 调用），用 --settings 挂一个临时 statusLine
命令把状态栏 JSON 原样落盘，从里面读 context_window.context_window_size。
拿不到该字段就记"未知"，不猜。

前提与用法（在仓库根目录）：
    python3 tools/probe_context_windows.py [--cwd 已信任目录] [config.json]
- --cwd 必须是已在 Claude Code 里点过信任的目录，否则会话卡在信任问答、
  探不到（记"未知"）；默认用当前目录。
- config 不给就核对 config.example.json。
- 结束后自动 kill 会话 ns-selftest（本仓库测试专用会话名）。
跑完打印对照表，人肉核对后手工写回 config（样例与生产都要）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.example.json"
TMUX_SESSION = "ns-selftest"  # 本仓库测试专用 tmux 会话名，用完即杀
WAIT_SECONDS = 40


def claude_rc_of(config: dict) -> dict:
    """S6 起 `runners.claude` 是权威表，顶层 `models`/`claude_bin` 只是兼容快照
    （两表可能分裂，9/1 就是手工分别加的）；有 runner 表就只看 runner 表。"""
    runners = config.get("runners")
    rc = runners.get("claude") if isinstance(runners, dict) else None
    return rc if isinstance(rc, dict) else {}


def models_of(config: dict) -> dict:
    """要核对的模型表：runners.claude.models 优先，退顶层 models。"""
    return dict(claude_rc_of(config).get("models") or config.get("models") or {})


def claude_bin_of(config: dict) -> str:
    return claude_rc_of(config).get("bin") or config.get("claude_bin") or "claude"


def _tmux(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, "", str(exc.stderr))


def probe_one(claude_bin: str, model: str, workdir: Path, cwd: Path) -> int | None:
    """起一次交互式会话（零 API 调用），从 statusLine 落盘 JSON 读窗口大小。"""
    out = workdir / f"{model}.json"
    settings = workdir / f"settings-{model}.json"
    settings.write_text(
        json.dumps(
            {
                "statusLine": {
                    "type": "command",
                    "command": f"cat > {out}",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _tmux("kill-session", "-t", f"={TMUX_SESSION}")
    proc = _tmux(
        "new-session", "-d", "-s", TMUX_SESSION, "-c", str(cwd),
        claude_bin, "--model", model, "--tools", "",
        "--settings", str(settings),
    )
    if proc.returncode != 0:
        print(f"[警告] {model}：tmux 起不来：{proc.stderr.strip()[-200:]}", file=sys.stderr)
        return None
    deadline = time.monotonic() + WAIT_SECONDS
    data: dict | None = None
    while time.monotonic() < deadline:
        if out.is_file():
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
                break
            except ValueError:
                data = None  # 写到一半，再等
        time.sleep(1)
    _tmux("kill-session", "-t", f"={TMUX_SESSION}")
    if not isinstance(data, dict):
        print(f"[警告] {model}：状态栏 JSON 没等到（目录未信任？）", file=sys.stderr)
        return None
    window = data.get("context_window")
    size = window.get("context_window_size") if isinstance(window, dict) else None
    return size if isinstance(size, int) and size > 0 else None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="核对各模型的上下文窗口")
    parser.add_argument("--cwd", default=os.getcwd(),
                        help="已在 Claude Code 里点过信任的目录（默认当前目录）")
    parser.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG))
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    table = models_of(config)
    models = list(table.keys())
    claude_bin = claude_bin_of(config)
    if not models:
        print("config 里没有 models，没得核对。")
        return 1
    print(f"核对 {config_path} 里的 {len(models)} 个模型（交互式起窗，零 API 调用）……\n")

    rows: list[tuple[str, int | None]] = []
    with tempfile.TemporaryDirectory(prefix="ns-probe-") as tmp:
        for model in models:
            print(f"探测 {model} ……", flush=True)
            rows.append((model, probe_one(claude_bin, model, Path(tmp), Path(args.cwd))))
    _tmux("kill-session", "-t", f"={TMUX_SESSION}")

    print("\n=== 核对表（模型 → 探到的 context_window_size → 配置值）===")
    print(f"{'模型':<34}{'探到':>16}{'配置':>14}  一致?")
    for model, size in rows:
        cur = (table.get(model) or {}).get("context_limit")
        got = "未知" if size is None else f"{size:,}"
        cur_s = "缺" if cur is None else f"{cur:,}"
        mark = "-" if size is None else ("✓" if size == cur else "×")
        print(f"{model:<34}{got:>16}{cur_s:>14}  {mark}")
    print("\n拿不到字段的一律记\"未知\"，不猜。核对完手工写回 config（样例与生产都要）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
