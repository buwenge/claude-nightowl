# nightshift（夜班）

到点在 tmux 里开一个新窗口、跑一个**正常的交互式 `claude`** 的定时任务调度器。
你早上 ssh 进来 `Ctrl+B w` 选窗口就能接着聊；跑的时候靠 Claude Code 自己的
hook 回报状态，靠机器读 transcript 算上下文水位——不猜、不接管会话。

## 依赖

- Python 3.12（只用标准库，不装任何第三方包，没有 venv）
- tmux
- Claude Code（`claude` 在 PATH 里）

## 快速开始

```bash
mkdir -p ~/.nightshift
cp config.example.json ~/.nightshift/config.json
```

然后把 `~/.nightshift/config.json` 改成自己的：

| 键 | 改什么 |
|---|---|
| `tmux_session` | 任务窗口开在哪个 tmux 会话里（你登录自动进的那个会话名） |
| `projects` | 项目名 → 目录绝对路径；表单/命令行只能从这里选 |
| `models` | 各模型的上下文上限 `context_limit`、账号额度里的单模型周线标签 `usage_label` |
| `efforts` / `guards` / `chain` | 思考档位、额度与上下文警戒线、换班策略 |
| `prompt_template` 等三个模板 | 提示词模板、到线提醒文案、续班文案 |

另外目标项目目录要先在 Claude Code 里点过一次"信任此文件夹"
（`~/.claude.json` 里 `hasTrustDialogAccepted`），没信任的目录到点会被拦下并开失败窗口。

## 五个常用命令

```bash
# 建任务：明晚 2:30（UTC+8）在 demo 项目里跑一个重构
python3 -m nightshift add --title "重构 store" --project demo \
    --model claude-fable-5 --effort high --run-at "2026-08-28 02:30" \
    --task-text "把 store.py 的读写拆开，跑测试，commit"

# 列出任务（含状态与上下文水位）
python3 -m nightshift list

# 看一个任务的完整状态与最近事件
python3 -m nightshift show <任务id>

# 不等到点现在就跑；先 --dry-run 预览将生成的 run.sh 与 tmux 命令
python3 -m nightshift run-now <任务id> --dry-run
python3 -m nightshift run-now <任务id>

# 查账号额度（无头跑一次 /usage 并解析）
python3 -m nightshift quota

# 抓某个任务窗口最近 200 行屏幕
python3 -m nightshift capture <任务id> --lines 200
```

## 数据目录

数据都在 `NIGHTSHIFT_HOME`（默认 `~/.nightshift`）：

```
~/.nightshift/
├── config.json                     # 你的配置（从 config.example.json 复制改）
├── auth.json                       # 网页登录口令的散列与签名密钥（0600）
├── quota.json                      # 最近一次 /usage 的解析结果（调度器写）
├── scheduler.log                   # 调度器日志（2 MB × 3 轮转）
└── tasks/<任务id>/
    ├── task.json                   # 任务定义（你写的）
    ├── status.json                 # 机器状态（hook 与调度器写）
    ├── events.log                  # 事件流水（一行一条）
    ├── prompt.txt / settings.json / run.sh   # 起会话用的三件套
    └── .lock                       # status.json 的文件锁
```

## hook 机制（两句话）

每个任务自带一套 Claude Code hook 配置（随 `--settings` 传入，不碰任何项目的
settings），七个事件都打到 `python3 -m nightshift.hook <任务id> <事件>`：
它读 stdin 的 JSON，只更新该任务自己的 `status.json`（文件锁 + 原子写）。
`Stop` 事件带回 `background_tasks`，据此区分"在等背景任务"和"真干完了一轮"。
hook 的 stdout 平时沉默；唯一例外见下一节——`PostToolUse` 回注提醒时输出一个
`hookSpecificOutput` JSON。

## 上下文到线与换班

一个任务可以跨多班（多个窗口）跑完，全程不用人在场。

**到线提醒（回注）。** 每20次工具调用，hook 机器侧读一次 transcript 算上下文
水位。到警戒线（`guards.context_warn_tokens`，没写就按
`context_warn_ratio × 该模型 context_limit`，默认 0.8）时，hook 通过
`PostToolUse` 的 `additionalContext` 往会话里注一句提醒（模型像看到系统提示
一样看见它，不靠它自己自觉查）：

