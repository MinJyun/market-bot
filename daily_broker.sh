#!/bin/zsh
# 分點資料每日增量:抓當日 300 檔 → 重建彙總表。由 launchd 呼叫,分兩輪:
#
#   daily_broker.sh early   18:30  搶先抓,讓網頁提前三小時看到當日資料
#   daily_broker.sh final   21:35  無條件重抓近兩日覆蓋(預設)
#
# 為何要兩輪:FinMind 文件說當日資料 21:00 更新,但 2026-09-03 實測 18:28 就已
# 定版 —— 手測的 2330/2317/3105/2382 買進股數與券商數,與當晚 21:35 排程入庫的
# 結果完全相同。既然如此就早點抓,但白天搶先抓有拿到「部分資料」的風險,而
# finmind_backfill.py 只對「0 列」不記為完成,rows>0 就永久跳過。
#
# 為何不設完整性門檻:分點買進佔官方成交量實測 59%~100% 都是正常值
# (鉅額交易未歸屬到分點,見 2026-09-01 的 2330),任何門檻都會誤判。
# 所以第二輪用 --force-days 2 無條件覆蓋,不猜門檻。涵蓋兩日是為了萬一
# 第二輪沒跑到(機器睡著),隔天第一輪也不會把昨天的殘缺資料留下來。
set -u
cd "$(dirname "$0")"
PY=/usr/bin/python3

ROUND=${1:-final}
if [[ "$ROUND" == "early" ]]; then
    FORCE=()
else
    FORCE=(--force-days 2)
fi

echo "[$(date '+%F %T')] === 分點增量開始($ROUND) ==="
# --days 3 涵蓋前兩天:補上因休市誤判或當時尚未更新而漏掉的日期
$PY -u finmind_backfill.py --universe top200.json --days 3 $FORCE
$PY -u finmind_backfill.py --universe otc100.json  --days 3 $FORCE
# price 不重抓:日線收盤即定版(2026-09-03 18:28 手測的成交量與 21:48 入庫一致)
$PY -u finmind_price.py --rate 3000
# 主動式 ETF 持股/異動:自帶「近 3 日重抓」,兩輪都跑即可,不必分 early/final
$PY -u finmind_etf.py
# 公司行動的股數還原係數。只在 final 輪跑:除權事件不會盤中新增,而它要逐檔
# 打 300 次請求。沒更新的後果很嚴重 —— 新的配股沒進表,跨過除權的損益會
# 差好幾倍(緯穎 2026-09-02 配股 1 股變 2.98 股,未實現一度虛報 +51.57 億)。
if [[ "$ROUND" != "early" ]]; then
    $PY -u finmind_adj.py --since 2025-01-01
fi

# 彙總表是 api/brokers 的資料來源,不重建的話分點排行會停在舊快照
$PY -u tools_summary.py
echo "[$(date '+%F %T')] === 完成($ROUND) ==="
