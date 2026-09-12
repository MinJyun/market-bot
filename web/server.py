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
import re
import sqlite3
import time
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
DB = HERE.parent / "data" / "broker.db"
# 主動式 ETF 的基金層級資料(淨值、流通單位數)只有 sources/active_etf.py
# 抓的投信公開揭露資料有,FinMind 沒有這個資料集,所以要跨兩個 DB。
# 持股明細則反過來只讀 broker.db(FinMind,40 檔全歷史)。
MARKET_DB = HERE.parent / "data" / "market.db"
PORT = 8811                # 8765 本機已被其他服務占用,固定用 8811 避免衝突
UNIVERSE = HERE.parent / "data" / "top100.json"

# 可對外提供的靜態檔白名單(檔名 → Content-Type)。用白名單而非「HERE 底下
# 任意檔案」:server.py 與 *.db 就在同一層,路徑穿越或手誤會直接外洩。
STATIC = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "lib.js": "text/javascript; charset=utf-8",
    "charts.js": "text/javascript; charset=utf-8",
    "tables.js": "text/javascript; charset=utf-8",
    "demo.js": "text/javascript; charset=utf-8",
}

# demo 分頁的示範資料(demo_build.py 產生,在 web/demo/)。落成靜態檔而非端點:
# 逐價位那層現在的 DB 沒有(finmind_backfill.aggregate 時壓掉了),每次開頁面
# 都去打 FinMind 既慢又浪費額度。
#
# 檔名逐字元驗證而非「demo/ 底下任意檔案」:白名單的用意就是不讓路徑由請求
# 決定,開一個目錄再放行等於把那個保護拆掉一半。只收 index 或 4~6 碼股票代號。
DEMO_DIR = HERE / "demo"
DEMO_RE = re.compile(r"^(index|[0-9]{4,6}[A-Z]?)\.json$")

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


def connect_market():
    """market.db 唯讀連線;不存在時回 None(ETF 面板的申贖欄位就留空)。"""
    if not MARKET_DB.exists():
        return None
    c = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
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


def trade_pnl(buy_sh, sell_sh, buy_amt, sell_amt, close):
    """區間內進出的損益拆解,回 (已實現, 未實現, 合計),單位皆為億元。

        已實現 = 買賣相抵股數 ×(賣均價 − 買均價)
        未實現 = **淨買超時**才算,以 close 評價剩餘部位

    **刻意不用移動平均成本逐日推部位**:那要假設區間起點的部位為 0,而抽樣
    400 組高量(分點×個股)發現 96% 的累積淨部位曾為負、78% 負到超過峰值
    10%(最極端:永豐金×0050 累積 −258 萬張)—— 多數分點在資料起點
    2025-07-28 前就有存貨,推出來的會是不存在的空頭部位。

    **淨賣超時不估未實現**(回 None):賣掉的那些股票是既有庫存,成本不可知。
    原本用 (賣均價 − 收盤) 當成「放空的浮動損益」,那等於假設它在區間起點
    部位為零、淨賣的部分全是新開空單 —— 對大券商完全不成立。實例:
    摩根士丹利×台積電 60 日淨賣 71,148 張,舊算法報未實現 −27.71 億,
    但它庫存龐大,那些賣出多半是實現獲利,不是虧損的空單。
    這種情況只有「已實現(相抵部分)」是可信的。

    恆等式(淨買超時成立,可用來驗算):
        已實現 + 未實現 == (賣出金額 − 買進金額) + 淨股數 × close
    """
    if close is None:
        return None, None, None
    bv = buy_amt * 1e8 / buy_sh if buy_sh else None
    sv = sell_amt * 1e8 / sell_sh if sell_sh else None
    matched = min(buy_sh, sell_sh)
    real = matched * (sv - bv) / 1e8 if (matched and bv and sv) else 0.0
    net = buy_sh - sell_sh
    if net > 0:
        unreal = net * (close - bv) / 1e8 if bv else 0.0
        return real, unreal, real + unreal
    if net == 0:
        return real, 0.0, real
    return real, None, real          # 淨賣超:未實現不可知,合計只認已實現


