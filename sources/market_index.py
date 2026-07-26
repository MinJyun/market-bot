"""資料源:加權指數收盤行情(TWSE 每日價格指數,全市場)。

TWSE MI_INDEX(type=IND)「價格指數」表:各類指數當日收盤、漲跌點數、漲跌
百分比,本模組只取「發行量加權股價指數」(大盤)。用途是給其他籌碼指標
(法人買賣超、融資融券)一個當天大盤漲跌的對照基準。

TWSE 開放 JSON、無 bot 防護,標準 requests 即可。可回補歷史。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
from datetime import date, datetime, timedelta

import requests

from core import store

NAME = "market_index"
URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
UA = {"User-Agent": "Mozilla/5.0"}
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
    dd = j.get("date", f"{d:%Y%m%d}")
    return f"{dd[:4]}-{dd[4:6]}-{dd[6:8]}", close, pts, pct, r.content


def _save(conn, dd, close, pts, pct, raw):
    store.save_raw(NAME, dd, "MI_INDEX", "json", raw)
    with conn:
        conn.execute("INSERT OR REPLACE INTO market_index VALUES (?,?,?,?,?)",
                     (dd, close, pts, pct, datetime.now().isoformat(timespec="seconds")))


def fetch(conn):
    init(conn)
    for i in range(0, 6):  # 從今天往回找最近一個有資料的交易日
        d = date.today() - timedelta(days=i)
        try:
            got = _fetch_day(d)
        except Exception as e:
            print(f"[fetch] market_index: 失敗 — {e}")
            return ["market_index"]
        if got:
            dd, close, pts, pct, raw = got
            _save(conn, dd, close, pts, pct, raw)
            print(f"[fetch] market_index: 資料日 {dd}")
            return []
    print("[fetch] market_index: 近 6 日查無資料")
    return ["market_index"]


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
            dd, close, pts, pct, raw = got
            _save(conn, dd, close, pts, pct, raw)
            print(f"[backfill] market_index {dd}")
        except Exception as e:
            print(f"[backfill] market_index {d}: 失敗 — {e}")


def build_message(conn):
    init(conn)
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
