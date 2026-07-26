"""資料源:三大法人現貨買賣超(TWSE 上市集中市場)。

TWSE BFI82U「三大法人買賣金額統計表」——外資/投信/自營每日在現貨市場的
買賣超金額(買賣差額,正=買超、負=賣超)。與期貨外資淨部位互補:現貨買+
期貨空常代表避險,現貨賣+期貨空才是真看空。

TWSE 開放 JSON、無 bot 防護,標準 requests 即可。可回補歷史。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
from datetime import date

import requests

from core import store
from core.twse import UA, backfill_days, fetch_recent, iso, to_int

NAME = "inst_spot"
URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inst_spot (
    data_date  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    foreign_net REAL, trust_net REAL, dealer_net REAL, total_net REAL,  -- 元
    fetched_at TEXT
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _fetch_day(d: date):
    """回傳 (data_date, 淨額 dict, 原始 bytes) 或 None(該日無資料)。"""
    r = requests.get(URL, params={"type": "day", "dayDate": f"{d:%Y%m%d}",
                                  "response": "json"}, headers=UA, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    net = {"外資": 0, "投信": 0, "自營": 0, "合計": 0}
    for row in j["data"]:
        name, diff = row[0], to_int(row[3])
        if name.startswith("外資"):
            net["外資"] += diff
        elif name.startswith("投信"):
            net["投信"] += diff
        elif name.startswith("自營商"):
            net["自營"] += diff
        elif name.startswith("合計"):
            net["合計"] = diff
    return iso(j.get("date", f"{d:%Y%m%d}")), net, r.content


def _save(conn, got):
    dd, net, raw = got
    store.save_raw(NAME, dd, "BFI82U", "json", raw)
    with conn:
        conn.execute("INSERT OR REPLACE INTO inst_spot VALUES (?,?,?,?,?,?)",
                     (dd, net["外資"], net["投信"], net["自營"], net["合計"],
                      store.now()))


def fetch(conn):
    return fetch_recent(conn, NAME, _fetch_day, _save)


def backfill(conn, days):
    backfill_days(conn, NAME, _fetch_day, _save, days)


def build_message(conn):
    row = conn.execute(
        "SELECT data_date,foreign_net,trust_net,dealer_net,total_net "
        "FROM inst_spot ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return None, {}
    dd, fo, tr, de, to = row

    def line(name, v):
        return f"{name} {'買超' if v >= 0 else '賣超'} {abs(v) / 1e8:,.1f} 億"

    text = "\n".join([
        f"📊 三大法人現貨買賣超 {dd[5:].replace('-', '/')}",
        line("外資", fo), line("投信", tr), line("自營", de), line("合計", to),
    ])
    return text, {"date": dd}
