# claude-nightowl（夜猫子）

**让 Claude Code（和 Codex CLI）在你睡觉时按计划在 tmux 里无人值守地干活——自带额度预检、hook 状态回报、上下文水位守卫、自动换班交接、隔离工作树，以及可选的"施工 → 审稿"流水线。**

包名与命令叫 `nightshift`（夜班）。English version: [README.md](README.md) · 详细操作手册：[docs/使用手册.md](docs/使用手册.md)

```
02:30  调度器在 tmux 里开一个窗口 ──▶ claude --permission-mode auto（你的提示词）
       hook 逐条回报事件 ──────────▶ status.json / events.log
       上下文到 80% ────────────────▶ "写交接、commit、停下"
       交接末行 NEXT: continue ─────▶ 下一个窗口接着上一班的进度干
07:00  你 ssh 进来，Ctrl+B w 选窗口，在同一个会话里接着聊
```

---

## 为什么做这个

交互式的 Claude Code 会话是干正经活最好的地方：完整的工具权限、plan 模式、记忆、子 agent，而且人随时可以插进去。问题只有一个——得有人醒着往里打字。

用 cron 跑 `claude -p` 能自动化，但交互式会话没了。**夜猫子把交互式会话留着，把"守夜"的活接过来**：到点在 tmux 窗口里起一个*正常的* `claude`，通过 Claude Code 自己的 hook 看它在干什么，读 transcript 算上下文用了多少，窗口快满的时候让模型写交接、再开下一班。

它从不抓屏幕猜模型在干嘛，也从不接管对话。第二天早上窗口还在。

## 功能

- **带预检的定时。** 任务可以定在某个时刻跑，也可以定在另一个任务结束之后跑。起跑前调度器检查：目标目录已被 Claude Code 信任、同目录没有别的任务在跑、账号额度在你设的线以上——不够就**推迟**（默认 30 分钟一步，最多 6 小时），绝不硬起。
- **靠 hook 报状态，不抓屏。** 每个任务自带一套 hook 配置（走 `--settings`，不往你项目里写任何东西）。七个 hook 事件在文件锁下更新任务自己的 `status.json`。`Stop` 事件会带回后台任务和已设闹钟，所以调度器分得清"在等后台任务""在等闹钟"和"真干完了"。
- **上下文守卫与换班交接。** 每 20 次工具调用（或 transcript 长了一截、或过了 5 分钟）hook 读一次 transcript 算水位。到警戒线（默认模型窗口的 80%）就注入收尾指令：写 `handover-<n>.md`、commit、停下。调度器读交接的最后一行——`NEXT: continue` 拿着交接开下一个窗口；`NEXT: done` 任务完结。每个角色最多 `chain.max_windows` 班。
- **额度守卫。** 每 5 分钟解析一次 `claude -p /usage`（本地斜杠命令，不耗额度）。撞五小时线时会话自己设闹钟、刷新后接着干；撞周线就收尾。子 agent 也会收到一句短提醒，不会在主会话睡下后继续烧。
- **隔离工作树与存档点。** 默认每个任务在 `<项目>/.claude/worktrees/<slug>` 里、分支 `ns/<slug>` 上干活。模型不 commit；调度器在每个班次边界打一个存档点。干完了你在网页上点**合并**（`--no-ff`）或**丢弃**，或者设 `merge_policy: auto`。启动时的对账绝不删除任何不是它自己建的东西。
- **施工 → 审稿流水线。** 打开 `review`，施工班干完后接一个只读审稿班（同一个模型、换一个模型、或 Codex 都行），审稿班用 `NEXT: done` / `NEXT: fix` / `NEXT: pending` 给结论。`fix` 会让施工角色带着审稿意见回到*同一个*工作树返工；轮数有上限。任何一条流水线都可以在网页上**"我来看"**暂停、继续，或跳过审稿。
- **Codex CLI 当第二种工人。** 任务可以跑在 `codex` 上而不是 `claude`。状态一样来自 hook（通过 Codex 的 hooks profile 和官方 notify 端点），守卫文案改用 `tmux send-keys` 投递，还有一个小小的**后台进程登记簿**，让沙箱里的 Codex 会话能起长任务而不丢单。两个账号的额度并排显示。
- **保活与卡住检测。** 在等待的会话（等后台任务、或被"我来看"按住）每 50 分钟（Codex 25 分钟）收到一句探针，让提示词缓存保持热的。在一条工具调用里静默 15 分钟的会话会被标为疑似卡住；可选让调度器按 `Esc` 并注入一句自检提示。
- **手机优先的网页。** 带日历的任务列表、带实时提示词预览的新建表单、可编辑的全部文案模板、带刷新倒计时的额度卡、任何运行中窗口的只读屏幕快照，以及一键操作（取消、中止、停后台、我来看/继续、合并/丢弃、往会话里捎话）。一个口令、cookie 登录，附 nginx 片段。
- **零依赖。** Python 3.12 标准库、`tmux`、`claude` 命令。没有 venv，不 pip。

