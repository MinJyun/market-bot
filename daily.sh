#!/bin/zsh
# 每日排程入口：抓持股、產報告，有新資料就 commit 進 git（launchd 呼叫）
set -u
cd "$(dirname "$0")"

# 等網路就緒(最多 5 分鐘):機器剛喚醒 DNS 未就緒時,整輪抓取會全數失敗
# (2026-08-07 兩輪排程即因此全空轉)
for i in {1..10}; do
    curl -s --max-time 5 -o /dev/null "https://www.twse.com.tw" && break
    echo "[daily.sh] 網路未就緒,30 秒後重試($i/10)"
    sleep 30
done

# 整輪上限 20 分鐘:任何一處網路呼叫 hang 住都不能佔住 launchd 的 job,
# 否則同 label 的後續排程會被整個跳過
# (2026-09-18 21:41 那輪卡在 Google Sheets 14 小時,吃掉 09-19 早報)
# -u 讓輸出即時落 log;卡住時才看得出進度到哪
TIMEOUT=1200
/usr/bin/python3 -u -W ignore main.py daily &
py_pid=$!
(
    sleep $TIMEOUT
    if kill -0 $py_pid 2>/dev/null; then
        echo "[daily.sh] 逾時 ${TIMEOUT}s,中止本輪"
        kill $py_pid 2>/dev/null
        sleep 10
        kill -9 $py_pid 2>/dev/null
    fi
) &
watchdog_pid=$!
wait $py_pid
rc=$?
pkill -P $watchdog_pid 2>/dev/null   # 先殺它的 sleep,否則 sleep 會拖著 fd 活滿 TIMEOUT
kill $watchdog_pid 2>/dev/null

git add -A data reports
if ! git diff --cached --quiet; then
    git commit -q -m "daily snapshot $(date +%Y-%m-%d)"
    git push -q || echo "push 失敗，快照僅存本機"
fi

exit $rc
