"""資料源:三大法人個股買賣超(TWSE T86,依股票代號)。

TWSE「三大法人買賣超日報」(T86):不只外資,也含投信/自營,依個股呈現當日
外資/投信/自營/合計買賣超股數。原始回應約 1.3 萬筆(含 ETF、權證、特別股),
只保留純股票(4 碼純數字、非 00 開頭代號)入庫,約 1000 檔——00 開頭的 4 碼
(0050、0056...)是 ETF,股票代號從未以 00 開頭。

訊息分兩塊:
  1. ETF 關注股:交叉比對本專案 7 檔主動 ETF 目前持股清單,顯示交集中三大
     法人買賣超前 5 大——用來對照 ETF 經理人的加減碼是否與法人籌碼同向。
  2. 全市場排行:純股票中三大法人買超/賣超前 5 大,與 ETF 持股無關的大盤
     籌碼指標。

TWSE 開放 JSON、無 bot 防護,標準 requests 即可。可回補歷史。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
import re
from datetime import date, datetime, timedelta

import requests

from core import store
from sources import active_etf

NAME = "inst_stock"
URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
UA = {"User-Agent": "Mozilla/5.0"}
STOCK_RE = re.compile(r"^(?!00)\d{4}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS inst_stock (
    data_date   TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT,
    foreign_net REAL, trust_net REAL, dealer_net REAL, total_net REAL,  -- 股數
    fetched_at  TEXT,
    PRIMARY KEY (data_date, code)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _int(s):
    s = str(s).replace(",", "").strip()
    return int(s) if s.lstrip("-").isdigit() else 0


def _fetch_day(d: date):
    """回傳 (data_date, {code: (name,外資淨,投信淨,自營淨,合計淨)}, 原始 bytes) 或 None。"""
    r = requests.get(URL, params={"date": f"{d:%Y%m%d}", "selectType": "ALL",
                                  "response": "json"}, headers=UA, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    rows = {}
    for row in j["data"]:
        code = row[0]
        if not STOCK_RE.match(code):
            continue
        rows[code] = (row[1].strip(), _int(row[4]) + _int(row[7]),
                      _int(row[10]), _int(row[11]), _int(row[18]))
    dd = j.get("date", f"{d:%Y%m%d}")
    return f"{dd[:4]}-{dd[4:6]}-{dd[6:8]}", rows, r.content


def _save(conn, dd, rows, raw):
    store.save_raw(NAME, dd, "T86", "json", raw)
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM inst_stock WHERE data_date=?", (dd,))
        conn.executemany(
            "INSERT INTO inst_stock VALUES (?,?,?,?,?,?,?,?)",
            [(dd, code, name, fo, tr, de, to, now)
             for code, (name, fo, tr, de, to) in rows.items()])


def fetch(conn):
    init(conn)
    for i in range(0, 6):  # 從今天往回找最近一個有資料的交易日
        d = date.today() - timedelta(days=i)
        try:
            got = _fetch_day(d)
        except Exception as e:
            print(f"[fetch] inst_stock: 失敗 — {e}")
            return ["inst_stock"]
        if got:
            dd, rows, raw = got
            _save(conn, dd, rows, raw)
            print(f"[fetch] inst_stock: 資料日 {dd},{len(rows)} 檔個股")
            return []
    print("[fetch] inst_stock: 近 6 日查無資料")
    return ["inst_stock"]


def backfill(conn, days):
    init(conn)
    for i in range(1, days + 1):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            got = _fetch_day(d)
            if not got:
                continue
            dd, rows, raw = got
            _save(conn, dd, rows, raw)
            print(f"[backfill] inst_stock {dd}")
        except Exception as e:
            print(f"[backfill] inst_stock {d}: 失敗 — {e}")


# ================================================================ LINE 訊息
def _etf_watchlist(conn):
    """回傳本專案主動 ETF 目前(各檔最新資料日)持股的 distinct 股票代號集合。"""
    active_etf.init(conn)
    codes = set()
    for (etf,) in conn.execute("SELECT DISTINCT etf FROM holding"):
        dd = conn.execute("SELECT MAX(data_date) FROM holding WHERE etf=?",
                          (etf,)).fetchone()[0]
        codes |= {r[0] for r in conn.execute(
            "SELECT code FROM holding WHERE etf=? AND data_date=?", (etf, dd))}
    return codes


def _fmt(code, name, total_net):
    d = "買超" if total_net >= 0 else "賣超"
    return f"{name}({code}) {d}{abs(total_net) / 1000:,.0f}張"


def build_message(conn):
    init(conn)
    dd = conn.execute("SELECT MAX(data_date) FROM inst_stock").fetchone()[0]
    if not dd:
        return None, {}
    all_rows = conn.execute(
        "SELECT code, name, total_net FROM inst_stock WHERE data_date=?",
        (dd,)).fetchall()
    if not all_rows:
        return None, {}
    by_code = {r[0]: r for r in all_rows}

    lines = [f"📊 三大法人個股買賣超 {dd[5:].replace('-', '/')}"]

    watch = _etf_watchlist(conn) & by_code.keys()
    if watch:
        top = sorted((by_code[c] for c in watch), key=lambda r: -abs(r[2]))[:5]
        lines.append("▍ETF關注股(交叉比對主動ETF目前持股)")
        for code, name, net in top:
            lines.append(f"　{_fmt(code, name, net)}")

    ranked = sorted(all_rows, key=lambda r: -r[2])
    lines.append("▍全市場買超前5")
    for code, name, net in ranked[:5]:
        lines.append(f"　{_fmt(code, name, net)}")
    lines.append("▍全市場賣超前5")
    for code, name, net in ranked[-5:][::-1]:
        lines.append(f"　{_fmt(code, name, net)}")

    return "\n".join(lines), {"date": dd}
