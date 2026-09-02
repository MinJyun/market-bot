"""FinMind 分點資料回補(一次性,一年約 5 小時)。

查詢維度:**按 (股票, 單日) 查**。文件寫可用 securities_trader_id 查某分點
當天的全部股票,實測不行 — API 強制要 data_id(股票)與 start_date,且
end_date 給區間會回 400,只能逐日。所以請求數 = 股票數 × 交易日數。

一次請求回該股當天「全部」分點(台積電約 814 個、逐價位 20,351 列),
所以反向索引(某分點在這些股票買了什麼)照樣建得起來,只是要付出
股票數 × 天數 的請求量。

原始資料逐價位,入庫時聚合成 (日期, 股票, 分點) → 總買量/總賣量/
買賣加權均價(實測壓縮比約 10.8:1)。

容量:一次請求約 800 個分點,100 檔 × 一年約 20M 列 ≈ 1.6 GB
(本機僅餘 35 GB,五年約 8 GB 尚可但要留意)。MIN_SHARES 預設 0 全留,
理由見該常數註解。

認證:token 放 .env 的 FINMIND_TOKEN(網頁帳戶頁取得)。.env 已列入
.gitignore — daily.sh 會 git add -A data 並 push 到公開 GitHub,token
不能進版控。環境變數同名者優先,方便臨時覆蓋。

用法(斷點續傳:中斷後直接再跑即接續):
    python3 finmind_backfill.py --days 400          # 近約 13 個月
    python3 finmind_backfill.py                     # 回到 2021-06-30
    python3 finmind_backfill.py --stocks 30         # 只前 30 檔
"""
import argparse, json, os, sqlite3, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ENV = HERE / ".env"
API = "https://api.finmindtrade.com/api/v4"
DATASET = "TaiwanStockTradingDailyReport"
START = date(2021, 6, 30)          # FinMind 該資料集起始日
# 瓶頸是請求延遲(實測約 1.0 秒/次)而非流量上限:單執行緒只跑到 1,980/小時,
# 額度卻有 6,000。故用少量併發把吞吐拉到上限的八成,由 RATE_PER_HOUR 節流,
# 不是靠 sleep 猜。
WORKERS = 3
RATE_PER_HOUR = 4800               # 上限 6,000,留兩成安全邊際

# 入庫門檻預設 0(全留)。原本設 5000 股想省空間,實測對高成交股幾乎不過濾
# (國巨 808/814 列都過關,因為每個分點至少成交 5 張),省不到 1% 卻讓非重點
# 分點的歷史出現缺漏 —— 做流向累計會低估。要省空間請改用金額門檻而非股數。
MIN_SHARES = 0
# 重點分點:不論量多小都保留 —— 散戶型分點(如土城永寧)本身就是小額,
# 純套門檻會把要研究的對象濾掉。
KEEP_BROKERS = {
    "9268": "凱基-台北", "9800": "元大", "5850": "統一", "9600": "富邦",
    "9875": "元大-土城永寧", "9216": "凱基-信義", "9217": "凱基-松山",
    "9661": "富邦-新店", "8440": "摩根大通", "1470": "摩根士丹利",
    "1650": "新加坡商瑞銀", "1480": "美商高盛", "1440": "美林",
}

