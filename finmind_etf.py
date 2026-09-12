"""FinMind 主動式 ETF 每日持股與異動,存入 data/broker.db。

**為何放 broker.db 而不是 market.db**:market.db 在版控裡,daily.sh 每天
`git add -A data && git push` 到**公開** GitHub(MinJyun/market-bot)。FinMind
是授權資料,寫進 market.db 等於公開散布。broker.db 已列入 .gitignore。
sources/active_etf.py 抓的投信公開揭露資料不受此限,繼續留在 market.db。

**與 sources/active_etf.py 的分工**(不是主/備,是三塊不同的資料):
- 持股股數與異動 → 本檔(FinMind)。廣度 40 檔 vs 7 檔,深度各檔上市日起
  vs 30~40 天,且元大/群益的歷史投信 API 根本不給(只回最新一日)。
- 個股市值 → 統一/復華 FinMind 有;**元大/群益 FinMind 回 0**,只能用
  active_etf 的「淨資產×權重」估算。
- 淨值 / 流通單位數 / 淨資產 → **只有 active_etf 有**,FinMind 沒有這個
  資料集。判讀「主動加碼」與「規模擴張被動買進」的差別靠它。

**API 的坑**:給了 start_date 卻不給 end_date 時,回應會包含該日之後的資料。
用 {component_stock_id: row} 去重會靜默拿到錯誤的一天(2026-09-03 就這樣
誤判成 FinMind 日期偏移)。所以本檔一律 start_date == end_date 成對送出。

效率:不帶 data_id 時**一次請求回該日全市場所有 ETF**,所以請求數 = 交易
日數 × 2 個資料集,回補一年約 660 次,幾分鐘就跑完。

用法:
    python3 finmind_etf.py                  # 增量(自 DB 最後日期起)
    python3 finmind_etf.py --days 500       # 回補近 500 個日曆日
    python3 finmind_etf.py --all            # 從 2025-05-05(資料集起點)全補
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
START = date(2025, 5, 5)           # 資料集起點(00980A 上市日)
DB = HERE / "data" / "broker.db"
# 近幾日無條件重抓:當日資料傍晚才齊,白天跑會拿到部分或空的。與
# daily_broker.sh 的兩輪制同一個思路 —— 不猜「多少列算完整」,直接覆蓋。
#
# 為何要 10 天(每天只 2 次請求,很便宜):
# 1. **海外型與債券型慢兩個交易日**。2026-09-04 實測:34 檔裡有 13 檔
#    (00402A/00988A/00989A/00990A/00997A/00983A 等海外型 + 00980D~00986D
#    債券型)最新資料日還停在 09-02,其餘 21 檔已有 09-04。
# 2. **這裡數的是日曆日,不是交易日**。原本設 3,週一跑的視窗是
#    週一/週日/週六 —— 只含一個交易日,上週五若當時揭露不全就永遠補不到。
REFETCH_DAYS = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS etf_info (
    etf        TEXT PRIMARY KEY,
    name       TEXT,
    category   TEXT,          -- domestic / foreign
    market     TEXT,          -- twse / tpex
    updated    TEXT
);
CREATE TABLE IF NOT EXISTS etf_holding (
    data_date    INTEGER NOT NULL,   -- YYYYMMDD,與 price/broker_daily 同慣例
    etf          TEXT NOT NULL,
    code         TEXT NOT NULL,      -- 成分代號;非個股者是中文名(如債券附買回)
    name         TEXT,
    asset_type   TEXT,               -- stock/bond/futures/repo/cash/etf/option/other
    shares       REAL,               -- 期貨為口數
    weight       REAL,               -- 佔淨值 %
    market_value REAL,               -- 元大/群益回 0(投信不揭露)
    currency     TEXT,               -- ''/TWD/USD
    PRIMARY KEY (data_date, etf, code)
) WITHOUT ROWID;
-- 反向查「某檔股票被哪些 ETF 持有」:跨 ETF 共識流向的主查詢
CREATE INDEX IF NOT EXISTS idx_etf_hold_code ON etf_holding(code, data_date);
CREATE TABLE IF NOT EXISTS etf_change (
    data_date INTEGER NOT NULL,
    etf       TEXT NOT NULL,
    code      TEXT NOT NULL,
    name      TEXT,
    buy       REAL,
    sell      REAL,
    PRIMARY KEY (data_date, etf, code)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_etf_chg_code ON etf_change(code, data_date);
-- 已抓日期(含 0 列的休市日),避免重複打;近 REFETCH_DAYS 天不看這張表
CREATE TABLE IF NOT EXISTS etf_fetched (
    data_date    INTEGER PRIMARY KEY,
    holding_rows INTEGER,
    change_rows  INTEGER
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


def fetch(token, dataset, d=None, tries=4):
    """單日查詢。d 為 None 時不帶日期參數(給 ETF 清單用)。"""
    p = {"dataset": dataset, "token": token}
    if d is not None:
        # start 與 end 必須成對,否則回應會夾帶之後的日期(見模組說明)
        p["start_date"] = p["end_date"] = d.isoformat()
    q = urllib.parse.urlencode(p)
    for i in range(tries):
        try:
            r = urllib.request.urlopen(f"{API}/data?{q}", timeout=120)
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


def save_info(conn, token):
    rows = fetch(token, "TaiwanStockActiveETFInfo")
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO etf_info VALUES (?,?,?,?,?)",
            [(r["stock_id"], r.get("stock_name"), r.get("category"),
              r.get("type"), r.get("date")) for r in rows])
    print(f"[info] 主動式 ETF 清單 {len(rows)} 檔")
    return len(rows)


def save_day(conn, token, d):
    dd = int(f"{d:%Y%m%d}")
    hold = fetch(token, "TaiwanStockActiveETFHolding", d)
    chg = fetch(token, "TaiwanStockActiveETFHoldingChange", d)
    with conn:
        # 重抓日先清空該日,免得 ETF 清單縮減時留下前一輪的殘列
        conn.execute("DELETE FROM etf_holding WHERE data_date=?", (dd,))
        conn.execute("DELETE FROM etf_change  WHERE data_date=?", (dd,))
        conn.executemany(
            "INSERT INTO etf_holding VALUES (?,?,?,?,?,?,?,?,?)",
            [(dd, r["stock_id"], str(r["component_stock_id"]).strip(),
              (r.get("component_stock_name") or "").strip(),
              r.get("asset_type"), r.get("shares"), r.get("weight"),
              r.get("market_value"), r.get("currency")) for r in hold])
        conn.executemany(
            "INSERT INTO etf_change VALUES (?,?,?,?,?,?)",
            [(dd, r["stock_id"], str(r["component_stock_id"]).strip(),
              (r.get("component_stock_name") or "").strip(),
              r.get("buy"), r.get("sell")) for r in chg])
        conn.execute("INSERT OR REPLACE INTO etf_fetched VALUES (?,?,?)",
                     (dd, len(hold), len(chg)))
    return len(hold), len(chg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int,
                    help="回補近 N 個日曆日(預設:自 DB 最後日期起增量)")
    ap.add_argument("--all", action="store_true",
                    help=f"從資料集起點 {START} 全補")
    ap.add_argument("--rate", type=int, default=1200,
                    help="每小時請求上限(每個交易日耗 2 次)")
    args = ap.parse_args()
    token = get_token()
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    save_info(conn, token)

    today = date.today()
    rf_date = today - timedelta(days=REFETCH_DAYS - 1)
    if args.all:
        start = START
    elif args.days:
        start = today - timedelta(days=args.days)
    else:
        last = conn.execute("SELECT MAX(data_date) FROM etf_fetched").fetchone()[0]
        start = (date(last // 10000, last // 100 % 100, last % 100)
                 if last else START)
        # 起點要退到重抓視窗前緣,否則視窗根本走不到:增量的 start 是
        # 「DB 最後日期」,而那通常就是今天,迴圈只跑一天,REFETCH_DAYS
        # 形同無效(實測只回報「待抓 1 個交易日」)。
        start = min(start, rf_date)
    refetch_from = int(f"{rf_date:%Y%m%d}")
    done = {dd for (dd,) in conn.execute("SELECT data_date FROM etf_fetched")
            if dd < refetch_from}
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    todo = [d for d in days
            if d.weekday() < 5 and int(f"{d:%Y%m%d}") not in done]
    interval = 3600.0 / args.rate * 2          # 每個交易日打 2 次請求
    print(f"待抓 {len(todo)} 個交易日({start} ~ {today})"
          f" | 上限 {args.rate:,}/小時 → 約 {len(todo)*interval/60:.0f} 分鐘"
          f" | 近 {REFETCH_DAYS} 日重抓")

    n_ok = n_hold = n_chg = 0
    for d in todo:
        t0 = time.time()
        try:
            h, c = save_day(conn, token, d)
        except Exception as e:
            print(f"  {d}: 失敗 — {e}")
            print("  中止(已完成部分可續跑)")
            break
        n_ok += 1
        n_hold += h
        n_chg += c
        if n_ok % 20 == 0 or h == 0:
            tag = "(休市)" if h == 0 else ""
            print(f"  {n_ok}/{len(todo)}  {d} 持股 {h} 異動 {c} {tag}"
                  f"  累計 {n_hold:,}/{n_chg:,} 列", flush=True)
        time.sleep(max(0, interval - (time.time() - t0)))
    th = conn.execute("SELECT COUNT(*) FROM etf_holding").fetchone()[0]
    tc = conn.execute("SELECT COUNT(*) FROM etf_change").fetchone()[0]
    print(f"\n本輪 {n_ok} 天 / 新增 {n_hold:,} 持股 + {n_chg:,} 異動 | "
          f"DB 共 {th:,} + {tc:,} 列")


if __name__ == "__main__":
    main()
