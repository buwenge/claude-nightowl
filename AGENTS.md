# nightshift（夜班）——施工守则

本仓库是一个独立的 Claude Code 定时任务调度器：到点在 tmux 里开一个交互式 `claude` 窗口跑任务，靠 CC 的 hook 回报状态。设计稿：`/root/CC/moving/自动工作流调度器设计与分阶段施工.md`（只读，不许改）。

## 硬规矩
- **Python 3.12 标准库 only**，不装任何第三方包，没有 venv。`python3` 就是 `/usr/bin/python3`。
- **代码/配置样例/注释里不许出现任何私人信息**：没有 `/root/xiaoyu`、没有人名代号、没有域名、没有 token。运行时一切个性化内容都从数据目录的 `config.json` 读（数据目录 = 环境变量 `NIGHTSHIFT_HOME`，默认 `~/.nightshift`）。仓库里只放 `config.example.json`。
- 数据目录里所有文件写盘一律"写临时文件 + `os.replace`"原子替换；`status.json` 的读改写要拿 `fcntl.flock` 文件锁（hook 进程和调度器会并发写）。
- **不许碰名为 `claude` 的 tmux 会话**（那是用户的），测试用的 tmux 会话只能叫 `ns-selftest`，用完 `tmux kill-session -t ns-selftest`。
- **不许真的起 `claude`**（花钱）。launcher 通过环境变量 `NIGHTSHIFT_CLAUDE_BIN` 换成 `tests/fake_claude.sh` 做集成测试。
- 测试：`cd /root/CC/nightshift && python3 -m pytest tests -q`，测试写盘一律指 `tmp_path`，`NIGHTSHIFT_HOME` 在测试里必须指向临时目录。
- 注释、docstring、验收单用中文；标识符英文。
- 不许 `git add -A`；不许 push；每个 commit 只包含开工令里说的那一部分。
