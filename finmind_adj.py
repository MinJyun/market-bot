"""公司行動的**股數**還原係數,存入 data/broker.db 的 stock_adj 表。

為什麼需要:分點資料的 buy_sh/buy_vwap 是當時的原始股數與原始價格。跨過
除權、分割、減資之後,舊的股數與新的收盤價不在同一個基礎上,直接相減算
損益會錯得離譜 —— 2026-09-02 緯穎(6669)配股票股利 19.83 元(1 股變
2.9828 股),摩根士丹利的面板一度顯示未實現 +51.57 億,實際上那是把分割前
的買均價 5302 拿去和分割後的收盤價 2565 相減。

**只還原股數,不還原股價**:現金股息也會讓還原股價變動,但它不改變股數,
把兩者混在一個係數裡就分不開了(adj_close/close 是兩者的乘積)。忽略現金
股息的效果是損益以「價格報酬」計、不含股息,誤差是個位數百分比且方向保守;
把股票股利當成價格變動則會造成數倍的錯誤,兩者嚴重性差很多。

三種會改變股數的事件:
- **股票股利**(TaiwanStockDividend):q = 1 + (盈餘配股 + 公積配股)/10。
  面額 10 元,配 19.827946 元 → 每股配 1.9828 股 → q = 2.9828(與從 ETF
  持股反推出的係數完全相同,已交叉驗證)。
- **面額變更/分割**(TaiwanStockSplitPrice):q = before_price / after_price。
- **減資**(TaiwanStockCapitalReductionReferencePrice):
  q = 最後交易日收盤 / 減資後參考價(股數變少,q < 1)。

用法:
    python3 finmind_adj.py            # 全股票池,自 2025-01-01 起的事件
    python3 finmind_adj.py --since 2024-01-01
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
API = "https://api.finmindtrade.com/api/v4"
DB = HERE / "data" / "broker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_adj (
    stock_id TEXT NOT NULL,
    ex_date  INTEGER NOT NULL,   -- YYYYMMDD;當日起已是新股數基礎
    kind     TEXT NOT NULL,      -- stock_dividend / split / reduction
    q        REAL NOT NULL,      -- 1 股變成 q 股
    note     TEXT,
    PRIMARY KEY (stock_id, ex_date, kind)
);
"""


def get_token():
    tok = os.environ.get("FINMIND_TOKEN", "").strip()
    env = HERE / ".env"
    if not tok and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "FINMIND_TOKEN":
                tok = v.strip().strip('"').strip("'")
                break
    if not tok:
        sys.exit("缺少 FINMIND_TOKEN(見 .env.example)")
    print(f"[auth] token 尾碼 …{tok[-6:]}")
    return tok


