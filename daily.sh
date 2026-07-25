#!/bin/zsh
# 每日排程入口：抓持股、產報告，有新資料就 commit 進 git（launchd 呼叫）
set -u
cd "$(dirname "$0")"

/usr/bin/python3 -W ignore main.py daily
rc=$?

git add -A data reports
if ! git diff --cached --quiet; then
    git commit -q -m "daily snapshot $(date +%Y-%m-%d)"
    git push -q || echo "push 失敗，快照僅存本機"
fi

exit $rc
