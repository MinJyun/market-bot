#!/bin/zsh
# 分點資料每日增量:抓當日 300 檔 → 重建彙總表。由 launchd 於 21:35 呼叫。
#
# 為何 21:35:FinMind 週一至五 21:00 更新分點資料,提早跑會拿到空資料。
# finmind_backfill.py 對「近 7 天內抓到 0 列」的日期不記為完成,故萬一
# 提早跑了,隔天會自動重試,不會永久跳過。
set -u
cd "$(dirname "$0")"
PY=/usr/bin/python3

echo "[$(date '+%F %T')] === 分點增量開始 ==="
# --days 3 涵蓋前兩天:補上因休市誤判或當時尚未更新而漏掉的日期
$PY -u finmind_backfill.py --universe top200.json --days 3
$PY -u finmind_backfill.py --universe otc100.json  --days 3
$PY -u finmind_price.py --rate 3000

# 彙總表是 api/brokers 的資料來源,不重建的話分點排行會停在舊快照
$PY -u tools_summary.py
echo "[$(date '+%F %T')] === 完成 ==="
