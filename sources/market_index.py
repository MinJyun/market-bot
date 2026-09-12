"""資料源:加權指數收盤行情與成交金額(TWSE 每日市場成交資訊)。

TWSE FMTQIK「每日市場成交資訊」:每日收盤指數、漲跌點數、成交金額(元)。
給其他籌碼指標(法人買賣超、融資融券)當漲跌與量能的對照基準——縮量跌與
爆量跌的判讀不同。漲跌百分比由點數回推(API 未提供)。

一次請求回傳整個月,fetch 順帶回補當月;backfill 依月往前抓。
TWSE 開放 JSON、無 bot 防護,標準 requests 即可。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
import sys
from datetime import date

import requests

from core import store
from core.twse import UA

NAME = "market_index"
URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
# 開高低收在另一支 endpoint(FMTQIK 只給收盤與成交金額)。路徑是
# /rwd/zh/**TAIEX**/,不是 /rwd/zh/afterTrading/ —— 後者回 text/html 不是 JSON。
URL_OHLC = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_index (
    data_date  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    close      REAL,               -- 收盤指數
    change_pts REAL,               -- 漲跌點數(signed)
    change_pct REAL,               -- 漲跌百分比(signed,由點數回推)
    amount     REAL,               -- 成交金額(元)
    fetched_at TEXT,
    open REAL, high REAL, low REAL   -- 來自 MI_5MINS_HIST
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


def _fetch_month(conn, d: date):
    """抓 d 所在月份的全部交易日入庫,回傳最新資料日(該月無資料回 None)。"""
    r = requests.get(URL, params={"date": f"{d:%Y%m}01", "response": "json"},
                     headers=UA, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    rows = []
    for rec in j["data"]:
        y, m, day = rec[0].split("/")       # 民國年
        dd = f"{int(y) + 1911}-{m}-{day}"
        close, pts, amount = _num(rec[4]), _num(rec[5]), _num(rec[2])
        prev_close = close - pts
        pct = round(pts / prev_close * 100, 2) if prev_close else 0.0
        rows.append((dd, close, pts, pct, amount))
    now = store.now()
    with conn:
        # 具名欄位 + upsert,不用 INSERT OR REPLACE:後者會先刪整列再插,
        # 把另一支 endpoint 抓來的 open/high/low 一起清成 NULL。
        conn.executemany(
            "INSERT INTO market_index (data_date, close, change_pts,"
            " change_pct, amount, fetched_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(data_date) DO UPDATE SET close=excluded.close,"
            " change_pts=excluded.change_pts, change_pct=excluded.change_pct,"
            " amount=excluded.amount, fetched_at=excluded.fetched_at",
            [(*row, now) for row in rows])
    store.save_raw(NAME, rows[-1][0], "FMTQIK", "json", r.content)
    return rows[-1][0]


def _fetch_ohlc_month(conn, d: date):
    """抓 d 所在月份的開高低(MI_5MINS_HIST),回傳 (寫入筆數, 收盤不符筆數)。

    該 endpoint 也給收盤指數,拿來與 FMTQIK 的收盤交叉驗證 —— 兩支不同的
    endpoint 對不上就是有一邊解析錯了,不該悄悄放過。
    """
    r = requests.get(URL_OHLC, params={"date": f"{d:%Y%m}01", "response": "json"},
                     headers=UA, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK" or not j.get("data"):
        return 0, 0
    rows, bad = [], 0
    have = dict(conn.execute("SELECT data_date, close FROM market_index"))
    for rec in j["data"]:
        y, m, day = rec[0].split("/")
        dd = f"{int(y) + 1911}-{m}-{day}"
        o, h, lo, c = (_num(rec[1]), _num(rec[2]), _num(rec[3]), _num(rec[4]))
        if dd in have and have[dd] and abs(have[dd] - c) > 0.01:
            bad += 1
            print(f"[ohlc] {dd} 收盤不符:FMTQIK {have[dd]} vs "
                  f"MI_5MINS_HIST {c}", file=sys.stderr)
        rows.append((dd, o, h, lo))
    with conn:
        conn.executemany(
            "INSERT INTO market_index (data_date, open, high, low)"
            " VALUES (?,?,?,?) ON CONFLICT(data_date) DO UPDATE SET"
            " open=excluded.open, high=excluded.high, low=excluded.low", rows)
    return len(rows), bad


def fetch(conn):
    try:
        dd = _fetch_month(conn, date.today())
        if not dd:  # 月初尚無交易日(如 1 日遇假日),退回上月
            dd = _fetch_month(conn, date.today().replace(day=1) - date.resolution)
        if not dd:
            print("[fetch] market_index: 查無資料")
            return ["market_index"]
        n, bad = _fetch_ohlc_month(conn, date.today())
        print(f"[fetch] market_index: 資料日 {dd}(開高低 {n} 筆"
              f"{f',收盤不符 {bad} 筆' if bad else ''})")
        return []
    except Exception as e:
        print(f"[fetch] market_index: 失敗 — {e}")
        return ["market_index"]


def backfill(conn, days):
    d = date.today()
    months = days // 20 + 1  # 交易日換算月份,寧多勿少
    for _ in range(months):
        d = d.replace(day=1) - date.resolution  # 上月最後一天
        try:
            dd = _fetch_month(conn, d)
            n, bad = _fetch_ohlc_month(conn, d)
            print(f"[backfill] market_index {d:%Y-%m}"
                  f"{'' if dd else ':無資料'}(開高低 {n} 筆"
                  f"{f',收盤不符 {bad} 筆' if bad else ''})")
        except Exception as e:
            print(f"[backfill] market_index {d:%Y-%m}: 失敗 — {e}")


def build_message(conn):
    rows = conn.execute(
        "SELECT data_date, close, change_pts, change_pct, amount "
        "FROM market_index ORDER BY data_date DESC LIMIT 2").fetchall()
    if not rows:
        return None, {}
    dd, close, pts, pct, amount = rows[0]
    arrow = "▲" if pts > 0 else ("▼" if pts < 0 else "-")

    lines = [f"📊 加權指數 {dd[5:].replace('-', '/')}",
             f"收盤 {close:,.2f}（{arrow}{abs(pts):,.2f}，{pct:+.2f}%）"]
    if amount:
        vol = f"成交 {amount / 1e8:,.0f}億"
        if len(rows) > 1 and rows[1][4]:
            vol += f"（較前日{(amount - rows[1][4]) / 1e8:+,.0f}億）"
        lines.append(vol)
    return "\n".join(lines), {"date": dd}
