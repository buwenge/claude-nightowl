# nightshift（夜班）

到点在 tmux 里开一个新窗口、跑一个**正常的交互式 `claude`** 的定时任务调度器。
她早上 ssh 进来 `Ctrl+B w` 选窗口就能接着聊；跑的时候靠 Claude Code 自己的
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
| `tmux_session` | 任务窗口开在哪个 tmux 会话里（她登录自动进的那个会话名） |
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
它读 stdin 的 JSON，只更新该任务自己的 `status.json`（文件锁 + 原子写），
stdout 永远沉默。`Stop` 事件带回 `background_tasks`，据此区分"在等背景任务"
和"真干完了一轮"。
