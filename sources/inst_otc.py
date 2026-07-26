"""資料源:三大法人上櫃買賣超(TPEx 櫃買中心)。

TPEx「三大法人買賣金額彙總表」:外資/投信/自營每日在上櫃市場的買賣超金額
(正=買超、負=賣超)。與 inst_spot(上市)互補——中小型股籌碼主要在上櫃,
只看上市會漏掉法人在中小型股的動向。

TPEx 開放 JSON、無 bot 防護,標準 requests 即可。可回補歷史。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
from datetime import date

import requests

from core import store
from core.twse import UA, backfill_days, fetch_recent, to_int

NAME = "inst_otc"
URL = "https://www.tpex.org.tw/www/zh-tw/insti/summary"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inst_otc (
    data_date  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    foreign_net REAL, trust_net REAL, dealer_net REAL, total_net REAL,  -- 元
    fetched_at TEXT
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _fetch_day(d: date):
    """回傳 (data_date, 淨額 dict, 原始 bytes) 或 None(該日無資料)。"""
    r = requests.get(URL, params={"type": "Daily", "date": f"{d:%Y/%m/%d}",
                                  "response": "json"}, headers=UA, timeout=20)
    r.raise_for_status()
    j = r.json()
    t = (j.get("tables") or [{}])[0]
    if not t.get("data"):
        return None
    net = {}
    for row in t["data"]:
        name = row[0].strip()
        if name == "外資及陸資合計":
            net["外資"] = to_int(row[3])
        elif name == "投信":
            net["投信"] = to_int(row[3])
        elif name == "自營商合計":
            net["自營"] = to_int(row[3])
        elif name.startswith("三大法人合計"):
            net["合計"] = to_int(row[3])
    if len(net) < 4:
        return None
    y, m, day = t["date"].split("/")        # 民國年
    return f"{int(y) + 1911}-{m}-{day}", net, r.content


def _save(conn, got):
    dd, net, raw = got
    store.save_raw(NAME, dd, "summary", "json", raw)
    with conn:
        conn.execute("INSERT OR REPLACE INTO inst_otc VALUES (?,?,?,?,?,?)",
                     (dd, net["外資"], net["投信"], net["自營"], net["合計"],
                      store.now()))


def fetch(conn):
    return fetch_recent(conn, NAME, _fetch_day, _save)


def backfill(conn, days):
    backfill_days(conn, NAME, _fetch_day, _save, days)


def build_message(conn):
    row = conn.execute(
        "SELECT data_date,foreign_net,trust_net,dealer_net,total_net "
        "FROM inst_otc ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return None, {}
    dd, fo, tr, de, to = row

    def line(name, v):
        return f"{name} {'買超' if v >= 0 else '賣超'} {abs(v) / 1e8:,.1f} 億"

    text = "\n".join([
        f"📊 上櫃三大法人買賣超 {dd[5:].replace('-', '/')}",
        line("外資", fo), line("投信", tr), line("自營", de), line("合計", to),
    ])
    return text, {"date": dd}