def last_closes(conn, upto):
    """回 (收盤價 dict, 實際評價日)。取區間結束日或之前最近一個交易日。

    未實現一律用「區間結束日」評價,不用「該分點最後一次進出那天」:同一張
    排行表裡的股票要在同一個時點評價才可比。
    """
    d = conn.execute("SELECT MAX(data_date) FROM price WHERE data_date<=?",
                     (upto,)).fetchone()[0]
    if d is None:
        return {}, None
    # 收盤價也要除以當日係數換算到今日基礎:區間結束日若早於某次除權,
    # 它的原始收盤與(已換算成今日基礎的)成本就不在同一個尺度上。
    return dict(conn.execute(
        "SELECT p.stock_id, p.close / COALESCE(a.q, 1) FROM price p"
        " LEFT JOIN adj_daily a ON a.stock_id=p.stock_id AND a.data_date=p.data_date"
        " WHERE p.data_date=?", (d,))), d


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


def _rank_pnl(rows, topn):
    """(已實現+未實現) 排行。只看已實現會漏掉抱著獲利的部位,只看未實現會
    漏掉來回賺走的價差,所以用合計排。"""
    ok = [x for x in rows if x["pnl"] is not None]
    gain = sorted([x for x in ok if x["pnl"] > 0], key=lambda x: -x["pnl"])
    loss = sorted([x for x in ok if x["pnl"] < 0], key=lambda x: x["pnl"])
    return {"top_gain": gain[:topn], "top_loss": loss[:topn],
            "n_gain": len(gain), "n_loss": len(loss),
            "pnl_total": sum(x["pnl"] for x in ok) if ok else None}


def api_broker_pnl(conn, q):
    """單一分點的個股損益排行。

    **獨立於主頁的日期區間**:主圖是在挑「看哪一段行情」,損益排行是在問
    「這段期間做得好不好」,兩者常要不同長度(而且損益排行只看一天沒意義)。
    """
    bno = q.get("bno", ["9268"])[0]
    f = int(q.get("from", ["0"])[0] or 0)
    t2 = int(q.get("to", ["99999999"])[0] or 99999999)
    topn = max(1, min(200, int(q.get("topn", ["20"])[0] or 20)))
    # 股數乘上公司行動係數換算成今日基礎;金額(股數×成交價)本身不受影響
    rows = conn.execute(
        f"SELECT b.stock_id,"
        f" SUM(COALESCE(b.buy_sh,0) * COALESCE(a.q,1)),"
        f" SUM(COALESCE(b.sell_sh,0) * COALESCE(a.q,1)),"
        f" SUM({BUY}), SUM({SELL}) FROM broker_daily b"
        f" LEFT JOIN adj_daily a ON a.stock_id=b.stock_id"
        f"  AND a.data_date=b.data_date"
        f" WHERE b.bno=? AND b.data_date BETWEEN ? AND ?"
        f" GROUP BY b.stock_id", (bno, f, t2)).fetchall()
    px, px_date = last_closes(conn, t2)
    names = stock_names()
    packed = []
    for sid, bs, ss, ba, sa in rows:
        real, unreal, tot = trade_pnl(bs, ss, ba, sa, px.get(sid))
        packed.append({"stock_id": sid, "name": names.get(sid, ""),
                       "buy_sh": bs, "sell_sh": ss,
                       "matched_sh": min(bs, ss),
                       "real": real, "unreal": unreal, "pnl": tot})
    out = _rank_pnl(packed, topn)
    out.update({"bno": bno, "name": broker_names().get(bno, ""),
                "range": [f, t2], "px_date": px_date})
    return out


