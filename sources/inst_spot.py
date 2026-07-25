"""資料源:三大法人現貨買賣超(TWSE 上市集中市場)。

TWSE BFI82U「三大法人買賣金額統計表」——外資/投信/自營每日在現貨市場的
買賣超金額(買賣差額,正=買超、負=賣超)。與期貨外資淨部位互補:現貨買+
期貨空常代表避險,現貨賣+期貨空才是真看空。

TWSE 開放 JSON、無 bot 防護,標準 requests 即可。可回補歷史。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
from datetime import date, datetime, timedelta

import requests

from core import store

NAME = "inst_spot"
URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
UA = {"User-Agent": "Mozilla/5.0"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS inst_spot (
    data_date  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    foreign_net REAL, trust_net REAL, dealer_net REAL, total_net REAL,  -- 元
    fetched_at TEXT
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _int(s):
    s = str(s).replace(",", "").strip()
    return int(s) if s.lstrip("-").isdigit() else 0


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
        name, diff = row[0], _int(row[3])
        if name.startswith("外資"):
            net["外資"] += diff
        elif name.startswith("投信"):
            net["投信"] += diff
        elif name.startswith("自營商"):
            net["自營"] += diff
        elif name.startswith("合計"):
            net["合計"] = diff
    dd = j.get("date", f"{d:%Y%m%d}")
    return f"{dd[:4]}-{dd[4:6]}-{dd[6:8]}", net, r.content


def _save(conn, dd, net, raw):
    store.save_raw(NAME, dd, "BFI82U", "json", raw)
    with conn:
        conn.execute("INSERT OR REPLACE INTO inst_spot VALUES (?,?,?,?,?,?)",
                     (dd, net["外資"], net["投信"], net["自營"], net["合計"],
                      datetime.now().isoformat(timespec="seconds")))


def fetch(conn):
    init(conn)
    for i in range(0, 6):  # 從今天往回找最近一個有資料的交易日
        d = date.today() - timedelta(days=i)
        try:
            got = _fetch_day(d)
        except Exception as e:
            print(f"[fetch] inst_spot: 失敗 — {e}")
            return ["inst_spot"]
        if got:
            dd, net, raw = got
            _save(conn, dd, net, raw)
            print(f"[fetch] inst_spot: 資料日 {dd}")
            return []
    print("[fetch] inst_spot: 近 6 日查無資料")
    return ["inst_spot"]


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
            dd, net, raw = got
            _save(conn, dd, net, raw)
            print(f"[backfill] inst_spot {dd}")
        except Exception as e:
            print(f"[backfill] inst_spot {d}: 失敗 — {e}")


def build_message(conn):
    init(conn)
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
