"""分點資金流向檢視器(本機 HTTP,唯讀查 data/broker.db)。

刻意獨立於 core/dashboard.py:那條管線把 PNG commit 進**公開** repo 供 LINE
抓圖,分點資料是 FinMind 授權內容,不能走那條路。本檔只綁 127.0.0.1,
資料不出本機;日後若要給人看,前面接 Cloudflare Tunnel + Access 做認證。

用法:
    python3 web/server.py            # http://127.0.0.1:8811
    python3 web/server.py --port 9000
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
DB = HERE.parent / "data" / "broker.db"
PORT = 8811                # 8765 本機已被其他服務占用,固定用 8811 避免衝突
UNIVERSE = HERE.parent / "data" / "top100.json"

# 重點分點:UI 預設排在前面。與 finmind_backfill.KEEP_BROKERS 同一份名單,
# 這裡重複一份以免 web 依賴回補腳本(兩者生命週期不同)。
FEATURED = {
    "9268": "凱基-台北", "9800": "元大", "5850": "統一", "9600": "富邦",
    "9875": "元大-土城永寧", "9216": "凱基-信義", "9217": "凱基-松山",
    "9661": "富邦-新店", "8440": "摩根大通", "1470": "摩根士丹利",
    "1650": "新加坡商瑞銀", "1480": "美商高盛", "1440": "美林",
}


_CACHE = {}          # {key: (到期時間, 值)}


def cached(key, ttl, fn):
    """meta/brokers 是全表掃描(560 萬列要 17 秒),但值只隨回補緩慢變化,
    故短 TTL 快取。第一次仍慢,之後命中即時回應。"""
    hit = _CACHE.get(key)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    val = fn()
    _CACHE[key] = (now + ttl, val)
    return val


def connect():
    """每個請求一條唯讀連線。

    原本是一條長期連線跨執行緒共用(check_same_thread=False)—— 那是錯的:
    sqlite3 連線不支援並發使用,加上寫入端 WAL 會 checkpoint,長期連線的
    快照失效時會回報 "database disk image is malformed"(實際資料完好)。
    連線開啟成本極低(次毫秒),改成每請求開一條,問題根除。
    """
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.execute("PRAGMA busy_timeout=15000")
    return c


@lru_cache(maxsize=1)
def broker_names():
    """官方券商名稱(tools_broker_names.py 產生);FEATURED 的手寫名優先。"""
    f = HERE.parent / "data" / "broker_names.json"
    out = json.loads(f.read_text()) if f.exists() else {}
    out.update(FEATURED)
    return out


@lru_cache(maxsize=1)
def stock_names():
    """合併所有股票池檔的代號→名稱。

    比對 stock_id 欄位而非檔名前綴 —— 原本只掃 top*.json,漏掉櫃買的
    otc100.json,上櫃個股在 UI 上只剩代號沒有名稱。
    """
    out = {}
    data = HERE.parent / "data"
    for f in sorted(data.glob("*.json")):
        try:
            rows = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for d in rows:
            if isinstance(d, dict) and "stock_id" in d and "name" in d:
                out.setdefault(d["stock_id"], d["name"])
    return out


# 淨買超金額(億元):買進金額 − 賣出金額。vwap 可能為 NULL(當日只買或只賣)
NET = ("(COALESCE(buy_sh*buy_vwap,0) - COALESCE(sell_sh*sell_vwap,0))/1e8")
BUY = "COALESCE(buy_sh*buy_vwap,0)/1e8"
SELL = "COALESCE(sell_sh*sell_vwap,0)/1e8"


def api_meta(conn, _q):
    def build():
        # fetched 只有 (股票×日) 幾萬列,拿日期/檔數比掃 broker_daily 快兩個數量級
        # 只算「真的有資料」的日期:fetched 也會記錄 rows=0 的日子
        # (盤中或休市時抓到空資料),否則日期範圍與天數會虛報一天。
        r = conn.execute("SELECT MIN(data_date), MAX(data_date),"
                         " COUNT(DISTINCT data_date), COUNT(DISTINCT stock_id)"
                         " FROM fetched WHERE rows>0").fetchone()
        rows = conn.execute("SELECT SUM(rows) FROM fetched").fetchone()[0] or 0
        # 全表 DISTINCT bno 要掃 1,850 萬列(實測 7.8 秒);改數最新一日的
        # 分點數,走 PK 前綴只掃當日(63 毫秒)。語意是「最近一日活躍分點」,
        # 與歷史曾出現過的總數(894)略有差異,標籤已註明。
        nb = conn.execute(
            "SELECT COUNT(DISTINCT bno) FROM broker_daily WHERE data_date="
            "(SELECT MAX(data_date) FROM broker_daily)").fetchone()[0]
        return {"date_min": r[0], "date_max": r[1], "days": r[2],
                "stocks": r[3], "rows": rows, "brokers": nb,
                "featured": [{"bno": k, "name": v} for k, v in FEATURED.items()]}
    return cached("meta", 60, build)


def api_brokers(conn, q):
    """分點排行:依總進出金額。limit 預設 60。"""
    limit = int(q.get("limit", ["60"])[0])
    return cached(f"brokers:{limit}", 300, lambda: _brokers(conn, limit))


def _brokers(conn, limit):
    # 優先讀離線彙總表(掃幾百列);沒有才退回全表 GROUP BY(1,850 萬列要
    # 40 秒以上)。彙總表由 tools_summary.py 重建。
    try:
        rows = conn.execute(
            "SELECT bno, buy, sell, days, stocks FROM broker_totals"
            " ORDER BY (buy+sell) DESC LIMIT ?", (limit,)).fetchall()
        if rows:
            nm = broker_names()
            return [{"bno": r[0], "name": nm.get(r[0], ""), "buy": r[1],
                     "sell": r[2], "net": r[1] - r[2], "days": r[3],
                     "stocks": r[4]} for r in rows]
    except sqlite3.OperationalError:
        pass
    rows = conn.execute(
        f"SELECT bno, SUM({BUY}) b, SUM({SELL}) s, COUNT(DISTINCT data_date) d,"
        f" COUNT(DISTINCT stock_id) k FROM broker_daily"
        f" GROUP BY bno ORDER BY (b+s) DESC LIMIT ?", (limit,)).fetchall()
    nm = broker_names()
    return [{"bno": r[0], "name": nm.get(r[0], ""), "buy": r[1],
             "sell": r[2], "net": r[1] - r[2], "days": r[3], "stocks": r[4]}
            for r in rows]


def api_broker(conn, q):
    """單一分點:每日淨流向 + 個股排行。"""
    bno = q.get("bno", ["9268"])[0]
    # 個股排行必須跟著使用者選的日期區間(不給則全期間)
    f = int(q.get("from", ["0"])[0] or 0)
    t2 = int(q.get("to", ["99999999"])[0] or 99999999)
    daily = conn.execute(
        f"SELECT data_date, SUM({NET}), SUM({BUY}), SUM({SELL})"
        f" FROM broker_daily WHERE bno=? GROUP BY data_date ORDER BY data_date",
        (bno,)).fetchall()
    topn = max(1, min(500, int(q.get("topn", ["20"])[0] or 20)))
    # 買超與賣超分開呈現:混在一起用絕對值排,兩邊會互相遮蔽,看不出
    # 「在累積什麼」與「在出貨什麼」。股票池最多 200 檔,一次全取再切。
    allrows = conn.execute(
        f"SELECT stock_id, SUM({NET}) n, SUM({BUY}+{SELL}) t,"
        f" SUM(COALESCE(buy_sh,0)), SUM(COALESCE(sell_sh,0)) FROM broker_daily"
        f" WHERE bno=? AND data_date BETWEEN ? AND ?"
        f" GROUP BY stock_id ORDER BY n DESC", (bno, f, t2)).fetchall()
    names = stock_names()

    def pack(rows):
        # 股數一併回傳,前端換算成張(1 張 = 1,000 股)。金額看規模,
        # 張數看周轉 —— 同樣淨買 10 億,可能是買 100 張沒賣,也可能是
        # 買 5,000 張賣 4,900 張,行為完全不同。
        return [{"stock_id": r[0], "name": names.get(r[0], ""),
                 "net": r[1], "turnover": r[2],
                 "buy_sh": r[3], "sell_sh": r[4]} for r in rows]

    buys = [r for r in allrows if r[1] > 0][:topn]
    sells = [r for r in allrows if r[1] < 0][-topn:][::-1]   # 由最負開始
    return {"bno": bno, "name": broker_names().get(bno, ""),
            "range": [f, t2],
            "daily": [{"d": r[0], "net": r[1], "buy": r[2], "sell": r[3]}
                      for r in daily],
            "top_buy": pack(buys), "top_sell": pack(sells),
            "n_buy": sum(1 for r in allrows if r[1] > 0),
            "n_sell": sum(1 for r in allrows if r[1] < 0)}


def api_stock(conn, q):
    """單一個股:區間內各分點買賣超排行 + 重點分點每日淨額。

    支援日期區間(from/to);只給 date 時視為單日,不給則取最新一日。
    """
    sid = q.get("stock_id", ["2330"])[0]
    one = q.get("date", [None])[0]
    f = q.get("from", [None])[0]
    t2 = q.get("to", [None])[0]
    if f or t2:
        f = int(f) if f else 0
        t2 = int(t2) if t2 else 99999999
    elif one:
        f = t2 = int(one)
    else:
        last = conn.execute("SELECT MAX(data_date) FROM broker_daily"
                            " WHERE stock_id=?", (sid,)).fetchone()[0] or 0
        f = t2 = last
    # 區間內同一分點可能有多天,須先 GROUP BY 分點再排序
    rows = conn.execute(
        f"SELECT bno, SUM({NET}) n, SUM({BUY}+{SELL}) t,"
        f" SUM(COALESCE(buy_sh,0)), SUM(COALESCE(sell_sh,0)),"
        f" COUNT(DISTINCT data_date) FROM broker_daily"
        f" WHERE stock_id=? AND data_date BETWEEN ? AND ?"
        f" GROUP BY bno ORDER BY n DESC", (sid, f, t2)).fetchall()
    topn = max(1, min(200, int(q.get("topn", ["20"])[0] or 20)))
    _bn = broker_names()

    def pack(rs):
        return [{"bno": r[0], "name": _bn.get(r[0], ""), "net": r[1],
                 "turnover": r[2], "buy_sh": r[3], "sell_sh": r[4],
                 "days": r[5]} for r in rs]

    feat = conn.execute(
        f"SELECT data_date, bno, {NET} FROM broker_daily WHERE stock_id=?"
        f" AND data_date BETWEEN ? AND ?"
        f" AND bno IN ({','.join('?'*len(FEATURED))}) ORDER BY data_date",
        (sid, f, t2, *FEATURED)).fetchall()
    dates = conn.execute(
        "SELECT DISTINCT data_date FROM broker_daily WHERE stock_id=?"
        " ORDER BY data_date", (sid,)).fetchall()
    # 籌碼集中度:(前 N 大買超張 − 前 N 大賣超張) ÷ 期間成交張數。
    # 公式以看盤軟體的數字反推驗證:-1639 張 / 8,710 張 = -18.82%,吻合。
    # N 是各家自訂的口徑(常見 15),故做成參數並在 UI 標明。
    topc = max(1, min(50, int(q.get("conc_n", ["15"])[0] or 15)))
    # 分母用日線成交量(= 證交所官方數字,已交叉驗證吻合),不用分點買進合計:
    # 分點資料的成交量系統性少於官方(200 檔中 12 檔差 >1%,台積電某日少 41%),
    # 推測是盤後定價/鉅額等交易未歸屬到分點。用官方量分母才與看盤軟體可比。
    vol_sh = conn.execute(
        "SELECT SUM(COALESCE(volume,0)) FROM price"
        " WHERE stock_id=? AND data_date BETWEEN ? AND ?",
        (sid, f, t2)).fetchone()[0] or 0
    vol_src = "日線"
    if not vol_sh:                                # 沒有日線資料才退回分點合計
        vol_sh = conn.execute(
            "SELECT SUM(COALESCE(buy_sh,0)) FROM broker_daily"
            " WHERE stock_id=? AND data_date BETWEEN ? AND ?",
            (sid, f, t2)).fetchone()[0] or 0
        vol_src = "分點合計"
    nets = [r[3] - r[4] for r in rows]            # 各分點淨股數
    pos = sorted([n for n in nets if n > 0], reverse=True)[:topc]
    neg = sorted([n for n in nets if n < 0])[:topc]
    conc_sh = sum(pos) + sum(neg)                 # neg 本身為負,相加即為差
    px = conn.execute(
        "SELECT data_date, close FROM price WHERE stock_id=?"
        " AND data_date BETWEEN ? AND ? ORDER BY data_date", (sid, f, t2)).fetchall()
    return {"stock_id": sid, "name": stock_names().get(sid, ""),
            "range": [f, t2],
            "price": [{"d": r[0], "close": r[1]} for r in px],
            "conc_n": topc,
            # rows 為空表示該區間沒有分點資料(例如回補還沒推進到那些日期),
            # 此時回 None 而非 0,避免畫面顯示「集中度 0%」誤導
            "conc_lots": (conc_sh / 1000) if rows else None,
            "conc_pct": (conc_sh / vol_sh * 100) if (rows and vol_sh) else None,
            "vol_lots": (vol_sh / 1000) if vol_sh else None,
            "vol_src": vol_src,
            "dates": [d[0] for d in dates],
            "buyers": pack([r for r in rows if r[1] > 0][:topn]),
            "sellers": pack([r for r in rows if r[1] < 0][-topn:][::-1]),
            "n_buy": sum(1 for r in rows if r[1] > 0),
            "n_sell": sum(1 for r in rows if r[1] < 0),
            "featured_daily": [{"d": r[0], "bno": r[1], "net": r[2]}
                               for r in feat]}


def api_stocks(conn, _q):
    def build():
        # 走 fetched(幾萬列)而非 broker_daily 的 DISTINCT(1,850 萬列要 13 秒)
        names = stock_names()
        rows = conn.execute("SELECT DISTINCT stock_id FROM fetched"
                            " WHERE rows>0 ORDER BY stock_id").fetchall()
        return [{"stock_id": r[0], "name": names.get(r[0], "")} for r in rows]
    return cached("stocks", 300, build)


def api_pair(conn, q):
    """單一 (分點 × 個股) 的每日進出 —— 個股頁點某分點後的下鑽明細。"""
    bno = q.get("bno", [""])[0]
    sid = q.get("stock_id", [""])[0]
    rows = conn.execute(
        f"SELECT data_date, COALESCE(buy_sh,0), COALESCE(sell_sh,0),"
        f" buy_vwap, sell_vwap FROM broker_daily"
        f" WHERE bno=? AND stock_id=? ORDER BY data_date", (bno, sid)).fetchall()
    daily = [{"d": r[0], "buy_sh": r[1], "sell_sh": r[2],
              "net": (r[1] * (r[3] or 0) - r[2] * (r[4] or 0)) / 1e8,
              "buy_vwap": r[3], "sell_vwap": r[4]} for r in rows]
    tb = sum(r[1] for r in rows)
    ts = sum(r[2] for r in rows)
    ba = sum(r[1] * (r[3] or 0) for r in rows)
    sa = sum(r[2] * (r[4] or 0) for r in rows)
    # 帶開高低收與成交量,前端才畫得出 K 線(不只折線)
    px = {r[0]: r[1:] for r in conn.execute(
        "SELECT data_date, open, high, low, close, volume FROM price"
        " WHERE stock_id=?", (sid,)).fetchall()}
    for x in daily:
        o = px.get(x["d"])
        if o:
            x["open"], x["high"], x["low"], x["close"], x["vol"] = o
        else:
            x["open"] = x["high"] = x["low"] = x["close"] = x["vol"] = None
    return {"bno": bno, "name": broker_names().get(bno, ""),
            "stock_id": sid, "stock_name": stock_names().get(sid, ""),
            "daily": daily,
            "buy_lots": tb / 1000, "sell_lots": ts / 1000,
            "buy_vwap": (ba / tb) if tb else None,
            "sell_vwap": (sa / ts) if ts else None,
            "net_amt": (ba - sa) / 1e8}


ROUTES = {"/api/meta": api_meta, "/api/brokers": api_brokers,
          "/api/pair": api_pair,
          "/api/broker": api_broker, "/api/stock": api_stock,
          "/api/stocks": api_stocks}


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *a):
        print(f"  {datetime.now():%H:%M:%S} {fmt % a}")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            f = HERE / "index.html"
            if not f.exists():
                return self._send(404, b"index.html missing", "text/plain")
            # 不快取:改版後使用者若拿到舊 HTML,會以為新功能沒生效
            self.send_response(200)
            body = f.read_bytes()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        fn = ROUTES.get(u.path)
        if not fn:
            return self._send(404, b'{"error":"not found"}', "application/json")
        conn = None
        try:
            conn = connect()
            data = fn(conn, parse_qs(u.query))
            body = json.dumps(data, ensure_ascii=False).encode()
            return self._send(200, body, "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode()
            return self._send(500, body, "application/json; charset=utf-8")
        finally:
            if conn is not None:
                conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    if not DB.exists():
        raise SystemExit(f"找不到 {DB},請先跑 finmind_backfill.py")
    connect().close()          # 啟動即驗證 DB 可讀,不要等第一個請求才發現
    print(f"分點檢視器 → http://127.0.0.1:{args.port}  (Ctrl-C 結束)")
    # 只綁 loopback:不對外,也不受本機防火牆設定影響
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