def api_stock_pnl(conn, q):
    """單一個股的各分點損益排行(獨立區間,理由同 api_broker_pnl)。"""
    sid = q.get("stock_id", ["2330"])[0]
    f = int(q.get("from", ["0"])[0] or 0)
    t2 = int(q.get("to", ["99999999"])[0] or 99999999)
    topn = max(1, min(200, int(q.get("topn", ["20"])[0] or 20)))
    rows = conn.execute(
        f"SELECT b.bno,"
        f" SUM(COALESCE(b.buy_sh,0) * COALESCE(a.q,1)),"
        f" SUM(COALESCE(b.sell_sh,0) * COALESCE(a.q,1)),"
        f" SUM({BUY}), SUM({SELL}), COUNT(DISTINCT b.data_date)"
        f" FROM broker_daily b LEFT JOIN adj_daily a"
        f"  ON a.stock_id=b.stock_id AND a.data_date=b.data_date"
        f" WHERE b.stock_id=? AND b.data_date BETWEEN ? AND ?"
        f" GROUP BY b.bno", (sid, f, t2)).fetchall()
    px_row = conn.execute(
        "SELECT p.data_date, p.close / COALESCE(a.q,1) FROM price p"
        " LEFT JOIN adj_daily a ON a.stock_id=p.stock_id"
        "  AND a.data_date=p.data_date"
        " WHERE p.stock_id=? AND p.data_date<=?"
        " ORDER BY p.data_date DESC LIMIT 1", (sid, t2)).fetchone()
    px_date, px_close = px_row if px_row else (None, None)
    nm = broker_names()
    packed = []
    for bno, bs, ss, ba, sa, days in rows:
        real, unreal, tot = trade_pnl(bs, ss, ba, sa, px_close)
        packed.append({"bno": bno, "name": nm.get(bno, ""),
                       "buy_sh": bs, "sell_sh": ss, "days": days,
                       "matched_sh": min(bs, ss),
                       "real": real, "unreal": unreal, "pnl": tot})
    out = _rank_pnl(packed, topn)
    out.update({"stock_id": sid, "name": stock_names().get(sid, ""),
                "range": [f, t2], "px_date": px_date, "px_close": px_close})
    return out


def api_stock_featured(conn, q):
    """重點分點在該股的每日淨額(獨立區間),供堆疊柱狀圖。"""
    sid = q.get("stock_id", ["2330"])[0]
    f = int(q.get("from", ["0"])[0] or 0)
    t2 = int(q.get("to", ["99999999"])[0] or 99999999)
    rows = conn.execute(
        f"SELECT data_date, bno, {NET} FROM broker_daily WHERE stock_id=?"
        f" AND data_date BETWEEN ? AND ?"
        f" AND bno IN ({','.join('?'*len(FEATURED))}) ORDER BY data_date",
        (sid, f, t2, *FEATURED)).fetchall()
    return {"stock_id": sid, "range": [f, t2],
            "daily": [{"d": r[0], "bno": r[1], "net": r[2]} for r in rows]}


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
            "n_sell": sum(1 for r in rows if r[1] < 0)}


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
    # 一律換算到今日基礎:股數 ×q、價格 ÷q(金額不變)。不換算的話,跨過
    # 除權的買均價會拿去和除權後的收盤價相減 —— 緯穎(6669)2026-09-02 配
    # 股票股利 1 股變 2.98 股,面板一度顯示未實現 +51.57 億全是假的。
    # 係數逐日與 FinMind 還原股價比對過,差異 0.00%。
    rows = conn.execute(
        f"SELECT b.data_date, COALESCE(b.buy_sh,0) * COALESCE(a.q,1),"
        f" COALESCE(b.sell_sh,0) * COALESCE(a.q,1),"
        f" b.buy_vwap / COALESCE(a.q,1), b.sell_vwap / COALESCE(a.q,1),"
        f" COALESCE(a.q,1) FROM broker_daily b"
        f" LEFT JOIN adj_daily a ON a.stock_id=b.stock_id"
        f"  AND a.data_date=b.data_date"
        f" WHERE b.bno=? AND b.stock_id=? ORDER BY b.data_date",
        (bno, sid)).fetchall()
    daily = [{"d": r[0], "buy_sh": r[1], "sell_sh": r[2],
              "net": (r[1] * (r[3] or 0) - r[2] * (r[4] or 0)) / 1e8,
              "buy_vwap": r[3], "sell_vwap": r[4], "q": r[5]} for r in rows]
    tb = sum(r[1] for r in rows)
    ts = sum(r[2] for r in rows)
    ba = sum(r[1] * (r[3] or 0) for r in rows)
    sa = sum(r[2] * (r[4] or 0) for r in rows)
    # 帶開高低收與成交量,前端才畫得出 K 線(不只折線)
    # K 線同樣換算,否則除權前後的價格會出現斷崖,買賣均價虛線也對不上
    px = {r[0]: r[1:] for r in conn.execute(
        "SELECT p.data_date, p.open/COALESCE(a.q,1), p.high/COALESCE(a.q,1),"
        " p.low/COALESCE(a.q,1), p.close/COALESCE(a.q,1),"
        " p.volume*COALESCE(a.q,1) FROM price p"
        " LEFT JOIN adj_daily a ON a.stock_id=p.stock_id"
        "  AND a.data_date=p.data_date"
        " WHERE p.stock_id=?", (sid,)).fetchall()}
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


