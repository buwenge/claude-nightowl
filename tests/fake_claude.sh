#!/bin/bash
# 假 claude：只给 nightshift 的集成测试用，绝不真起 claude（那是要花钱的）。
# 行为：从自己的参数里拿 --settings 与 --session-id，读 settings 里的 hook 配置，
# 依次触发 UserPromptSubmit / Stop / SessionEnd 三个事件的 hook 命令，
# stdin 分别喂 fixtures/ 下对应夹具的内容（session_id 换成参数里的），
# 每条之间 sleep 1，全部参数追加到 $NIGHTSHIFT_FAKE_LOG，最后退出码 0。
set -u

SESSION_ID=""
SETTINGS=""
PREV=""
for arg in "$@"; do
    case "$PREV" in
        --session-id) SESSION_ID="$arg" ;;
        --settings)   SETTINGS="$arg" ;;
    esac
    PREV="$arg"
done

if [ -n "${NIGHTSHIFT_FAKE_LOG:-}" ]; then
    printf '%s\n' "$@" >> "$NIGHTSHIFT_FAKE_LOG"
fi

FIXTURES="$(cd "$(dirname "$0")" && pwd)/fixtures"

run_hook() {  # $1=事件名  $2=夹具文件
    [ -n "$SETTINGS" ] || return 0
    python3 - "$SETTINGS" "$1" "$2" "$SESSION_ID" <<'PYEOF'
import json
import subprocess
import sys

settings_path, event, fixture_path, session_id = sys.argv[1:5]
with open(settings_path, encoding="utf-8") as f:
    settings = json.load(f)
command = settings["hooks"][event][0]["hooks"][0]["command"]
with open(fixture_path, encoding="utf-8") as f:
    payload = f.read().replace(
        "f5153209-d2a8-4c35-8d9a-fe6e604968d1", session_id
    )
subprocess.run(command, shell=True, input=payload, capture_output=True, text=True)
PYEOF
}

run_hook "UserPromptSubmit" "$FIXTURES/hook_userpromptsubmit.json"
sleep 1
run_hook "Stop" "$FIXTURES/hook_stop_idle.json"
sleep 1
run_hook "SessionEnd" "$FIXTURES/hook_sessionend.json"
exit 0
