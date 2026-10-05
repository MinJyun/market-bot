#!/bin/zsh
# 每日排程入口（launchd 每 30 分鐘巡邏一次呼叫，不是固定時刻觸發）
#
# 時段由 tools_slot.py 以**台北時間**判斷，原因見該檔：機器時區會變（人在
# 國外）、機器睡著會錯過固定時刻。巡邏式讓睡過頭的時段醒來後補得回來。
set -u
cd "$(dirname "$0")"
export TZ=Asia/Taipei      # 程式內的日期/時窗/交易日判斷一律用台北時間

ts() { date +'%m-%d %H:%M:%S' }

# 沒有時段到期就安靜結束（每 30 分鐘會進來一次，不要洗 log）
SLOT=$(/usr/bin/python3 tools_slot.py due) || exit 0

# 剛喚醒時 DNS 常還沒就緒，整輪抓取會全軍覆沒（2026-10-03、10-05 兩次）。
# 此時**不記錄時段**直接退出，下一次巡邏（30 分鐘後）會重試同一個時段。
ok=0
for i in 1 2 3; do
    curl -s --max-time 10 -o /dev/null "https://www.twse.com.tw" && { ok=1; break; }
    sleep 10
done
if [ $ok -eq 0 ]; then
    echo "[$(ts)] [daily.sh] $SLOT 時段：網路未就緒，略過，下次巡邏重試"
    exit 0
fi

echo "[$(ts)] [daily.sh] ===== $SLOT 時段開始 ====="

# 整輪上限 20 分鐘：任何一處網路呼叫 hang 住都不能佔住 launchd 的 job
# （2026-09-18 那輪卡在 Google Sheets 14 小時，吃掉隔天早報）
TIMEOUT=1200
/usr/bin/python3 -u -W ignore main.py daily --catchup &
py_pid=$!
(
    sleep $TIMEOUT
    if kill -0 $py_pid 2>/dev/null; then
        echo "[$(ts)] [daily.sh] 逾時 ${TIMEOUT}s，中止本輪"
        kill $py_pid 2>/dev/null
        sleep 10
        kill -9 $py_pid 2>/dev/null
    fi
) &
watchdog_pid=$!
wait $py_pid
rc=$?
pkill -P $watchdog_pid 2>/dev/null   # 先殺它的 sleep，否則 sleep 會拖著 fd 活滿 TIMEOUT
kill $watchdog_pid 2>/dev/null

# 網路是通的（上面驗過），這輪就算有個別來源失敗也記為跑過，避免每 30
# 分鐘重抓一次撞限流；漏掉的資料下個時段或隔天的回補會補上
/usr/bin/python3 tools_slot.py done "$SLOT"

git add -A data reports
if ! git diff --cached --quiet; then
    git commit -q -m "daily snapshot $(date +%Y-%m-%d)"
    git push -q || echo "[$(ts)] [daily.sh] push 失敗，快照僅存本機"
fi

echo "[$(ts)] [daily.sh] ===== $SLOT 時段結束 rc=$rc ====="
exit $rc
