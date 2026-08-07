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

/usr/bin/python3 -W ignore main.py daily
rc=$?

git add -A data reports
if ! git diff --cached --quiet; then
    git commit -q -m "daily snapshot $(date +%Y-%m-%d)"
    git push -q || echo "push 失敗，快照僅存本機"
fi

exit $rc
