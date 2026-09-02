"""重建分點彙總表 broker_totals。

api/brokers 要列出每個分點的總買賣,對 broker_daily 全表 GROUP BY 在
1,850 萬列時要 40 秒以上(擴到 200 檔會翻倍),網頁等於卡死。改成離線
算好存表,查詢就變成掃幾百列。

回補完成後跑一次即可;回補進行中跑會得到當下快照(不影響正確性,只是
數字略舊)。

用法:  python3 tools_summary.py
"""
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).parent / "data" / "broker.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_totals (
    bno      TEXT PRIMARY KEY,
    buy      REAL,      -- 億元
    sell     REAL,
    days     INTEGER,
    stocks   INTEGER
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta_kv (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
"""


def rebuild(conn):
    """在 Python 端串流累加,不用 SQL 的 GROUP BY。

    SQL 的 GROUP BY bno 需對 3,390 萬列排序(表是以 (date,stock,bno) 叢集,
    bno 不是前綴),實測跑 23 分鐘仍未完成、大量暫存 I/O。改成依主鍵順序
    串流讀取、在記憶體用 dict 累加 —— 分點只有 900 個,記憶體微不足道。
    """
    t0 = time.time()
    conn.executescript(SCHEMA)
    acc = {}          # bno -> [買金額, 賣金額, {日期}, {股票}]
    n = 0
    for bno, bsh, ssh, bv, sv, dd, sid in conn.execute(
            "SELECT bno, buy_sh, sell_sh, buy_vwap, sell_vwap, data_date,"
            " stock_id FROM broker_daily"):
        a = acc.get(bno)
        if a is None:
            a = acc[bno] = [0.0, 0.0, set(), set()]
        if bsh and bv:
            a[0] += bsh * bv
        if ssh and sv:
            a[1] += ssh * sv
        a[2].add(dd); a[3].add(sid)
        n += 1
        if n % 5_000_000 == 0:
            print(f"  已讀 {n:,} 列…({time.time()-t0:.0f} 秒)", flush=True)
    rows = [(b, v[0] / 1e8, v[1] / 1e8, len(v[2]), len(v[3]))
            for b, v in acc.items()]
    with conn:
        conn.execute("DELETE FROM broker_totals")
        conn.executemany("INSERT INTO broker_totals VALUES (?,?,?,?,?)", rows)
        conn.execute("INSERT OR REPLACE INTO meta_kv VALUES ('totals_built',?)",
                     (time.strftime("%Y-%m-%d %H:%M:%S"),))
    print(f"broker_totals:{len(rows)} 個分點,耗時 {time.time()-t0:.1f} 秒")


if __name__ == "__main__":
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    rebuild(c)
