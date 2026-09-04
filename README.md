# claude-nightowl

**Run scheduled, unattended Claude Code (and Codex CLI) sessions inside tmux — with quota preflight, hook-based status tracking, a context-window guard, automatic shift handover, isolated git worktrees, and an optional build → review loop.**

The package and CLI are called `nightshift`. [中文说明 →](README.zh-CN.md) · Detailed operator's manual (Chinese): [docs/使用手册.md](docs/使用手册.md)

```
02:30  scheduler opens a tmux window ──▶ claude --permission-mode auto  (your prompt)
       hooks report every event ─────▶ status.json / events.log
       context hits 80% ─────────────▶ "write a handover, commit, stop"
       handover says NEXT: continue ─▶ next window picks up where it left off
07:00  you ssh in, Ctrl+B w, and keep chatting in the same session
```

---

## Why this exists

An interactive Claude Code session is the best place for real work: full tool access, plan mode, memory, sub-agents, and a human can jump in at any point. The problem is that someone has to be awake to type into it.

`claude -p` under cron gives you automation but throws away the interactive session. **nightowl keeps the interactive session and adds the babysitting**: it launches a *normal* `claude` in a tmux window at the scheduled time, watches it through Claude Code's own hooks, measures context usage by reading the transcript, and when the window is about to run out of room it asks the model to write a handover and starts the next shift.

It never scrapes the screen to guess what the model is doing, and it never takes over the conversation. In the morning the window is still there.

## Features

