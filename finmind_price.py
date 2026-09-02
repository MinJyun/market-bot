"""FinMind 個股日線回補(開高低收/量),存入 data/broker.db 的 price 表。

與分點資料同一個 DB,方便 join(下鑽面板要把分點買賣均價疊在股價上,
「券商損益」也需要收盤價才算得出來)。

量級與分點資料差 1,300 倍:這個資料集**一次請求可取一整年**(分點強制
單日),故 300 檔只要 300 次請求、約 5 MB。TaiwanStockPrice 是免費資料集,
上市與上櫃都涵蓋(實測穩懋、信驊皆可)。

增量設計:每檔查 price 表現有最後日期,只抓之後的區間;完全沒資料才從
START 抓。所以重複執行等於增量更新,不必另設狀態表。

用法:
    python3 finmind_price.py                    # 全部股票池,增量
    python3 finmind_price.py --rate 900         # 與其他回補並行時壓低速率
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
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
API = "https://api.finmindtrade.com/api/v4"
DATASET = "TaiwanStockPrice"
START = date(2025, 7, 28)          # 與分點資料同起點
DB = HERE / "data" / "broker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS price (
    data_date INTEGER NOT NULL,
    stock_id  TEXT NOT NULL,
    open  REAL, high REAL, low REAL, close REAL,
    spread REAL,
    volume INTEGER,          -- 成交股數
    amount INTEGER,          -- 成交金額(元)
    trans  INTEGER,          -- 成交筆數
    PRIMARY KEY (data_date, stock_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_price_stk ON price(stock_id, data_date);
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


def universe():
    """所有股票池檔的聯集,依代號排序。"""
    out = {}
    for f in sorted((HERE / "data").glob("*.json")):
        try:
            rows = json.loads(f.read_text())
        except Exception:
            continue
        if isinstance(rows, list):
            for d in rows:
                if isinstance(d, dict) and "stock_id" in d:
                    out.setdefault(d["stock_id"], d.get("name", ""))
    return sorted(out.items())


def fetch(token, sid, d0, d1, tries=4):
    q = urllib.parse.urlencode({"dataset": DATASET, "data_id": sid,
                                "start_date": d0.isoformat(),
                                "end_date": d1.isoformat(), "token": token})
    for i in range(tries):
        try:
            r = urllib.request.urlopen(f"{API}/data?{q}", timeout=90)
            j = json.loads(r.read())
            if j.get("status") != 200:
                raise RuntimeError(f"API {j.get('status')}: {j.get('msg')}")
            return j.get("data") or []
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("msg", "")
            except Exception:
                msg = ""
            if e.code < 500 and e.code != 429:      # 權限/參數錯,重試無用
                raise RuntimeError(f"HTTP {e.code} — {msg}") from None
            if i == tries - 1:
                raise RuntimeError(f"HTTP {e.code} — {msg}") from None
        except Exception as e:
            if i == tries - 1:
                raise RuntimeError(str(e)) from None
        time.sleep(2 ** i + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=3000,
                    help="每小時請求上限(與其他回補並行時壓低)")
    args = ap.parse_args()
    token = get_token()
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)

    stocks = universe()
    have = dict(conn.execute("SELECT stock_id, MAX(data_date) FROM price"
                             " GROUP BY stock_id"))
    today = date.today()
    interval = 3600.0 / args.rate
    print(f"股票池 {len(stocks)} 檔 | 上限 {args.rate:,}/小時"
          f" → 約 {len(stocks)*interval/60:.0f} 分鐘")

    n_ok = n_rows = 0
    for sid, name in stocks:
        last = have.get(sid)
        if last:
            d0 = date(last // 10000, last // 100 % 100, last % 100) + timedelta(days=1)
            if d0 > today:
                continue                      # 已是最新
        else:
            d0 = START
        t0 = time.time()
        try:
            rows = fetch(token, sid, d0, today)
        except Exception as e:
            print(f"  {sid} {name}: 失敗 — {e}")
            print("  中止(已完成部分可續跑)")
            break
        recs = [(int(x["date"].replace("-", "")), x["stock_id"],
                 x.get("open"), x.get("max"), x.get("min"), x.get("close"),
                 x.get("spread"), x.get("Trading_Volume"),
                 x.get("Trading_money"), x.get("Trading_turnover"))
                for x in rows]
        if recs:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO price VALUES (?,?,?,?,?,?,?,?,?,?)",
                    recs)
        n_ok += 1
        n_rows += len(recs)
        if n_ok % 50 == 0:
            print(f"  {n_ok}/{len(stocks)}  最新 {sid} {name}"
                  f"  累計 {n_rows:,} 列", flush=True)
        time.sleep(max(0, interval - (time.time() - t0)))
    total = conn.execute("SELECT COUNT(*) FROM price").fetchone()[0]
    print(f"\n本輪 {n_ok} 檔 / 新增 {n_rows:,} 列 | price 表共 {total:,} 列")


if __name__ == "__main__":
    main()
