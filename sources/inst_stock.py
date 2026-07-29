"""資料源:個股籌碼訊息(TWSE T86 入庫 + 上市/上櫃逐股排行)。

抓取面:TWSE「三大法人買賣超日報」(T86)依個股呈現當日外資/投信/自營/合計
買賣超股數。原始回應約 1.3 萬筆(含 ETF、權證、特別股),只保留純股票
(4 碼純數字、非 00 開頭代號)入庫,約 1000 檔。

訊息面:獨立成一則「個股籌碼」LINE(main.py 的 stocks 分組),上市/上櫃分開:
  - 每市場列 三大法人/外資/投信 的買超前5、賣超前5,連續同向 ≥2 天標注
    「連N買/連N賣」(投信連買常視為認養訊號)。
  - 上櫃資料讀 inst_otc 來源入庫的 inst_otc_stock 表(跨來源讀表,兩邊
    docstring 互相標注)。
  - ETF 關注股:交叉比對本專案主動 ETF 目前持股(上市+上櫃),顯示交集中
    三大法人買賣超前 5——對照 ETF 經理人加減碼是否與法人同向。

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
def _streak(conn, table, col, code, dd, cur):
    """含當日的連續同向天數(最多回看 15 個交易日)。"""
    days = 1
    for (v,) in conn.execute(
            f"SELECT {col} FROM {table} WHERE code=? AND data_date<? "
            "ORDER BY data_date DESC LIMIT 15", (code, dd)):
        if v and (v > 0) == (cur > 0):
            days += 1
        else:
            break
    return days


def _market_sections(conn, label, table, dd, lines):
    """一個市場的 三大法人/外資/投信 買賣超前5(含連N買/賣標注)。"""
    rows = [r for r in conn.execute(
        f"SELECT code, name, foreign_net, trust_net, total_net FROM {table} "
        "WHERE data_date=?", (dd,)) if STOCK_RE.match(r[0])]

    def fmt(r, idx, col):
        code, name, net = r[0], r[1], r[idx]
        d = _streak(conn, table, col, code, dd, net)
        tag = f" 連{d}{'買' if net > 0 else '賣'}" if d >= 2 else ""
        return f"　{name}({code}) {net / 1000:+,.0f}張{tag}"

    for title, idx, col in (("三大法人", 4, "total_net"),
                            ("外資", 2, "foreign_net"),
                            ("投信", 3, "trust_net")):
        ranked = sorted((r for r in rows if r[idx]), key=lambda r: -r[idx])
        if not ranked:
            continue
        lines.append(f"▍{label}·{title}")
        lines += [fmt(r, idx, col) for r in ranked[:5] if r[idx] > 0]
        lines += [fmt(r, idx, col) for r in ranked[-5:][::-1] if r[idx] < 0]


def build_message(conn):
    dd = conn.execute("SELECT MAX(data_date) FROM inst_stock").fetchone()[0]
    if not dd:
        return None, {}
    odd = conn.execute("SELECT MAX(data_date) FROM inst_otc_stock").fetchone()[0]

    lines = [f"📊 個股籌碼 {dd[5:].replace('-', '/')}"]
    _market_sections(conn, "上市", "inst_stock", dd, lines)
    if odd:
        _market_sections(conn, "上櫃", "inst_otc_stock", odd, lines)

    # ETF 關注股:主動 ETF 持股 ∩ 上市+上櫃法人買賣超
    by_code = {r[0]: r for r in conn.execute(
        "SELECT code, name, total_net FROM inst_stock WHERE data_date=?", (dd,))}
    if odd:
        for r in conn.execute("SELECT code, name, total_net FROM inst_otc_stock "
                              "WHERE data_date=?", (odd,)):
            by_code.setdefault(r[0], r)
    watch = active_etf.latest_holding_codes(conn) & by_code.keys()
    if watch:
        top = sorted((by_code[c] for c in watch), key=lambda r: -abs(r[2]))[:5]
        lines.append("▍ETF關注股(交叉比對主動ETF目前持股)")
        lines += [f"　{name}({code}) {net / 1000:+,.0f}張"
                  for code, name, net in top]

    return "\n".join(lines), {"twse": dd, "otc": odd}