def fetch(token, dataset, tries=4, **kw):
    p = {"dataset": dataset, "token": token}
    p.update(kw)
    q = urllib.parse.urlencode(p)
    for i in range(tries):
        try:
            r = urllib.request.urlopen(f"{API}/data?{q}", timeout=120)
            j = json.loads(r.read())
            if j.get("status") != 200:
                raise RuntimeError(f"API {j.get('status')}: {j.get('msg')}")
            return j.get("data") or []
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise RuntimeError(f"HTTP {e.code}") from None
            if i == tries - 1:
                raise RuntimeError(f"HTTP {e.code}") from None
        except Exception as e:
            if i == tries - 1:
                raise RuntimeError(str(e)) from None
        time.sleep(2 ** i + 1)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_daily(conn):
    """把事件展開成每日累積係數表 adj_daily,供 SQL 直接 join。

    Q(股票, d) = ∏{ q : ex_date > d } —— 除權當天的成交價已經是新基礎,
    所以只乘「該日之後」的事件。**只存 q != 1 的列**:98 筆事件只影響
    少數股票的少數區段,全展開是 300 檔 × 280 天的浪費。
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS adj_daily (
            stock_id  TEXT NOT NULL,
            data_date INTEGER NOT NULL,
            q         REAL NOT NULL,   -- 該日的 1 股 = 今日的 q 股
            PRIMARY KEY (stock_id, data_date)
        ) WITHOUT ROWID;
    """)
    ev = {}
    for sid, ex, q in conn.execute(
            "SELECT stock_id, ex_date, q FROM stock_adj ORDER BY ex_date"):
        ev.setdefault(sid, []).append((ex, q))
    rows = []
    for sid, evs in ev.items():
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT data_date FROM broker_daily WHERE stock_id=?",
            (sid,))]
        for d in dates:
            f = 1.0
            for ex, q in evs:
                if ex > d:
                    f *= q
            if abs(f - 1.0) > 1e-9:
                rows.append((sid, d, f))
    with conn:
        conn.execute("DELETE FROM adj_daily")
        conn.executemany("INSERT INTO adj_daily VALUES (?,?,?)", rows)
    n = len({r[0] for r in rows})
    print(f"[adj] adj_daily:{len(rows):,} 列,影響 {n} 檔股票")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-01-01", help="只取此日之後的事件")
    ap.add_argument("--rate", type=int, default=3000, help="每小時請求上限")
    args = ap.parse_args()
    token = get_token()
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)

    stocks = [r[0] for r in conn.execute(
        "SELECT DISTINCT stock_id FROM fetched ORDER BY stock_id")]
    rows = []

    # 1) 面額變更/分割 與 減資:支援不帶 data_id 的區間查詢,各一次請求
    for rec in fetch(token, "TaiwanStockSplitPrice", start_date=args.since):
        before, after = _num(rec.get("before_price")), _num(rec.get("after_price"))
        if before and after:
            rows.append((rec["stock_id"], int(rec["date"].replace("-", "")),
                         "split", before / after, rec.get("type") or ""))
    time.sleep(1)
    for rec in fetch(token, "TaiwanStockCapitalReductionReferencePrice",
                     start_date=args.since):
        last = _num(rec.get("ClosingPriceonTheLastTradingDay"))
        post = _num(rec.get("PostReductionReferencePrice"))
        if last and post:
            rows.append((rec["stock_id"], int(rec["date"].replace("-", "")),
                         "reduction", last / post,
                         rec.get("ReasonforCapitalReduction") or ""))
    print(f"[adj] 分割 + 減資:{len(rows)} 筆")

    # 2) 股票股利:市場層級查詢只回 20 筆(實測),必須逐檔查
    interval = 3600.0 / args.rate
    n_div = 0
    for i, sid in enumerate(stocks, 1):
        t0 = time.time()
        try:
            recs = fetch(token, "TaiwanStockDividend", data_id=sid,
                         start_date=args.since)
        except Exception as e:
            print(f"  {sid}: 失敗 — {e}")
            recs = []
        for rec in recs:
            stock = _num(rec.get("StockEarningsDistribution")) + \
                    _num(rec.get("StockStatutorySurplus"))
            ex = rec.get("StockExDividendTradingDate") or ""
            if stock > 0 and len(ex) == 10:
                # 面額 10 元:配發 X 元 → 每股多拿 X/10 股
                rows.append((sid, int(ex.replace("-", "")), "stock_dividend",
                             1 + stock / 10, f"股票股利 {stock:g} 元"))
                n_div += 1
        if i % 50 == 0:
            print(f"  {i}/{len(stocks)} 檔,累計股票股利 {n_div} 筆", flush=True)
        time.sleep(max(0, interval - (time.time() - t0)))

    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_adj VALUES (?,?,?,?,?)", rows)
    build_daily(conn)
    total = conn.execute("SELECT COUNT(*) FROM stock_adj").fetchone()[0]
    print(f"\n本輪 {len(rows)} 筆(股票股利 {n_div})| stock_adj 共 {total} 筆")
    for r in conn.execute(
            "SELECT stock_id, ex_date, kind, ROUND(q,4), note FROM stock_adj"
            " ORDER BY ex_date DESC LIMIT 10"):
        print("   ", r)


if __name__ == "__main__":
    main()