- **Scheduling with preflight.** A task runs at a time, or after another task ends. Before launching, the scheduler checks the target directory is trusted by Claude Code, that no other task is running in the same directory, and that your account quota is above the configured lines — and *postpones* (default 30 min, up to 6 h) instead of launching into a wall.
- **Status via hooks, not screen-scraping.** Every task carries its own hook settings (`--settings`, nothing written into your project). Seven hook events update the task's `status.json` under a file lock. `Stop` reports background tasks and scheduled alarms, so the scheduler can tell *waiting for a background job* from *waiting for an alarm* from *actually done*.
- **Context guard and shift handover.** Every 20 tool calls (or on transcript growth, or every 5 minutes) the hook reads the transcript and computes the context watermark. At the warning line (default 80% of the model's window) it injects a wrap-up instruction: write `handover-<n>.md`, commit, stop. The scheduler reads the handover's last line — `NEXT: continue` starts the next window with the handover as context; `NEXT: done` finishes the task. Up to `chain.max_windows` shifts per role.
- **Quota guard.** `claude -p /usage` is parsed every 5 minutes (a local slash command, it costs nothing). With a long-lived token (`claude setup-token` in `CLAUDE_CODE_OAUTH_TOKEN`) `/usage` prints nothing, so the guard instead reads a shared file (`runners.claude.rate_limits_file`, default `~/.claude/rate_limits.json`) that a statusLine script keeps updated from the `rate_limits` every Claude Code session receives with each reply. No extra requests; whoever is spending quota is refreshing the numbers. See `docs/使用手册.md` for the file format. Hitting the five-hour line makes the session set its own wake-up alarms and resume after the reset; hitting the weekly line makes it wrap up. Sub-agents get their own short notice so they stop burning quota too.
- **Isolated worktrees and checkpoints.** By default each task works in `<project>/.claude/worktrees/<slug>` on branch `ns/<slug>`. The model never commits; the scheduler commits a checkpoint at every shift boundary. When the task is done you get **merge** (`--no-ff`) or **discard** buttons — or set `merge_policy: auto`. Startup reconciliation never deletes anything it did not create.
- **Build → review pipelines.** Turn on `review` and a finished build shift is followed by a read-only review shift (same model, a different one, or Codex) that answers `NEXT: done` / `NEXT: fix` / `NEXT: pending`. `fix` sends the build role back into the *same* worktree with the review attached; rounds are capped. You can put any pipeline on **hold** ("let me look"), resume it, or skip the review from the web UI.
- **Codex CLI as a second worker.** Tasks can run on `codex` instead of `claude`. Status still comes from hooks (via a Codex hooks profile and the official notify endpoint), guard messages are delivered with `tmux send-keys`, and a small **background-process registry** lets a sandboxed Codex session start long jobs without losing track of them. Both accounts' quotas show up side by side.
- **Keepalive and stuck detection.** Sessions that are waiting (on a background job, or on hold) get a short probe every 50 minutes (25 for Codex) so the prompt cache stays warm. A session that has been silent for 15 minutes inside a tool call is flagged; optionally the scheduler presses `Esc` and injects a self-check prompt.
- **Web UI, phone-first.** Task list with a calendar, new-task form with a live prompt preview, editable message templates, quota card with reset countdowns, a read-only screen snapshot of any running window, and one-click controls (cancel, abort, stop background jobs, hold/resume, merge/discard, send a note into the session). One password, cookie login, nginx snippet included.
- **Zero dependencies.** Python 3.12 standard library, `tmux`, and the `claude` CLI. No venv, no pip.

## How it works

```
                 ┌──────────────────────────── scheduler (python3 -m nightshift serve) ───────────────────────────┐
                 │  tick every 30 s: preflight → launch → watch → keepalive → handover/chain → checkpoint/merge   │
                 └───────┬──────────────────────────────────────────────────────────────────────────▲─────────────┘
                         │ tmux new-window  run.sh                                                 │ reads
                         ▼                                                                         │
   ┌────────────────────────────────────────┐   hook events (stdin JSON)   ┌────────────────────────┴──────────┐
   │ claude --session-id … --settings hooks │ ───────────────────────────▶ │ ~/.nightshift/tasks/<id>/         │
   │   (a normal interactive session)       │ ◀─── additionalContext ───── │   status.json  events.log         │
   └────────────────────────────────────────┘   (context / quota notices)  │   handover-<n>.md  prompt.txt     │
                         ▲                                                 └───────────────────────────────────┘
                         │ ssh in, Ctrl+B w, keep chatting
```

State machine, roughly: `scheduled → launching → working ⇄ waiting_background / waiting_wakeup → idle → (checkpoint) → chained | finished | awaiting_merge → merged`, with `held`, `postponed`, `failed`, `needs_attention`, `chain_exhausted` on the side. Every transition is written to `events.log` in plain language.

## Requirements

- Linux, macOS, or WSL — anywhere with `tmux`, Python 3.12, and the **terminal** version of Claude Code (`claude` on `PATH`). Windows native is out (no tmux, no `fcntl`); the desktop app / IDE plugins / web version are out too (the scheduler drives CLI sessions).
- The machine stays awake all night (a VPS does; a laptop needs sleep disabled).
- Each target project directory must have been opened in `claude` once and trusted ("trust this folder"). The preflight reads `hasTrustDialogAccepted` from `~/.claude.json` and refuses to launch into untrusted directories — it never writes that file for you.
- Use a model that supports `--permission-mode auto`. Claude Code silently falls back to manual approval for some models (Haiku 4.5, for one); the scheduler opens a "(注意)" window when it detects the fallback, but the task will sit there waiting for a human.
- Optional: the Codex CLI, if you want Codex workers.

## Quick start

```bash
git clone https://github.com/buwenge/claude-nightowl.git
cd claude-nightowl

mkdir -p ~/.nightshift
cp config.example.json ~/.nightshift/config.json
$EDITOR ~/.nightshift/config.json      # at minimum: tmux_session, projects, models

# run the scheduler + web UI in the foreground (127.0.0.1:8190)
python3 -m nightshift serve
```

Then open `http://127.0.0.1:8190/`, set the password once, and create a task — or use the CLI:

```bash
python3 -m nightshift add --title "split store.py" --project demo \
    --model claude-sonnet-5 --effort high --run-at "2026-09-03 02:30" \
    --task-text "Split store.py into read and write modules, run the tests, commit."

python3 -m nightshift list                      # tasks, states, context watermark
python3 -m nightshift show <task-id>            # full status + recent events
python3 -m nightshift run-now <task-id> --dry-run   # preview run.sh and the tmux command
python3 -m nightshift quota                     # parse /usage once
python3 -m nightshift capture <task-id> --lines 200 # last 200 screen lines of the window
python3 -m nightshift passwd                    # (re)set the web password
```

`--run-at` is interpreted in `display_tz_offset_hours` (default UTC+8) and stored as UTC.

## Configuration

Everything lives in `~/.nightshift/config.json` (override the directory with `NIGHTSHIFT_HOME`). Copy `config.example.json` and edit; the important keys:

| Key | What it is |
|---|---|
| `tmux_session`, `window_prefix` | Which tmux session task windows open in (the one you land in when you ssh), and how the windows are named. |
| `projects` | `name → absolute path`. Tasks can only target these. |
| `models` | Per-model `context_limit` (Claude Code does not expose it, so it is a table — Claude 5 family 1,000,000, Haiku 4.5 200,000) and the `usage_label` used to match per-model weekly lines in `/usage`. |
| `runners` | `claude` and `codex` blocks: binary, probe model, allowed models/efforts, keepalive interval and text. |
| `guards` | Default quota lines (`session_pct_max` 80, `weekly_pct_max` 95, `model_weekly_pct_max`), `context_warn_ratio` 0.8, keepalive on/off. Overridable per task. |
| `chain` | `max_windows` (default 3) and `on_no_handover` (`continue` / `stop`). |
| `review` | Default review settings: `max_rounds`, `on_no_quota`, `merge_policy` (`manual` / `auto`), `criteria_text`. |
| `scheduler` | Tick interval, launch grace, postpone step/cap, quota refresh interval, stuck thresholds, launching timeout. |
| `http` | Bind host/port, URL prefix, cookie settings. |
| `warmup` | Optional: send one cheap message at a fixed local time so your five-hour window starts earlier in the day. |
| `*_template`, `*_text` | Every message the system ever sends into a session — the task prompt, the handover prompt, context/quota notices, review instructions, keepalive probes. All editable in the web UI's *Templates* tab; placeholders are filled in for you. |

Per-task overrides (`guards`, `chain`, `review`, `keepalive`, `worktree`, `trigger`) are set in the new-task form or in `tasks/<id>/task.json`.

## The handover protocol

When a shift is told to wrap up it writes a plain markdown file at `~/.nightshift/tasks/<id>/handover-<shift>.md` — what is done, what is not, what to do next — and its **last non-empty line** must be one of:

```
NEXT: continue      # not finished: open the next shift with this file as context
NEXT: done          # finished: run the completion flow (finish / review / await merge)
```

A few things the scheduler is careful about:

- Only the file counts. A `NEXT:` line in the chat reply is ignored, so a model that says "done" twice does no harm.
- A handover is evaluated once. If it is rewritten later (you kept chatting in the window and asked for a different plan), the scheduler notices the file changed and evaluates it again.
- A shift that wrote its handover but is still holding a scheduled alarm is treated as finished; the scheduler asks it to cancel the alarm. A shift holding an alarm with **no** handover is left alone — it is still working.
- If you chat in a window whose task is already finished, the task briefly shows as working and then snaps back to its final state.
- Review shifts do not write files. Their verdict is the last line of their final reply.

## Deploying

**systemd.** The scheduler is a foreground process; a unit template is in `deploy/nightshift.service.example` (edit `WorkingDirectory` and `NIGHTSHIFT_HOME`, then `systemctl enable --now nightshift`). Task windows are children of tmux, not of the service, so restarting the service does not touch running sessions. `serve --once` runs a single tick for cron; `serve --no-http` skips the web UI.

**nginx.** Put the web UI behind HTTPS. `deploy/nginx-location.example.conf` proxies `/nightshift/` to `127.0.0.1:8190` and rate-limits the login endpoint. Everything in the UI uses relative paths, so any prefix works.

**Security, read this once.** The web password is a key that lets an auto-approving Claude Code act as the user the scheduler runs as (usually root) inside your projects. Treat it like a shell login: strong password, HTTPS only, and ideally a second factor in front (Cloudflare Access or similar). The password hash and cookie signing key live in `~/.nightshift/auth.json` (mode 0600), never in `config.json`. Login sessions last a year; changing the password invalidates all of them.

## Data directory

```
~/.nightshift/
├── config.json               your settings
├── auth.json                 password hash + cookie key (0600)
├── quota.json                latest parsed /usage
├── scheduler.log             rotating log
└── tasks/<id>/
    ├── task.json             the task as you defined it
    ├── status.json           machine state (hooks + scheduler write it under a lock)
    ├── events.log            one line per event, human-readable
    ├── prompt.txt            the final prompt sent to the session
    ├── settings.json         the hook settings passed with --settings
    ├── run.sh                what the tmux window runs
    ├── handover-<n>.md       shift handovers
    └── background/           Codex background-process registry
```

## Development

```bash
python3 -m pytest tests -q          # ~680 tests, about 4 minutes
```

Tests never start a real `claude` (they use `tests/fake_claude.sh` via `NIGHTSHIFT_CLAUDE_BIN`), write only to temporary directories, and use a tmux session called `ns-selftest` which they create and kill themselves. Run one pytest process at a time — two would fight over that session. Hook payloads and `/usage` output used as fixtures are real, anonymised captures.

Layout: `nightshift/` (scheduler, launcher, hook, store, context, quota, worktree, server, codex bits), `web/` (vanilla JS, no build step), `tests/`, `deploy/`, `tools/`. Contributor rules are in `AGENTS.md`.

## Limitations and non-goals

- It is a scheduler for **interactive CLI sessions**. It does not call the Anthropic or OpenAI APIs directly, and it will not work with the desktop app or IDE integrations.
- Context limits are a table you maintain; when a new model appears, add it to `models`.
- Merging is deliberately conservative: no `reset --hard`, no `clean`, nothing deleted that the scheduler did not create. When in doubt it stops and opens a "needs attention" window instead of guessing.
- One machine, one user. There is no multi-tenant story and no plan for one.

## License

MIT — see [LICENSE](LICENSE).
