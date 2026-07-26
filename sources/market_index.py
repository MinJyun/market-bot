"""資料源:加權指數收盤行情(TWSE 每日價格指數,全市場)。

TWSE MI_INDEX(type=IND)「價格指數」表:各類指數當日收盤、漲跌點數、漲跌
百分比,本模組只取「發行量加權股價指數」(大盤)。用途是給其他籌碼指標
(法人買賣超、融資融券)一個當天大盤漲跌的對照基準。

TWSE 開放 JSON、無 bot 防護,標準 requests 即可。可回補歷史。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
from datetime import date

import requests

from core import store
from core.twse import UA, backfill_days, fetch_recent, iso

NAME = "market_index"
URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
ROW_NAME = "發行量加權股價指數"

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_index (
    data_date  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    close      REAL,               -- 收盤指數
    change_pts REAL,               -- 漲跌點數(signed)
    change_pct REAL,               -- 漲跌百分比(signed)
    fetched_at TEXT
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _num(s):
    s = str(s).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fetch_day(d: date):
    """回傳 (data_date, 收盤, 漲跌點數, 漲跌百分比, 原始 bytes) 或 None(該日無資料)。"""
    r = requests.get(URL, params={"date": f"{d:%Y%m%d}", "type": "IND",
                                  "response": "json"}, headers=UA, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK":
        return None
    row = None
    for t in j.get("tables", []):
        for rec in (t.get("data") or []):
            if rec[0] == ROW_NAME:
                row = rec
                break
        if row:
            break
    if not row:
        return None
    close = _num(row[1])
    pct = _num(row[4])          # 已含正負號(跌為負、漲為正或0)
    pts = _num(row[3])          # 只有絕對值,方向以百分比正負判斷
    if pct < 0:
        pts = -pts
    return iso(j.get("date", f"{d:%Y%m%d}")), close, pts, pct, r.content


def _save(conn, got):
    dd, close, pts, pct, raw = got
    store.save_raw(NAME, dd, "MI_INDEX", "json", raw)
    with conn:
        conn.execute("INSERT OR REPLACE INTO market_index VALUES (?,?,?,?,?)",
                     (dd, close, pts, pct, store.now()))


def fetch(conn):
    return fetch_recent(conn, NAME, _fetch_day, _save)


def backfill(conn, days):
    backfill_days(conn, NAME, _fetch_day, _save, days)


def build_message(conn):
    row = conn.execute(
        "SELECT data_date, close, change_pts, change_pct "
        "FROM market_index ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return None, {}
    dd, close, pts, pct = row
    arrow = "▲" if pts > 0 else ("▼" if pts < 0 else "-")

    text = (f"📊 加權指數 {dd[5:].replace('-', '/')}\n"
            f"收盤 {close:,.2f}（{arrow}{abs(pts):,.2f}，{pct:+.2f}%）")
    return text, {"date": dd}
