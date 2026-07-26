"""資料源:三大法人上櫃買賣超 + 上櫃逐股籌碼(TPEx 櫃買中心)。

LINE 訊息只用「三大法人買賣金額彙總表」:外資/投信/自營每日在上櫃市場的
買賣超金額(正=買超、負=賣超)。與 inst_spot(上市)互補——中小型股籌碼
主要在上櫃,只看上市會漏掉法人在中小型股的動向。

另抓三張逐股表入庫(不出 LINE,供 my_chips 交叉上櫃持股):
- insti/dailyTrade:逐股三大法人買賣超(股)→ inst_otc_stock
- margin/balance:逐股融資融券餘額(張)→ margin_otc_stock
- margin/sbl:逐股借券賣出餘額(股,版面同 TWSE TWT93U)→ margin_otc_stock

TPEx 開放 JSON、無 bot 防護,標準 requests 即可。可回補歷史。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
from datetime import date

import requests

from core import store
from core.twse import UA, backfill_days, fetch_recent, to_int

NAME = "inst_otc"
BASE = "https://www.tpex.org.tw/www/zh-tw"
URL = f"{BASE}/insti/summary"
STOCK_URL = f"{BASE}/insti/dailyTrade"
MARGIN_URL = f"{BASE}/margin/balance"
SBL_URL = f"{BASE}/margin/sbl"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inst_otc (
    data_date  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    foreign_net REAL, trust_net REAL, dealer_net REAL, total_net REAL,  -- 元
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS inst_otc_stock (
    data_date   TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT,
    foreign_net REAL, trust_net REAL, dealer_net REAL, total_net REAL,  -- 股
    PRIMARY KEY (data_date, code)
);
CREATE TABLE IF NOT EXISTS margin_otc_stock (
    data_date  TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT,
    fin_prev   REAL, fin_bal   REAL,                 -- 融資餘額(張)
    short_prev REAL, short_bal REAL,                 -- 融券餘額(張)
    sbl_prev   REAL, sbl_bal   REAL,                 -- 借券賣出餘額(股)
    PRIMARY KEY (data_date, code)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _get(url, d: date, **extra):
    r = requests.get(url, params={"date": f"{d:%Y/%m/%d}", "response": "json",
                                  **extra}, headers=UA, timeout=30)
    r.raise_for_status()
    j = r.json()
    return (j.get("tables") or [{}])[0], r.content


def _fetch_day(d: date):
    """回傳 (data_date, 淨額 dict, 逐股法人, 逐股資券借券, raws) 或 None。"""
    t, raw = _get(URL, d, type="Daily")
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
    dd = f"{int(y) + 1911}-{m}-{day}"

    # 逐股三大法人(股):欄位為 7 組買/賣/買賣超——外資不含自營、外資自營、
    # 外資合計(idx10)、投信(idx13)、自營自行、自營避險、自營合計(idx22),
    # 最後一欄(idx23)為三大法人合計。
    t2, raw2 = _get(STOCK_URL, d, type="Daily", sect="EW")
    inst = {row[0].strip(): (row[1].strip(), to_int(row[10]), to_int(row[13]),
                             to_int(row[22]), to_int(row[23]))
            for row in (t2.get("data") or [])}

    # 逐股資券(張):前資餘額 idx2、資餘額 idx6、前券餘額 idx10、券餘額 idx14
    t3, raw3 = _get(MARGIN_URL, d)
    ms = {row[0].strip(): [row[1].strip(), to_int(row[2]), to_int(row[6]),
                           to_int(row[10]), to_int(row[14]), 0, 0]
          for row in (t3.get("data") or [])}
    # 逐股借券賣出(股,版面同 TWT93U):前日餘額 idx8、當日餘額 idx12
    t4, raw4 = _get(SBL_URL, d)
    for row in (t4.get("data") or []):
        entry = ms.setdefault(row[0].strip(),
                              [row[1].strip(), 0, 0, 0, 0, 0, 0])
        entry[5], entry[6] = to_int(row[8]), to_int(row[12])
    if not inst or not ms:
        return None
    return dd, net, inst, ms, (raw, raw2, raw3, raw4)


def _save(conn, got):
    dd, net, inst, ms, (raw, raw2, raw3, raw4) = got
    store.save_raw(NAME, dd, "summary", "json", raw)
    store.save_raw(NAME, dd, "dailyTrade", "json", raw2)
    store.save_raw(NAME, dd, "balance", "json", raw3)
    store.save_raw(NAME, dd, "sbl", "json", raw4)
    with conn:
        conn.execute("INSERT OR REPLACE INTO inst_otc VALUES (?,?,?,?,?,?)",
                     (dd, net["外資"], net["投信"], net["自營"], net["合計"],
                      store.now()))
        conn.execute("DELETE FROM inst_otc_stock WHERE data_date=?", (dd,))
        conn.executemany(
            "INSERT INTO inst_otc_stock VALUES (?,?,?,?,?,?,?)",
            [(dd, code, *vals) for code, vals in inst.items()])
        conn.execute("DELETE FROM margin_otc_stock WHERE data_date=?", (dd,))
        conn.executemany(
            "INSERT INTO margin_otc_stock VALUES (?,?,?,?,?,?,?,?,?)",
            [(dd, code, *vals) for code, vals in ms.items()])


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
