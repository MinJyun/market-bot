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
from datetime import date

import requests

from core import store
from core.twse import UA, backfill_days, fetch_recent, iso, to_int
from sources import active_etf

NAME = "inst_stock"
URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
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
        rows[code] = (row[1].strip(), to_int(row[4]) + to_int(row[7]),
                      to_int(row[10]), to_int(row[11]), to_int(row[18]))
    return iso(j.get("date", f"{d:%Y%m%d}")), rows, r.content


def _save(conn, got):
    dd, rows, raw = got
    store.save_raw(NAME, dd, "T86", "json", raw)
    now = store.now()
    with conn:
        conn.execute("DELETE FROM inst_stock WHERE data_date=?", (dd,))
        conn.executemany(
            "INSERT INTO inst_stock VALUES (?,?,?,?,?,?,?,?)",
            [(dd, code, name, fo, tr, de, to, now)
             for code, (name, fo, tr, de, to) in rows.items()])
    return f",{len(rows)} 檔個股"


def fetch(conn):
    return fetch_recent(conn, NAME, _fetch_day, _save)


def backfill(conn, days):
    backfill_days(conn, NAME, _fetch_day, _save, days)


# ================================================================ LINE 訊息
def _fmt(code, name, total_net):
    d = "買超" if total_net >= 0 else "賣超"
    return f"{name}({code}) {d}{abs(total_net) / 1000:,.0f}張"


def build_message(conn):
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

    watch = active_etf.latest_holding_codes(conn) & by_code.keys()
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
