"""命令行入口：`python3 -m nightshift <子命令>`。

add / list / show / run-now / cancel / quota / capture / serve，全部中文帮助。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import launcher, quota, scheduler, store

__all__ = ["main"]


def _local_tz(hours: int) -> timezone:
    return timezone(timedelta(hours=hours))


def _parse_run_at(text: str, tz_hours: int) -> str:
    """按固定偏移的本地时间解释 `YYYY-MM-DD HH:MM`，转成 Z 结尾的 UTC ISO。"""
    naive = datetime.strptime(text, "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=_local_tz(tz_hours)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _display_tz(config: dict) -> timezone:
    return _local_tz(config["display_tz_offset_hours"])


# ---------- 子命令 ----------


def cmd_add(args) -> int:
    config = store.load_config()
    store.ensure_dirs()
    tz_hours = args.tz_hours if args.tz_hours is not None else config[
        "display_tz_offset_hours"
    ]
    try:
        run_at = _parse_run_at(args.run_at, tz_hours)
    except ValueError:
        print(f"run-at 认不出来：{args.run_at}（要 YYYY-MM-DD HH:MM）", file=sys.stderr)
        return 2

    if args.task_file:
        task_text = Path(args.task_file).read_text(encoding="utf-8")
    else:
        task_text = args.task_text or ""
    if not task_text.strip():
        print("任务内容不能为空：--task-text 或 --task-file 至少给一个", file=sys.stderr)
        return 2

    project_path = config["projects"].get(args.project)
    if not project_path:
        print(f"project 不在 config.projects 里：{args.project}", file=sys.stderr)
        return 2

    if args.prompt_file:
        prompt_final = Path(args.prompt_file).read_text(encoding="utf-8")
    else:
        prompt_final = store.build_prompt(
            config, args.title, args.project, args.model, task_text
        )

    task = {
        "title": args.title,
        "project": args.project,
        "model": args.model,
        "effort": args.effort,
        "run_at": run_at,
        "task_text": task_text,
        "prompt_final": prompt_final,
    }
    try:
        task_id = store.create_task(task, config)
    except ValueError as exc:
        print(f"建任务被拦：{exc}", file=sys.stderr)
        return 2
    print(f"任务已建：{task_id}")
    print(f"计划时间：{run_at}（本地 UTC+{tz_hours}：{args.run_at}）")
    print("—— 最终提示词全文如下，请过目 ——")
    print(prompt_final)
    return 0


def cmd_list(args) -> int:
    config = store.load_config()
    tz = _display_tz(config)
    for item in store.list_tasks():
        task, status = item["task"], item["status"]
        run_at = datetime.fromisoformat(task["run_at"].replace("Z", "+00:00"))
        ctx = status.get("context_pct")
        ctx_text = f"{ctx}%" if ctx is not None else "-"
        state = status.get("state", "-")
        print(f"{task['id']}  {state:<18} {run_at.astimezone(tz).strftime('%m-%d %H:%M')}  "
              f"{task['title']}  ctx={ctx_text}")
    return 0


def cmd_show(args) -> int:
    store.load_config()
    print("== task.json ==")
    print(json.dumps(store.load_task(args.id), ensure_ascii=False, indent=2))
    print("== status.json ==")
    print(json.dumps(store.read_status(args.id), ensure_ascii=False, indent=2))
    print("== events.log 末 20 行 ==")
    events = store.task_dir(args.id) / "events.log"
    if events.is_file():
        for line in events.read_text(encoding="utf-8").splitlines()[-20:]:
            print(line)
    return 0


def cmd_run_now(args) -> int:
    config = store.load_config()
    task = store.load_task(args.id)
    if args.dry_run:
        session_id = str(uuid.uuid4())
        d = store.task_dir(task["id"])
        window_name = f"{config['window_prefix']}{task['title']}"
        print("—— run.sh 将来会生成成这样（本次未写盘）——")
        print(launcher.run_sh_text(task, config, session_id))
        print("—— 到点会执行的 tmux 命令 ——")
        print(
            f"tmux new-window -d -P -F '#{{window_id}}' "
            f"-t {config['tmux_session']} -n '{window_name}' {d / 'run.sh'}"
        )
        return 0
    status = launcher.launch(args.id, config)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def cmd_cancel(args) -> int:
    store.load_config()
    status = store.read_status(args.id)
    if status.get("state") not in ("scheduled", "postponed"):
        print(f"只有 scheduled/postponed 的任务能取消，当前是 {status.get('state', '-')}",
              file=sys.stderr)
        return 1
    store.update_status(args.id, state="cancelled", last_event_at=store.utc_now_iso())
    store.append_event(args.id, "已取消")
    print(f"任务 {args.id} 已取消")
    return 0


def cmd_quota(args) -> int:
    config = store.load_config()
    try:
        usage = quota.fetch_usage(config)
    except (quota.UsageUnavailable, quota.UsageParseError) as exc:
        print(f"额度查不到：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(usage, ensure_ascii=False, indent=2))
    return 0


def cmd_capture(args) -> int:
    store.load_config()
    window_id = store.read_status(args.id).get("window_id")
    if not window_id:
        print("这个任务还没有开过窗口", file=sys.stderr)
        return 1
    print(launcher.capture_pane(window_id, lines=args.lines), end="")
    return 0


def cmd_serve(args) -> int:
    """调度器服务：--once 跑一轮 tick 就退出（cron 与集成测试用），否则常驻。"""
    config = store.load_config()
    store.ensure_dirs()
    if args.once:
        actions = scheduler.tick(config, datetime.now(timezone.utc))
        for line in actions:
            print(line)
        return 0
    scheduler.run_forever(config)
    return 0


# ---------- 参数表 ----------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nightshift",
        description="夜班（nightshift）：到点在 tmux 里开交互式 claude 跑定时任务",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="子命令")

    p = sub.add_parser("add", help="建一个定时任务")
    p.add_argument("--title", required=True, help="任务标题（也是 tmux 窗口名的一部分）")
    p.add_argument("--project", required=True, help="项目名，必须是 config.projects 里的键")
    p.add_argument("--model", required=True, help="模型名，建议用 config.models 里的键")
    p.add_argument("--effort", required=True, help="思考档位（config.efforts 之一）")
    p.add_argument("--run-at", required=True,
                   help='本地计划时间，如 "2026-08-28 02:30"')
    p.add_argument("--tz-hours", type=int, default=None,
                   help="本地时区相对 UTC 的偏移小时数（默认取配置 display_tz_offset_hours）")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--task-text", help="任务内容正文")
    group.add_argument("--task-file", help="任务内容从文件读")
    p.add_argument("--prompt-file", default=None,
                   help="直接给最终提示词文件（不给就按模板渲染）")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="列出所有任务")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="看一个任务的完整状态")
    p.add_argument("id", help="任务 id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("run-now", help="不等到点，现在就跑（本期不做额度预检）")
    p.add_argument("id", help="任务 id")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印将要生成的 run.sh 与 tmux 命令，不写盘不开窗")
    p.set_defaults(func=cmd_run_now)

    p = sub.add_parser("cancel", help="取消一个还没跑的任务")
    p.add_argument("id", help="任务 id")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("quota", help="查一次账号额度并打印解析结果")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("capture", help="抓一个任务窗口的最近屏幕")
    p.add_argument("id", help="任务 id")
    p.add_argument("--lines", type=int, default=200, help="抓最近多少行（默认 200）")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("serve", help="起调度器（systemd 常驻跑；--once 只跑一轮就退出）")
    p.add_argument("--once", action="store_true",
                   help="只跑一轮 tick 就退出（cron 与集成测试用）")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except store.ConfigMissing as exc:
        print(f"[nightshift] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