> [nightshift] 上下文已 412k / 500k，到警戒线了。现在收尾：①把已完成/未完成/
> 下一步写进 ~/.nightshift/tasks/<任务id>/handover-1.md，末行写 NEXT: continue
> 或 NEXT: done；②未提交的改动 commit；③然后停下，不要再开新的活。调度器会
> 按交接开下一班。

文案可在 `config.context_warn_text`（或任务级 `guards.context_warn_text`）里改。
额度到线（五小时或七日任一线碰到 `guards` 里的上限）时同样回注一段"请尽快
收尾"的额度提醒；两种提醒同时命中就合成一段。每过 20 次工具调用仍在线上就
再注一次，直到模型真的收尾。

**交接文件怎么写。** 就是一个普通 markdown，路径在提醒里给全
（`tasks/<任务id>/handover-<班次>.md`）。把"已完成 / 未完成 / 下一步"写清楚，
最后一行必须是调度器认的指令：

- `NEXT: continue` —— 活没干完，开下一班接着做；
- `NEXT: done` —— 干完了，任务完结。

**换班。** 模型收尾停下（Stop 且没有背景任务）后，调度器读到 idle 就看交接
文件：`continue` → 走一遍完整预检（额度不够就推迟，绝不硬起）→ 开下一班窗口，
提示词 = `chain_template` 渲染的"第 N 班 + 上一班交接"；`done` → 任务 finished。
会话在写完交接后崩了/被关了（exited）也一样认交接。

**没留交接怎么办（`chain.on_no_handover`）。** 这班收到过提醒却没写交接：

- `continue`（默认）——照常续班，提示词换成兜底文案"上一班没留交接，先看
  git log / git status / 项目里的验收单或 reports 目录判断进度"；
- `stop` —— 标 `needs_attention`，开"需要人工"窗口停下等人。

从没收到过提醒就 idle 的，视为正常干完 → `finished`。

**几班上限（`chain.max_windows`）。** 默认 3。到上限还要求续班的，任务标
`chain_exhausted` 并开"班次用尽"窗口。旧窗口一律保留不关，早上
`Ctrl+B w` 挨个看；网页卡片上能看到换班链（"已续班 → <后继id>" /
"上一班 <id>"）。

## 部署为 systemd 服务

调度器是常驻前台进程，用 systemd 托管：

1. 复制单元模板，改掉两处占位路径：
   ```bash
   cp deploy/nightshift.service.example /etc/systemd/system/nightshift.service
   ```
   - `WorkingDirectory=/path/to/nightshift` → 本仓库的绝对路径；
   - `Environment=NIGHTSHIFT_HOME=/root/.nightshift` → 你的数据目录
     （默认就是 `~/.nightshift` 的绝对路径）。
2. 启动并设开机自启：
   ```bash
   systemctl daemon-reload
   systemctl enable --now nightshift
   ```
3. 看调度日志：`~/.nightshift/scheduler.log`（2 MB × 3 轮转，stderr 同步一份）。

任务窗口是 tmux 的子进程而不是服务的子进程，所以 `systemctl restart nightshift`
不影响正在跑的任务。

不想常驻的话，`python3 -m nightshift serve --once` 跑一轮调度就退出，适合挂 cron
（不起网页）；只想跑调度不要网页，用 `python3 -m nightshift serve --no-http`。

## 网页

`serve` 默认在 `127.0.0.1:8190`（端口、监听地址、URL 前缀都在 `config.json` 的
`http` 段里改）同时跑调度循环和网页：任务列表 / 新建任务 / 模板编辑 / 屏幕快照，
手机上也能用。

- **首次打开**：还没设过口令时会自动跳到设置页，设一次口令（至少 8 个字符）。
  口令只能设这一次，散列连同签名密钥存 `~/.nightshift/auth.json`（0600），
  不进 `config.json`。
- **改口令**：在服务器上跑 `python3 -m nightshift passwd`，输入两遍即可覆盖；
  覆盖后旧的登录会话全部失效。
- **登录会话**：cookie（`ns_auth`）签发后一年有效，HttpOnly / SameSite=Lax /
  （https 下）Secure；登录接口还有进程内失败限速（同来源 15 分钟错 5 次即锁）。
- **放公网**：前面必须挡一层 nginx 反代，location 片段见
  `deploy/nginx-location.example.conf`（含登录路径限速；其中登录限速的 zone
  要在 nginx 的 `http {}` 层定义）。nginx 会剥掉 `/nightshift` 前缀，
  网页里的资源引用全是相对路径，直接照抄片段即可。
