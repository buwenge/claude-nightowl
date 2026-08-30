#!/bin/bash
# 假 codex：只给 nightshift 的集成测试用，绝不真起 codex（那是要花钱的）。
# Codex 的七件 hooks 走固定的用户级 hooks.json，task id 全靠 NIGHTOWL_TASK_ID
# 环境变量路由（run.sh 已 export，本脚本原样继承）——跟 fake_claude.sh 不同，
# 不需要读 --settings，直接依次触发 SessionStart / UserPromptSubmit /
# PostToolUse / Stop / SessionEnd，每条之间 sleep 1；resume 场景（argv 里带
# "resume"）跳过 SessionStart（真机实测续班不重新触发它）。
# 全部参数追加到 $NIGHTSHIFT_FAKE_LOG，最后退出码 0。
set -u

if [ -n "${NIGHTSHIFT_FAKE_LOG:-}" ]; then
    printf '%s\n' "$@" >> "$NIGHTSHIFT_FAKE_LOG"
fi

FIXTURES="$(cd "$(dirname "$0")" && pwd)/fixtures"
IS_RESUME=0
for arg in "$@"; do
    if [ "$arg" = "resume" ]; then
        IS_RESUME=1
    fi
done

run_hook() {  # $1=事件名 $2=夹具文件
    python3 -m nightshift.hook --codex "$1" < "$2"
}

if [ "$IS_RESUME" -eq 0 ]; then
    run_hook "SessionStart" "$FIXTURES/codex_hook_sessionstart.json"
    sleep 1
fi
run_hook "UserPromptSubmit" "$FIXTURES/codex_hook_userpromptsubmit.json"
sleep 1
run_hook "PostToolUse" "$FIXTURES/codex_hook_posttooluse.json"
sleep 1
run_hook "Stop" "$FIXTURES/codex_hook_stop.json"
sleep 1
run_hook "SessionEnd" "$FIXTURES/codex_hook_sessionend.json"
exit 0