## 工作原理

```
                 ┌─────────────────────────── 调度器（python3 -m nightshift serve）────────────────────────────┐
                 │  每 30 秒一轮：预检 → 起跑 → 看护 → 保活 → 交接/换班 → 存档点/合并                            │
                 └───────┬─────────────────────────────────────────────────────────────────────────▲────────────┘
                         │ tmux new-window  run.sh                                                │ 读
                         ▼                                                                        │
   ┌────────────────────────────────────────┐   hook 事件（stdin JSON）    ┌───────────────────────┴──────────┐
   │ claude --session-id … --settings hooks │ ───────────────────────────▶ │ ~/.nightshift/tasks/<id>/        │
   │   （一个普通的交互式会话）              │ ◀─── additionalContext ───── │   status.json  events.log        │
   └────────────────────────────────────────┘   （上下文 / 额度提醒）       │   handover-<n>.md  prompt.txt    │
                         ▲                                                └──────────────────────────────────┘
                         │ ssh 进来，Ctrl+B w，接着聊
```

状态机大致是：`scheduled → launching → working ⇄ waiting_background / waiting_wakeup → idle →（存档点）→ chained | finished | awaiting_merge → merged`，旁边还有 `held`、`postponed`、`failed`、`needs_attention`、`chain_exhausted`。每一次状态变化都用人话写进 `events.log`。

## 依赖与前提

- Linux、macOS 或 WSL——有 `tmux`、Python 3.12 和**终端版** Claude Code（`claude` 在 `PATH` 里）的地方。Windows 原生不行（没 tmux、没 `fcntl`）；桌面 app / IDE 插件 / 网页版也不行（调度器起的是 CLI 会话）。
- 机器整夜不睡（VPS 天然满足；笔记本得关掉睡眠）。
- 每个目标项目目录要先用 `claude` 开过一次、点过"信任此文件夹"。预检只读 `~/.claude.json` 里的 `hasTrustDialogAccepted`，没信任的目录拒绝起跑——它绝不替你写这个文件。
- 用支持 `--permission-mode auto` 的模型。Claude Code 对某些模型（比如 Haiku 4.5）会静默回落成手动批准；调度器发现回落会开一个"(注意)"窗口，但任务会停在那里等人。
- 可选：Codex CLI，如果你想用 Codex 工人。

## 快速开始

```bash
git clone https://github.com/buwenge/claude-nightowl.git
cd claude-nightowl

mkdir -p ~/.nightshift
cp config.example.json ~/.nightshift/config.json
$EDITOR ~/.nightshift/config.json      # 至少改：tmux_session、projects、models

# 前台跑调度器 + 网页（127.0.0.1:8190）
python3 -m nightshift serve
```

然后打开 `http://127.0.0.1:8190/`，设一次口令，建任务——或者用命令行：

```bash
python3 -m nightshift add --title "拆分 store.py" --project demo \
    --model claude-sonnet-5 --effort high --run-at "2026-09-03 02:30" \
    --task-text "把 store.py 拆成读写两个模块，跑测试，commit。"

python3 -m nightshift list                      # 任务、状态、上下文水位
python3 -m nightshift show <任务id>             # 完整状态 + 最近事件
python3 -m nightshift run-now <任务id> --dry-run   # 预览 run.sh 与 tmux 命令
python3 -m nightshift quota                     # 解析一次 /usage
python3 -m nightshift capture <任务id> --lines 200 # 抓窗口最近 200 行屏幕
python3 -m nightshift passwd                    # 设置/覆盖网页口令
```

`--run-at` 按 `display_tz_offset_hours`（默认 UTC+8）解释，落盘存 UTC。

## 配置

全部在 `~/.nightshift/config.json`（目录可用 `NIGHTSHIFT_HOME` 改）。从 `config.example.json` 复制后改，重要的键：

| 键 | 是什么 |
|---|---|
| `tmux_session`、`window_prefix` | 任务窗口开在哪个 tmux 会话里（你 ssh 进来落到的那个），窗口怎么命名。 |
| `projects` | `名字 → 绝对路径`。任务只能指向这里面的目录。 |
| `models` | 各模型的 `context_limit`（Claude Code 不暴露这个数，只能查表——Claude 5 全系 1,000,000，Haiku 4.5 200,000）和用来对上 `/usage` 里单模型周线的 `usage_label`。 |
| `runners` | `claude` 与 `codex` 两块：可执行文件、探针模型、允许的模型/档位、保活间隔与文案。 |
| `guards` | 默认额度线（`session_pct_max` 80、`weekly_pct_max` 95、`model_weekly_pct_max`）、`context_warn_ratio` 0.8、保活开关。可按任务覆盖。 |
| `chain` | `max_windows`（默认 3）与 `on_no_handover`（`continue` / `stop`）。 |
| `review` | 审稿默认值：`max_rounds`、`on_no_quota`、`merge_policy`（`manual` / `auto`）、`criteria_text`。 |
| `scheduler` | 巡检间隔、起跑宽限、推迟步长/上限、额度刷新间隔、卡住阈值、起跑超时。 |
| `http` | 监听地址/端口、URL 前缀、cookie 设置。 |
| `warmup` | 可选：每天固定时刻发一句便宜的话，让五小时窗口早点开始算。 |
| `*_template`、`*_text` | 系统往会话里发的每一句话——任务提示词、续班提示词、上下文/额度提醒、审稿指令、保活探针。网页"模板"页都能改；占位符自动填。 |