# ==================================================== 主動式 ETF
def _tw_code(col="code"):
    """只認純數字代號:海外型主動 ETF 的成分是美股(如 "BE US"),債券型是
    中文名稱(如 "債券附買回-P11台電6B"),都不該混進台股資金流向的統計。"""
    return f"{col} GLOB '[0-9][0-9][0-9][0-9]*'"


def api_etf_meta(conn, _q):
    def build():
        r = conn.execute("SELECT MIN(data_date), MAX(data_date), COUNT(*)"
                         " FROM etf_fetched WHERE holding_rows>0").fetchone()
        rows = conn.execute(
            "SELECT i.etf, i.name, i.category, i.market,"
            " (SELECT COUNT(DISTINCT data_date) FROM etf_holding h"
            "  WHERE h.etf=i.etf) FROM etf_info i ORDER BY i.etf").fetchall()
        mk = connect_market()
        own = set()
        if mk is not None:
            try:
                own = {x[0] for x in mk.execute("SELECT DISTINCT etf FROM fund_day")}
            except sqlite3.OperationalError:
                pass
            finally:
                mk.close()
        return {
            "date_min": r[0], "date_max": r[1], "days": r[2],
            # own=True 表示該檔另有自家投信資料,申贖/淨值曲線畫得出來
            "etfs": [{"etf": x[0], "name": x[1], "category": x[2],
                      "market": x[3], "days": x[4], "own": x[0] in own}
                     for x in rows],
        }
    return cached("etf_meta", 120, build)