DB = HERE / "data" / "broker.db"   # 獨立於 market.db:量級大、可重抓,不進版控
SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_daily (
    data_date  INTEGER NOT NULL,    -- YYYYMMDD
    stock_id   TEXT NOT NULL,
    bno        TEXT NOT NULL,       -- securities_trader_id
    buy_sh     INTEGER,             -- 買進股數
    sell_sh    INTEGER,             -- 賣出股數
    buy_vwap   REAL,                -- 買進加權均價
    sell_vwap  REAL,
    PRIMARY KEY (data_date, stock_id, bno)
-- WITHOUT ROWID:複合主鍵直接當叢集索引,省掉 rowid 與重複的 PK 索引。
-- 實測 50 萬列下 107.5 → 83.5 bytes/列(-22%),列小(~50B)故無反效果。
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bno  ON broker_daily(bno, data_date);
CREATE INDEX IF NOT EXISTS idx_stk  ON broker_daily(stock_id, data_date);
-- 記錄已抓過的 (股票, 日期),含「該日無資料」(假日/停牌)以免重複打
CREATE TABLE IF NOT EXISTS fetched (
    stock_id TEXT NOT NULL, data_date INTEGER NOT NULL, rows INTEGER,
    PRIMARY KEY (stock_id, data_date)
);
"""


def get_token():
    """環境變數優先,其次 .env 的 FINMIND_TOKEN。"""
    tok = os.environ.get("FINMIND_TOKEN", "").strip()
    if not tok and ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "FINMIND_TOKEN":
                tok = v.strip().strip('"').strip("'")
                break
    if not tok:
        sys.exit("缺少 FINMIND_TOKEN:複製 .env.example 為 .env 並填入 token")
    print(f"[auth] token 尾碼 …{tok[-6:]}")
    return tok


class Fatal(RuntimeError):
    """不該重試的錯誤(token 無效、會員等級不足、參數錯)。"""


def _once(token, stock_id, d):
    q = urllib.parse.urlencode({"dataset": DATASET, "data_id": stock_id,
                                "start_date": d.isoformat(), "token": token})
    try:
        r = urllib.request.urlopen(f"{API}/data?{q}", timeout=90)
    except urllib.error.HTTPError as e:
        # 400 的真正原因只在回應內容裡,urllib 遇 4xx 會丟掉 body,手動讀出。
        try:
            msg = json.loads(e.read()).get("msg", "")
        except Exception:
            msg = ""
        text = f"HTTP {e.code}{(' — ' + msg) if msg else ''}"
        # 429/5xx 是暫時性;其餘 4xx(尤其 400 的權限與參數問題)重試無意義
        if e.code == 429 or e.code >= 500:
            raise RuntimeError(text) from None
        raise Fatal(text) from None
    j = json.loads(r.read())
    if j.get("status") != 200:
        raise Fatal(f"API 錯誤 {j.get('status')}: {j.get('msg')}")
    return j.get("data") or []


def fetch_day(token, stock_id, d, tries=5):
    """回傳該股該日的逐價位列(含全部分點);空 list 表示無資料(假日/停牌)。

    暫時性失敗(讀取逾時、連線中斷、429/5xx)指數退避重試 —— 一次網路抖動
    不該讓數小時的回補整批中止(2026-09-01 就是這樣掉在 30%)。
    """
    for i in range(tries):
        try:
            return _once(token, stock_id, d)
        except Fatal:
            raise
        except Exception as e:
            if i == tries - 1:
                raise RuntimeError(f"重試 {tries} 次仍失敗:{e}") from None
            time.sleep(2 ** i + 1)          # 2, 3, 5, 9 秒


def aggregate(raw, min_shares=0):
    """逐價位 → (股票, 分點) 聚合。買賣各自加權均價。

    min_shares > 0 時,非 KEEP_BROKERS 的分點須 買+賣 ≥ 門檻才保留。
    """
    acc = defaultdict(lambda: [0, 0, 0.0, 0.0])   # buy_sh, sell_sh, buy_amt, sell_amt
    for r in raw:
        k = (r["stock_id"], r["securities_trader_id"])
        p = float(r.get("price") or 0)
        b = int(r.get("buy") or 0)
        s = int(r.get("sell") or 0)
        a = acc[k]
        a[0] += b; a[1] += s; a[2] += p * b; a[3] += p * s
    out = []
    for (sid, bno), (b, s, ba, sa) in acc.items():
        if min_shares and bno not in KEEP_BROKERS and b + s < min_shares:
            continue
        out.append((sid, bno, b, s,
                    round(ba / b, 4) if b else None,
                    round(sa / s, 4) if s else None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="只補最近 N 個日曆日(預設回到 START)")
    ap.add_argument("--universe", default="top100.json",
                    help="data/ 下的股票池檔名,如 top200.json")
    ap.add_argument("--stocks", type=int, default=999, help="取股票池前 N 檔")
    ap.add_argument("--min-shares", type=int, default=MIN_SHARES,
                    help="非重點分點的入庫門檻,0 表示全留")
    ap.add_argument("--workers", type=int, default=WORKERS, help="併發工作緒")
    ap.add_argument("--rate", type=int, default=RATE_PER_HOUR,
                    help="每小時請求上限(API 上限 6,000)")
    args = ap.parse_args()
    token = get_token()
    conn = sqlite3.connect(DB, timeout=30)
    # WAL:讓 web/server.py 能在回補進行中並行讀取(delete 模式下讀取會撞
    # 排他鎖,實測回補中開頁面就是 "database is locked")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)

    universe = json.loads((HERE / "data" / args.universe).read_text())
    stocks = [d["stock_id"] for d in universe[:args.stocks]]
    start = date.today() - timedelta(days=args.days) if args.days else START
    days = [start + timedelta(days=i)
            for i in range((date.today() - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]        # 跳過週末
    # 排除「抓過但 0 列」的近期日期:FinMind 週一至五 21:00 才更新,
    # 白天跑到當日會拿到空資料。若照常記為已完成,續傳會永久跳過那天
    # (2026-09-01 就這樣被跳掉)。7 天前以上的 0 列視為真的休市,保留。
    cutoff = int(f"{date.today() - timedelta(days=7):%Y%m%d}")
    done = {(s, dd) for s, dd, n in
            conn.execute("SELECT stock_id, data_date, rows FROM fetched")
            if n > 0 or dd < cutoff}
    # 日期外層、股票內層:中斷時已完成的日期是完整的(每天全部股票齊備),
    # 分析不會拿到「某天只有一半股票」的殘缺切片。
    todo = [(s, d) for d in days for s in stocks
            if (s, int(f"{d:%Y%m%d}")) not in done]
    interval = 3600.0 / args.rate                 # 每次請求的最小間隔
    print(f"股票池 {args.universe}:{len(stocks)} 檔")
    print(f"待抓 {len(todo):,} 個(股票×日) | {args.workers} 工作緒 "
          f"@ {args.rate:,}/小時 → 預估 {len(todo)*interval/3600:.1f} 小時"
          f" | 門檻 {args.min_shares} 股")

    lock = threading.Lock()
    next_slot = [time.time()]

    def throttled_fetch(job):
        """全域節流:所有工作緒共用一條時間軸,確保總速率不超過 rate。"""
        sid, d = job
        with lock:
            now = time.time()
            slot = max(now, next_slot[0])
            next_slot[0] = slot + interval
        time.sleep(max(0, slot - time.time()))
        return job, fetch_day(token, sid, d)

    n_ok = n_rows = 0
    t0 = time.time()
    stop = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        # 分批送出,避免一次排 3 萬個 future 佔記憶體
        for i in range(0, len(todo), 600):
            if stop:
                break
            batch = todo[i:i + 600]
            futures = {pool.submit(throttled_fetch, j): j for j in batch}
            for fut in as_completed(futures):
                sid, d = futures[fut]
                dd = int(f"{d:%Y%m%d}")
                try:
                    _, raw = fut.result()
                except Exception as e:
                    print(f"  {d} {sid}: 失敗 — {e}")
                    print("  中止(已完成部分可續跑)")
                    stop = True
                    break
                rows = aggregate(raw, args.min_shares)
                # 寫入集中在主執行緒,避免 SQLite 跨執行緒問題
                with conn:
                    conn.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?)",
                                 (sid, dd, len(rows)))
                    if rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO broker_daily (data_date,"
                            "stock_id,bno,buy_sh,sell_sh,buy_vwap,sell_vwap)"
                            " VALUES (?,?,?,?,?,?,?)",
                            [(dd, s2, b, bs, ss, bv, sv)
                             for s2, b, bs, ss, bv, sv in rows])
                n_ok += 1; n_rows += len(rows)
                if n_ok % 500 == 0:
                    el = time.time() - t0
                    eta = el / n_ok * (len(todo) - n_ok) / 3600
                    print(f"  {n_ok:,}/{len(todo):,}  最新 {d} {sid}  "
                          f"累計 {n_rows:,} 列  實測 {n_ok/el*3600:,.0f}/小時  "
                          f"剩約 {eta:.1f} 小時", flush=True)
    total = conn.execute("SELECT COUNT(*) FROM broker_daily").fetchone()[0]
    print(f"\n本輪 {n_ok:,} 組 / 新增 {n_rows:,} 列 | DB 共 {total:,} 列 "
          f"({DB.stat().st_size/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