按任务的覆盖项（`guards`、`chain`、`review`、`keepalive`、`worktree`、`trigger`）在新建表单里设，或直接改 `tasks/<id>/task.json`。

## 交接协议

一个班被要求收尾时，往 `~/.nightshift/tasks/<id>/handover-<班次>.md` 写一份普通的 markdown——做完了什么、没做完什么、下一步做什么——**最后一个非空行**必须是二选一：

```
NEXT: continue      # 没干完：拿着这份交接开下一班
NEXT: done          # 干完了：走完工流程（完结 / 审稿 / 等合并）
```

调度器在几件事上是有讲究的：

- 只认文件。聊天回复里写的 `NEXT:` 不算，所以模型说两遍"done"也没事。
- 一份交接只评估一次。之后要是被重写了（你在窗口里继续聊、让它换个方案），调度器发现文件变了会再评估一次。
- 写完交接但还挂着一个闹钟的班当作已收工，调度器会敲它一句撤掉闹钟；挂着闹钟但**没有**交接的班不动——它还在干活。
- 你在一个已经完结的任务窗口里聊天，任务会短暂显示为工作中，然后自动弹回原来的终态。
- 审稿班不写文件，结论是它最终回复的最后一行。

## 部署

**systemd。** 调度器是前台进程，单元模板在 `deploy/nightshift.service.example`（改 `WorkingDirectory` 与 `NIGHTSHIFT_HOME`，然后 `systemctl enable --now nightshift`）。任务窗口是 tmux 的子进程不是服务的子进程，重启服务不影响正在跑的会话。`serve --once` 只跑一轮，适合挂 cron；`serve --no-http` 不起网页。

**nginx。** 网页要放在 HTTPS 后面。`deploy/nginx-location.example.conf` 把 `/nightshift/` 反代到 `127.0.0.1:8190` 并给登录接口限速。网页里全是相对路径，任何前缀都行。

**安全，认真读一次。** 网页口令是一把钥匙，能让一个自动批准权限的 Claude Code 以调度器的运行用户（通常是 root）身份在你的项目里干活。把它当 shell 登录看：口令要强、只走 HTTPS、有条件前面再套一层第二因子（Cloudflare Access 之类）。口令散列与 cookie 签名密钥存在 `~/.nightshift/auth.json`（0600），不进 `config.json`。登录会话一年有效；改口令让全部登录失效。

## 数据目录

```
~/.nightshift/
├── config.json               你的配置
├── auth.json                 口令散列 + cookie 密钥（0600）
├── quota.json                最近一次解析的 /usage
├── scheduler.log             轮转日志
└── tasks/<id>/
    ├── task.json             你定义的任务
    ├── status.json           机器状态（hook 与调度器在锁下写）
    ├── events.log            一行一条事件，人话
    ├── prompt.txt            发给会话的最终提示词
    ├── settings.json         随 --settings 传入的 hook 配置
    ├── run.sh                tmux 窗口跑的脚本
    ├── handover-<n>.md       各班交接
    └── background/           Codex 后台进程登记簿
```

## 开发

```bash
python3 -m pytest tests -q          # 约 680 条测试，4 分钟左右
```

测试从不真的起 `claude`（通过 `NIGHTSHIFT_CLAUDE_BIN` 换成 `tests/fake_claude.sh`），只写临时目录，用一个叫 `ns-selftest` 的 tmux 会话（自己建、自己杀）。一次只跑一个 pytest 进程——两个会抢这个会话。夹具里的 hook 载荷和 `/usage` 输出都是脱敏过的真实采样。

目录：`nightshift/`（调度器、launcher、hook、store、context、quota、worktree、server、Codex 相关）、`web/`（原生 JS，无构建步骤）、`tests/`、`deploy/`、`tools/`。贡献守则在 `AGENTS.md`，更细的操作说明在 [docs/使用手册.md](docs/使用手册.md)。

## 局限与非目标

- 它是**交互式 CLI 会话**的调度器。不直接调 Anthropic / OpenAI 的 API，也不支持桌面 app 或 IDE 集成。
- 上下文上限是一张你自己维护的表；出了新模型就往 `models` 里加。
- 合并刻意保守：不 `reset --hard`、不 `clean`、不删任何不是调度器自己建的东西。拿不准就停下开"需要人工"窗口，不猜。
- 一台机器、一个用户。没有多租户，也不打算做。

## 许可证

MIT，见 [LICENSE](LICENSE)。