def corp_factor(ratios):
    """從同一天、同一檔股票的各 ETF 持股比值推出公司行動係數,沒有則回 None。

    FinMind 的 HoldingChange **不還原公司行動**:2026-09-02 緯穎 1:3 分割,
    15 檔持有它的 ETF 全部出現巨額「買進」,合計 883 萬股 = 當日成交量的
    81%,但沒有人買過一股。

    判準是「所有持有者的持股同比例變動」—— 市場買賣絕不會讓每一檔 ETF
    的持股比值分毫不差,公司行動才會。實測緯穎那天 15 檔有 14 檔比值都是
    2.9828,只有 00982A 是 2.7279(因為它同時真的賣了 13 萬股)。
    """
    if len(ratios) < 3:
        return None
    med = sorted(ratios)[len(ratios) // 2]
    if abs(med - 1) <= 0.02:                       # 沒有明顯的整體變動
        return None
    agree = sum(1 for r in ratios if abs(r - med) <= 0.005 * med)
    return med if agree * 2 >= len(ratios) else None


def api_etf_consensus(conn, q):
    """跨 ETF 共識流向:區間內每檔個股被幾檔主動式 ETF 加碼/減碼。

    7 檔做不出「共識」這個概念,40 檔才有意義。分母一律用 price 表的日線
    成交量 —— ETF 買賣股數不是成交量,拿它當分母會算出無意義的比率。
    """
    f = int(q.get("from", ["0"])[0] or 0)
    t2 = int(q.get("to", ["99999999"])[0] or 99999999)
    topn = max(1, min(200, int(q.get("topn", ["25"])[0] or 25)))
    # 逐 (日, ETF, 股票) 取出:公司行動的還原要在日內、跨 ETF 比對才做得到,
    # 先在 SQL 加總就無從還原了。holding 的 shares 是當日持股,前日持股用
    # shares − net 回推,省掉找「前一個有資料的交易日」。
    rows = conn.execute(
        f"""SELECT c.data_date, c.etf, c.code, c.name, c.buy, c.sell,
                   COALESCE(h.shares, 0)
            FROM etf_change c LEFT JOIN etf_holding h
              ON h.data_date=c.data_date AND h.etf=c.etf AND h.code=c.code
            WHERE c.data_date BETWEEN ? AND ? AND {_tw_code('c.code')}""",
        (f, t2)).fetchall()
    by_day = {}
    for dd, etf, code, nm, buy, sell, shares in rows:
        by_day.setdefault((dd, code), []).append(
            (etf, nm, (buy or 0) - (sell or 0), shares))

    agg, corp = {}, {}
    for (dd, code), items in by_day.items():
        ratios = [sh / (sh - net) for _, _, net, sh in items
                  if sh > 0 and sh - net > 0]
        factor = corp_factor(ratios)
        if factor:
            corp.setdefault(code, []).append(dd)
        a = agg.setdefault(code, {"name": "", "net_sh": 0.0, "turn_sh": 0.0,
                                  "buy": set(), "sell": set()})
        for etf, nm, net, sh in items:
            a["name"] = a["name"] or nm
            # 還原:真實淨額 = 今日持股 − 係數 × 前日持股。純受公司行動影響
            # 的 ETF 還原後理論上是 0,但係數取的是中位數,會留下不到一股的
            # 浮點殘差 —— 不歸零的話那些 ETF 會被算成「有買賣」,共識檔數
            # 就虛報(緯穎那天本來只有 1 檔真的動,卻報成 7 買 7 賣)。
            adj = net
            if factor:
                # 係數只套在「公司行動解釋得掉大部分變動」的 ETF 上。各投信
                # 反映分割的時點不一致:緯穎 09-03 那天 00400A/00407A 才補上
                # 分割(比值同為 2.98279),但同一天 00993A 是真的賣了 6,000 股
                # ——無條件套係數會把它算成賣 283,929 股。
                cand = sh - factor * (sh - net)
                if abs(cand) < abs(net):
                    adj = cand
            # 門檻取相對值:殘差 ≈ 係數誤差 × 前日持股,會隨部位規模放大,
            # 固定 1 股的門檻擋不住大部位(緯穎那天仍虛報 3 檔買)。
            # 反過來說,佔部位不到 0.1% 的變動對共識判讀也沒有意義。
            if abs(adj) < max(1.0, 0.001 * sh):
                continue
            a["net_sh"] += adj
            a["turn_sh"] += abs(adj)
            if adj > 0:
                a["buy"].add(etf)
            else:
                a["sell"].add(etf)

    vol = {s: (v, d) for s, v, d in conn.execute(
        "SELECT stock_id, SUM(volume), MAX(data_date) FROM price"
        " WHERE data_date BETWEEN ? AND ? GROUP BY stock_id", (f, t2))}
    close = dict(conn.execute(
        "SELECT stock_id, close FROM price WHERE data_date="
        "(SELECT MAX(data_date) FROM price WHERE data_date<=?)", (t2,)))
    names = stock_names()
    out = []
    for code, a in agg.items():
        if round(a["net_sh"]) == 0:
            continue
        v = vol.get(code, (None, None))[0]
        px = close.get(code)
        out.append({
            "stock_id": code, "name": names.get(code) or a["name"] or "",
            "net_sh": a["net_sh"], "turn_sh": a["turn_sh"],
            "n_buy": len(a["buy"]), "n_sell": len(a["sell"]),
            "net": (a["net_sh"] * px / 1e8) if px else None,
            # 佔期間成交量%:ETF 的買賣相對整體量能有多重
            "pct_vol": (abs(a["net_sh"]) / v * 100) if v else None,
            "corp": sorted(corp.get(code, [])) or None,
        })
    # 買超與賣超分開排:混在一起用絕對值排會互相遮蔽(與分點頁同一原則)
    buys = sorted([x for x in out if x["net_sh"] > 0],
                  key=lambda x: (-x["n_buy"], -abs(x["net"] or 0)))[:topn]
    sells = sorted([x for x in out if x["net_sh"] < 0],
                   key=lambda x: (-x["n_sell"], -abs(x["net"] or 0)))[:topn]
    return {"range": [f, t2], "top_buy": buys, "top_sell": sells,
            "n_buy": sum(1 for x in out if x["net_sh"] > 0),
            "n_sell": sum(1 for x in out if x["net_sh"] < 0),
            "n_corp": len(corp)}


def api_etf_fund(conn, q):
    """單一 ETF:每日規模與持股結構 + 申贖曲線 + 最新持股與當日異動。

    申贖(流通單位數)是判讀關鍵:規模擴張時經理人本來就要加碼,看到「加碼」
    不等於看好。這欄只有自家投信資料有,FinMind 沒有。
    """
    etf = q.get("etf", ["00981A"])[0]
    daily = conn.execute(
        "SELECT data_date, COUNT(*), SUM(CASE WHEN asset_type='stock'"
        " THEN weight ELSE 0 END), SUM(market_value)"
        " FROM etf_holding WHERE etf=? GROUP BY data_date ORDER BY data_date",
        (etf,)).fetchall()
    chg = dict(conn.execute(
        "SELECT data_date, SUM(buy)+SUM(sell) FROM etf_change WHERE etf=?"
        f" AND {_tw_code()} GROUP BY data_date", (etf,)).fetchall())
    # 自家投信資料的淨值與流通單位數(只有 7 檔有)
    units = {}
    mk = connect_market()
    if mk is not None:
        try:
            units = {int(d.replace("-", "")): (n, u) for d, n, u in mk.execute(
                "SELECT data_date, nav, units FROM fund_day WHERE etf=?", (etf,))}
        except sqlite3.OperationalError:
            pass
        finally:
            mk.close()
    series = []
    for dd, n, w, mv in daily:
        nav, u = units.get(dd, (None, None))
        series.append({"d": dd, "holdings": n, "stock_weight": w,
                       "market_value": mv, "turn_sh": chg.get(dd, 0),
                       "nav": nav, "units": u})

    # 可指定資料日;不給或該日無資料則取最新一日(不要靜默回空表)
    want = q.get("date", [None])[0]
    dates = [r[0] for r in daily]
    last = None
    if dates:
        if want and int(want) in dates:
            last = int(want)
        elif want:
            earlier = [d for d in dates if d <= int(want)]
            last = earlier[-1] if earlier else dates[0]
        else:
            last = dates[-1]
    hold = conn.execute(
        "SELECT code, name, asset_type, shares, weight, market_value"
        " FROM etf_holding WHERE etf=? AND data_date=?"
        " ORDER BY weight DESC", (etf, last)).fetchall() if last else []
    moves = conn.execute(
        "SELECT code, name, buy, sell FROM etf_change WHERE etf=? AND"
        " data_date=? ORDER BY ABS(buy-sell) DESC", (etf, last)).fetchall() \
        if last else []
    info = conn.execute("SELECT name, category, market FROM etf_info"
                        " WHERE etf=?", (etf,)).fetchone()
    return {
        "etf": etf, "name": info[0] if info else "",
        "category": info[1] if info else "", "market": info[2] if info else "",
        "has_units": any(x["units"] for x in series),
        "last": last, "asked": int(want) if want else None,
        "dates": dates, "daily": series,
        "holdings": [{"stock_id": r[0], "name": r[1], "asset_type": r[2],
                      "shares": r[3], "weight": r[4], "market_value": r[5]}
                     for r in hold],
        "moves": [{"stock_id": r[0], "name": r[1], "buy": r[2], "sell": r[3],
                   "net_sh": (r[2] or 0) - (r[3] or 0)} for r in moves],
    }


# ==================================================== 大盤(期權籌碼)
# 全部讀 market.db(TWSE/期交所公開資料),與 broker.db 的 FinMind 授權資料無關。
def _mk_rows(mk, sql, args=()):
    try:
        return mk.execute(sql, args).fetchall()
    except sqlite3.OperationalError:      # 該表還沒建(來源尚未跑過)
        return []


def api_index(conn, q):
    """大盤:K 線 + 期貨/選擇權三大法人未平倉 + 十大特法 + P/C ratio。

    未平倉是**部位存量**(今天站多少多空),不是當日買賣;`*_trade` 才是當日
    交易淨額。兩者判讀完全不同,所以一起回傳、UI 上分開標示。
    """
    days = max(5, min(500, int(q.get("days", ["60"])[0] or 60)))
    mk = connect_market()
    if mk is None:
        return {"error": "market.db 不存在"}
    try:
        px = _mk_rows(mk,
            "SELECT data_date, open, high, low, close, change_pts, change_pct,"
            " amount FROM market_index WHERE open IS NOT NULL"
            " ORDER BY data_date DESC LIMIT ?", (days,))[::-1]
        if not px:
            return {"error": "market_index 無開高低資料"}
        d0 = px[0][0]
        fut = _mk_rows(mk,
            "SELECT data_date, contract, foreign_net, trust_net, dealer_net,"
            " foreign_trade, trust_trade, dealer_trade FROM futures_inst"
            " WHERE data_date>=? ORDER BY data_date", (d0,))
        opt = _mk_rows(mk,
            "SELECT data_date, cp, foreign_net, trust_net, dealer_net,"
            " foreign_trade, trust_trade, dealer_trade FROM options_inst_cp"
            " WHERE data_date>=? ORDER BY data_date", (d0,))
        # 十大特法:buy5/buy10 是「前 5/10 大特定法人」的多方部位,
        # sell 同理;淨 = buy − sell。*_all 是全部交易人(含自然人)。
        flt = _mk_rows(mk,
            "SELECT data_date, contract, buy5, sell5, buy10, sell10,"
            " buy5_all, sell5_all, buy10_all, sell10_all, oi FROM futures_lt"
            " WHERE data_date>=? ORDER BY data_date", (d0,))
        olt = _mk_rows(mk,
            "SELECT data_date, contract, buy5, sell5, buy10, sell10,"
            " buy5_all, sell5_all, buy10_all, sell10_all, oi FROM options_lt"
            " WHERE data_date>=? ORDER BY data_date", (d0,))
        pcr = _mk_rows(mk,
            "SELECT data_date, vol_ratio, oi_ratio FROM pc_ratio"
            " WHERE data_date>=? ORDER BY data_date", (d0,))
        spot = _mk_rows(mk,
            "SELECT data_date, foreign_net, trust_net, dealer_net, total_net"
            " FROM inst_spot WHERE data_date>=? ORDER BY data_date", (d0,))
    finally:
        mk.close()

    def with_delta(rows, key, fields):
        """補上「與前一日的增減」。

        逐 key(契約/買賣權)各自往前找上一個交易日 —— 未平倉是存量,判讀時
        看的是「今天比昨天多空了多少口」,而不是絕對數字本身
        (見 market-bot-dashboard-prefs:值 + 前日Δ)。
        前一日缺資料時回 None,不要用 0 冒充「沒變動」。
        """
        out, prev = [], {}
        for r in rows:
            k = r[key]
            cur = {f: r[f] for f in fields}
            p = prev.get(k)
            for f in fields:
                r[f + "_d"] = (None if p is None or p[f] is None
                               or cur[f] is None else cur[f] - p[f])
            prev[k] = cur
            out.append(r)
        return out

    def inst(rows, key):
        packed = [{"d": r[0], key: r[1], "foreign": r[2], "trust": r[3],
                   "dealer": r[4], "foreign_t": r[5], "trust_t": r[6],
                   "dealer_t": r[7]} for r in rows]
        return with_delta(packed, key, ("foreign", "trust", "dealer"))

    def lt(rows):
        packed = [{"d": r[0], "contract": r[1],
                   "net5": (r[2] or 0) - (r[3] or 0),
                   "net10": (r[4] or 0) - (r[5] or 0),
                   "net5_all": (r[6] or 0) - (r[7] or 0),
                   "net10_all": (r[8] or 0) - (r[9] or 0),
                   "oi": r[10]} for r in rows]
        for r in packed:                      # 第 6~10 名 = 前十大 − 前五大
            r["net6_10"] = r["net10"] - r["net5"]
        return with_delta(packed, "contract",
                          ("net5", "net10", "net6_10", "net5_all",
                           "net10_all", "oi"))

    return {
        "days": days, "range": [d0, px[-1][0]],
        "price": [{"d": r[0], "open": r[1], "high": r[2], "low": r[3],
                   "close": r[4], "pts": r[5], "pct": r[6], "amount": r[7]}
                  for r in px],
        "fut_inst": inst(fut, "contract"),
        "opt_inst": inst(opt, "cp"),
        "fut_lt": lt(flt), "opt_lt": lt(olt),
        "pc_ratio": with_delta(
            [{"d": r[0], "k": "pcr", "vol": r[1], "oi": r[2]} for r in pcr],
            "k", ("vol", "oi")),
        "inst_spot": with_delta(
            [{"d": r[0], "k": "spot", "foreign": r[1], "trust": r[2],
              "dealer": r[3], "total": r[4]} for r in spot],
            "k", ("foreign", "trust", "dealer", "total")),
    }


ROUTES = {"/api/meta": api_meta, "/api/brokers": api_brokers,
          "/api/pair": api_pair,
          "/api/broker": api_broker, "/api/stock": api_stock,
          "/api/stocks": api_stocks,
          "/api/broker/pnl": api_broker_pnl,
          "/api/stock/pnl": api_stock_pnl,
          "/api/stock/featured": api_stock_featured,
          "/api/index": api_index,
          "/api/etf/meta": api_etf_meta,
          "/api/etf/consensus": api_etf_consensus,
          "/api/etf/fund": api_etf_fund}


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
        name = "index.html" if u.path == "/" else u.path.lstrip("/")
        if name in STATIC:
            f = HERE / name
            if not f.exists():
                return self._send(404, f"{name} missing".encode(), "text/plain")
            # 不快取:改版後使用者若拿到舊檔,會以為新功能沒生效。ES module
            # 尤其要注意 —— 混到一份舊的 app.js 會出現「函式不存在」的怪錯。
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC[name])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if name.startswith("demo/"):
            leaf = name[len("demo/"):]
            if not DEMO_RE.match(leaf):
                return self._send(404, b'{"error":"not found"}',
                                  "application/json")
            f = DEMO_DIR / leaf
            if not f.exists():
                return self._send(404, b'{"error":"no demo data"}',
                                  "application/json")
            return self._send(200, f.read_bytes(),
                              "application/json; charset=utf-8")
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
